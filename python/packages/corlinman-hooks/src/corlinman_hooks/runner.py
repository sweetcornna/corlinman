"""Hook runner — blocking, decision-returning, discoverable hooks.

Mirrors the claude-code / hermes hooks feature on two levels:

1. **Shell-command hooks** (the original surface). Operators register
   shell commands for specific events (``pre_tool``, ``post_tool``,
   ``notification``) in the agent config dict. When the event fires the
   command runs with the event payload on stdin as JSON. For blocking
   events (``pre_tool``), a non-zero exit code stops the tool call.

2. **File-discovered hooks** (``HOOK.yaml`` + ``handler.py``). A hooks
   directory (``~/.corlinman/hooks/<name>/`` style) can hold one folder
   per hook, each with a ``HOOK.yaml`` manifest naming the events it
   subscribes to and a ``handler.py`` exposing a callable. Discovered
   handlers run in-process and return a :class:`HookDecision` so they can
   allow / deny / mutate a tool call or veto a turn-end ``Stop``.

Configuration shape (shell hooks, from the agent config dict)::

    {
        "hooks": {
            "pre_tool": "path/to/hook.sh",
            "pre_read_file": "path/to/read-file-hook.sh",
            "post_tool": "path/to/after-hook.sh",
            "notification": "path/to/notify.sh"
        }
    }

Lookup order for ``pre_tool`` events:

1. Tool-specific key: ``pre_{tool_name}`` (e.g. ``pre_run_shell``).
2. Wildcard key: ``pre_tool``.

The first matching key wins. Missing keys are a silent no-op (allow-all
is the safe default).

Return-value contract (C3): the decision methods return a
:class:`HookDecision`. For backwards compatibility the decision unpacks
as a ``(allow, reason)`` 2-tuple so the existing call sites and tests
that wrote ``ok, msg = runner.run_pre_tool(...)`` keep working unchanged.

Thread-safety: ``HookRunner`` is designed for use from a single asyncio
event loop. Shell hooks run via :func:`asyncio.create_subprocess_shell`
when called from an async context (see :meth:`run_pre_tool_async`), or
with :mod:`subprocess` in the sync fallback used by tests and the
agent_servicer's ``_emit_pre_tool_dispatch`` path.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

__all__ = ["HookDecision", "HookRunner", "emit_collect"]

_log = logging.getLogger("corlinman.hooks.runner")

# Maximum time (seconds) a hook command is allowed to run before we
# forcibly kill it and treat the result as "allow" (so a broken hook
# never permanently bricks tool dispatch).
_HOOK_TIMEOUT: float = 5.0


@dataclass
class HookDecision:
    """Verdict returned by a decision-returning hook (C3 contract).

    Fields:

    * ``allow`` — ``True`` lets the action proceed, ``False`` blocks it.
    * ``reason`` — human-readable explanation (surfaced to the model /
      logged) when the hook blocks or wants to annotate an allow.
    * ``mutated_args`` — when not ``None``, replaces the tool's argument
      dict before dispatch (lets a hook rewrite a call rather than only
      veto it).
    * ``inject_message`` — when not ``None``, a message the loop should
      inject (e.g. a continuation prompt on a vetoed ``Stop``).
    * ``stop`` — request the loop to halt (turn-end hook semantics).

    The decision is **tuple-compatible**: ``allow, reason = decision``
    unpacks the first two fields, so legacy call sites that expected the
    old ``tuple[bool, str]`` return keep working. The ``reason`` slot in
    that unpacking is coerced to ``""`` when ``None`` for parity with the
    historical contract.
    """

    allow: bool = True
    reason: str | None = None
    mutated_args: dict[str, Any] | None = None
    inject_message: str | None = None
    stop: bool = False

    # -- tuple compatibility -------------------------------------------
    def __iter__(self):  # type: ignore[no-untyped-def]
        # Yields (allow, reason-as-str) so ``ok, msg = decision`` matches
        # the historical ``tuple[bool, str]`` return shape exactly.
        yield self.allow
        yield self.reason or ""

    def __getitem__(self, index: int) -> Any:
        return (self.allow, self.reason or "")[index]

    def __len__(self) -> int:
        return 2

    @classmethod
    def deny(cls, reason: str, *, stop: bool = False) -> HookDecision:
        return cls(allow=False, reason=reason, stop=stop)

    @classmethod
    def allow_all(cls) -> HookDecision:
        return cls(allow=True)


# A discovered in-process handler. It receives ``(event, payload)`` and
# returns either a :class:`HookDecision`, a plain ``bool`` (True=allow),
# ``None`` (abstain), or a dict the runner coerces into a decision.
_Handler = Callable[[str, "dict[str, Any]"], Any]


def _coerce_decision(value: Any) -> HookDecision | None:
    """Normalize a handler return into a :class:`HookDecision` or ``None``.

    ``None`` → abstain (``None``). ``bool`` → allow/deny. ``HookDecision``
    passes through. A dict is treated as ``HookDecision(**dict)`` for the
    recognized keys (unknown keys ignored).
    """
    if value is None:
        return None
    if isinstance(value, HookDecision):
        return value
    if isinstance(value, bool):
        return HookDecision(allow=value, reason=None if value else "denied by hook")
    if isinstance(value, dict):
        allow = bool(value.get("allow", True))
        return HookDecision(
            allow=allow,
            reason=value.get("reason"),
            mutated_args=value.get("mutated_args"),
            inject_message=value.get("inject_message"),
            stop=bool(value.get("stop", False)),
        )
    # Unknown shape → conservatively treat as abstain so a misbehaving
    # handler never silently blocks the agent.
    _log.warning("hook handler returned unrecognized value type: %r", type(value))
    return None


class HookRunner:
    """Runs shell-command + file-discovered hooks keyed by event name.

    Parameters
    ----------
    config:
        The agent-level config dict (or any sub-dict). ``hooks`` is
        extracted from ``config.get("hooks", {})``. An empty or missing
        mapping means all shell-hook events pass through.
    hooks_dir:
        Optional directory to discover ``HOOK.yaml`` + ``handler.py``
        hooks from. When provided (or set later via :meth:`discover`),
        each subfolder's manifest registers its handler against the
        events it names. Discovery failures are logged and skipped so a
        broken hook folder never bricks the runner.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        hooks_dir: str | Path | None = None,
        rule_matcher: Callable[[str, str, dict[str, Any]], bool] | None = None,
        prompt_evaluator: Callable[[str, dict[str, Any]], Any] | None = None,
        agent_evaluator: Callable[[str, dict[str, Any]], Any] | None = None,
        http_post: Callable[[str, dict[str, Any], float], tuple[int, str]] | None = None,
    ) -> None:
        # Wiring callables are kept so :meth:`reload` can rebuild the
        # declarative engine without the call site re-passing them.
        self._rule_matcher = rule_matcher
        self._prompt_evaluator = prompt_evaluator
        self._agent_evaluator = agent_evaluator
        self._http_post = http_post
        self._hooks_dir: Path | None = Path(hooks_dir) if hooks_dir is not None else None
        # event name -> list of in-process handlers discovered from disk.
        self._handlers: dict[str, list[_Handler]] = {}
        self._configure(config or {})
        if hooks_dir is not None:
            self.discover(hooks_dir)

    def _configure(self, config: dict[str, Any]) -> None:
        """(Re)build shell-hook + declarative state from a config dict.

        Shell hooks are string values only; the ``declarative`` sub-table
        (and scalar knobs like ``enabled``) must never be mistaken for a
        shell command.
        """
        from corlinman_hooks.declarative import DeclarativeEngine, parse_declarative

        raw = config.get("hooks", {})
        if not isinstance(raw, dict):
            raw = {}
        self._hooks = {k: str(v) for k, v in raw.items() if v and isinstance(v, str) and k != "declarative"}
        self._declarative = DeclarativeEngine(
            parse_declarative(raw.get("declarative")),
            rule_matcher=self._rule_matcher,
            prompt_evaluator=self._prompt_evaluator,
            agent_evaluator=self._agent_evaluator,
            http_post=self._http_post,
        )

    def reload(self, config: dict[str, Any] | None, hooks_dir: str | Path | None = None) -> dict[str, Any]:
        """Rebuild shell hooks, declarative groups, and discovered handlers.

        Called by ``/hooks reload`` and the config-watcher callback so a
        ``[hooks]`` edit takes effect without a restart (the runner was
        historically boot-time-only). Returns a summary dict for display.
        """
        self._configure(config or {})
        self._handlers = {}
        target_dir = Path(hooks_dir) if hooks_dir is not None else self._hooks_dir
        if target_dir is not None:
            self._hooks_dir = target_dir
            self.discover(target_dir)
        return {
            "shell_hooks": len(self._hooks),
            "declarative_groups": len(self.declarative_groups),
            "declarative_warnings": len(self._declarative.warnings),
            "discovered_handlers": sum(len(hs) for hs in self._handlers.values()),
        }

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @property
    def registered(self) -> dict[str, str]:
        """Return a copy of the registered shell-hook commands keyed by event.

        Useful for the ``GET /admin/hooks`` discovery endpoint.
        """
        return dict(self._hooks)

    @property
    def discovered_events(self) -> dict[str, int]:
        """Map of event name -> count of discovered in-process handlers."""
        return {ev: len(hs) for ev, hs in self._handlers.items()}

    @property
    def declarative_groups(self) -> list[dict[str, Any]]:
        """Serializable declarative matcher-group summary (admin/`/hooks`)."""
        return self._declarative.describe()

    @property
    def declarative_warnings(self) -> list[str]:
        """Parse warnings collected from the declarative config."""
        return self._declarative.warnings

    def supported_events(self) -> list[str]:
        """Return the canonical event names this runner understands.

        The list documents the hook protocol; registered commands are a
        subset. Lifecycle events are included so the discovery endpoint
        and ``HOOK.yaml`` validation can advertise them.
        """
        return [
            "pre_tool",
            "post_tool",
            "notification",
            "session_start",
            "session_end",
            "session_reset",
            "pre_compact",
            "post_compact",
            "stop",
            "user_prompt_submit",
        ]

    def register_handler(self, event: str, handler: _Handler) -> None:
        """Register an in-process ``handler`` against ``event``.

        Used by :meth:`discover` and available for programmatic
        registration (tests, plugin-contributed hooks).
        """
        self._handlers.setdefault(event, []).append(handler)

    def discover(self, hooks_dir: str | Path) -> int:
        """Discover ``HOOK.yaml`` + ``handler.py`` hooks under ``hooks_dir``.

        Layout (per claude-code / hermes convention)::

            <hooks_dir>/
                my-hook/
                    HOOK.yaml      # {events: [pre_tool, stop], handler: handler.py:run}
                    handler.py     # def run(event, payload) -> HookDecision | bool | None

        The manifest's ``events`` (list or comma string) names the events
        the handler subscribes to. ``handler`` is ``"<file>:<callable>"``
        (defaults to ``handler.py:run`` / ``handler.py:handle`` when
        omitted). Returns the number of (event, handler) pairs registered.

        Robust by design: a missing dir, an unparseable manifest, or an
        import error for one folder is logged and skipped — the rest of
        discovery proceeds. No third-party YAML dependency is required;
        :mod:`yaml` is used when importable, else a minimal built-in
        fallback parses the flat ``key: value`` manifest shape.
        """
        root = Path(hooks_dir)
        if not root.is_dir():
            return 0
        registered = 0
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            manifest_path = entry / "HOOK.yaml"
            if not manifest_path.is_file():
                manifest_path = entry / "HOOK.yml"
                if not manifest_path.is_file():
                    continue
            try:
                manifest = self._load_manifest(manifest_path)
            except Exception as exc:  # noqa: BLE001 — one bad hook must not break the rest
                _log.warning("hook.discover.manifest_error", extra={"dir": str(entry), "error": str(exc)})
                continue
            events = self._manifest_events(manifest)
            if not events:
                _log.info("hook.discover.no_events", extra={"dir": str(entry)})
                continue
            handler_ref = str(manifest.get("handler") or "handler.py:run")
            try:
                handler = self._load_handler(entry, handler_ref)
            except Exception as exc:  # noqa: BLE001
                _log.warning("hook.discover.handler_error", extra={"dir": str(entry), "error": str(exc)})
                continue
            if handler is None:
                continue
            for ev in events:
                self.register_handler(ev, handler)
                registered += 1
        if registered:
            _log.info("hook.discover.registered", extra={"dir": str(root), "count": registered})
        return registered

    @staticmethod
    def _load_manifest(path: Path) -> dict[str, Any]:
        """Parse a ``HOOK.yaml`` manifest into a dict.

        Uses :mod:`yaml` when available (optional dependency — guarded),
        falling back to a tiny flat ``key: value`` / ``key: [a, b]``
        parser otherwise so discovery works on a 1.9GB box that never
        installed PyYAML.
        """
        text = path.read_text(encoding="utf-8")
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            return HookRunner._parse_flat_yaml(text)
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise ValueError("HOOK.yaml top level must be a mapping")
        return data

    @staticmethod
    def _parse_flat_yaml(text: str) -> dict[str, Any]:
        """Minimal fallback parser for a flat ``HOOK.yaml`` (no PyYAML).

        Handles ``key: value`` and ``key: [a, b, c]`` (inline list). Lines
        starting with ``#`` and blank lines are ignored. Quotes around
        scalars are stripped. This is intentionally tiny — discovered
        manifests are expected to be simple flat maps.
        """
        out: dict[str, Any] = {}
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if value.startswith("[") and value.endswith("]"):
                inner = value[1:-1].strip()
                items = [v.strip().strip("'\"") for v in inner.split(",") if v.strip()]
                out[key] = items
            else:
                out[key] = value.strip("'\"")
        return out

    @staticmethod
    def _manifest_events(manifest: dict[str, Any]) -> list[str]:
        """Normalize the manifest's ``events`` (or ``event``) into a list."""
        raw = manifest.get("events")
        if raw is None:
            raw = manifest.get("event")
        if raw is None:
            return []
        if isinstance(raw, str):
            return [e.strip() for e in raw.split(",") if e.strip()]
        if isinstance(raw, (list, tuple)):
            return [str(e).strip() for e in raw if str(e).strip()]
        return []

    @staticmethod
    def _load_handler(hook_dir: Path, handler_ref: str) -> _Handler | None:
        """Import ``handler_ref`` (``file.py:callable``) from ``hook_dir``.

        Loads the module by file path (so it doesn't need to be on
        ``sys.path``) under a unique synthetic module name keyed by the
        hook folder. Returns the named callable, or the first of
        ``run`` / ``handle`` / ``main`` when no name is given.
        """
        file_part, _, attr = handler_ref.partition(":")
        file_part = file_part.strip() or "handler.py"
        handler_file = hook_dir / file_part
        if not handler_file.is_file():
            _log.warning("hook.discover.missing_handler_file", extra={"file": str(handler_file)})
            return None
        mod_name = f"_corlinman_hook_{hook_dir.name}_{handler_file.stem}"
        spec = importlib.util.spec_from_file_location(mod_name, handler_file)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if attr:
            fn = getattr(module, attr.strip(), None)
        else:
            fn = (
                getattr(module, "run", None)
                or getattr(module, "handle", None)
                or getattr(module, "main", None)
            )
        if fn is None or not callable(fn):
            _log.warning("hook.discover.handler_not_callable", extra={"ref": handler_ref})
            return None
        return cast(_Handler, fn)

    def _run_handlers(self, event: str, payload: dict[str, Any]) -> HookDecision:
        """Run every discovered in-process handler for ``event`` and fold
        their verdicts into a single :class:`HookDecision`.

        Semantics: the first explicit deny wins (and short-circuits). An
        allow with ``mutated_args`` / ``inject_message`` / ``stop`` is
        carried forward and merged. A handler that raises is isolated.
        Returns an allow-all decision when no handler objects.
        """
        handlers = self._handlers.get(event)
        if not handlers:
            return HookDecision.allow_all()
        result = HookDecision.allow_all()
        for handler in handlers:
            try:
                raw = handler(event, payload)
            except Exception as exc:  # noqa: BLE001 — isolate a broken handler
                _log.warning("hook.handler.error", extra={"event": event, "error": str(exc)})
                continue
            decision = _coerce_decision(raw)
            if decision is None:
                continue
            if not decision.allow:
                return decision  # first deny wins
            if decision.mutated_args is not None:
                result.mutated_args = decision.mutated_args
                if decision.mutated_args is not None:
                    payload = {**payload, "args": decision.mutated_args}
            if decision.inject_message is not None:
                result.inject_message = decision.inject_message
            if decision.stop:
                result.stop = True
            if decision.reason and not result.reason:
                result.reason = decision.reason
        return result

    @staticmethod
    def _merge_pre_tool_tiers(specific: HookDecision, generic: HookDecision) -> HookDecision:
        """Fold the specific (``pre_<tool>``) and generic (``pre_tool``)
        allow-path verdicts into a single :class:`HookDecision`.

        Both tiers are allow at this point (a deny short-circuits earlier).
        ``mutated_args`` / ``inject_message`` / ``stop`` / ``reason`` are
        carried forward in specific-then-generic order (last write wins for
        the mutate/inject slots, ``stop`` OR-folds, first ``reason`` sticks).
        Shared by both :meth:`run_pre_tool` and :meth:`run_pre_tool_async`
        so the sync and async paths never diverge.
        """
        merged = HookDecision.allow_all()
        for d in (specific, generic):
            if d.mutated_args is not None:
                merged.mutated_args = d.mutated_args
            if d.inject_message is not None:
                merged.inject_message = d.inject_message
            if d.stop:
                merged.stop = True
            if d.reason and not merged.reason:
                merged.reason = d.reason
        return merged

    # ------------------------------------------------------------------
    # Synchronous decision API (C3): used inside ``_emit_pre_tool_dispatch``
    # + tests. Returns a HookDecision (tuple-compatible for back-compat).
    # ------------------------------------------------------------------

    def run_pre_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        ctx: dict[str, Any] | None = None,
    ) -> HookDecision:
        """Run the pre-tool gate (shell hook + discovered handlers).

        Returns a :class:`HookDecision`. ``allow=False`` blocks the call;
        ``reason`` carries the hook's message. No matching hook → allow.
        Because :class:`HookDecision` unpacks as ``(allow, reason)``, the
        legacy ``ok, msg = runner.run_pre_tool(...)`` form still works.

        The shell hook receives a JSON payload on stdin::

            {"tool": "<name>", "args": {...}}

        A zero exit code = allow. Non-zero = block. Timeout (>5 s) is
        treated as allow so a stuck hook never bricks tool dispatch.

        In-process discovered ``pre_tool`` handlers run *after* the shell
        hook (only if it allowed) and can deny, mutate args, or annotate.
        Declarative hooks run last (legacy → discovered → declarative;
        first deny anywhere wins, mutations merge last-write-wins).
        """
        base = self._legacy_pre_tool(tool_name, args, ctx)
        if not base.allow or not self._declarative.has("pre_tool"):
            return base
        effective = base.mutated_args if base.mutated_args is not None else args
        decl = self._declarative.run_sync("pre_tool", tool_name, effective, ctx or {})
        if not decl.allow:
            return decl
        return self._merge_pre_tool_tiers(base, decl)

    def _legacy_pre_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        ctx: dict[str, Any] | None = None,
    ) -> HookDecision:
        cmd = self._hooks.get(f"pre_{tool_name}") or self._hooks.get("pre_tool")
        if cmd:
            payload = json.dumps({"tool": tool_name, "args": args}, ensure_ascii=False)
            try:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    input=payload,
                    capture_output=True,
                    text=True,
                    timeout=_HOOK_TIMEOUT,
                )
            except subprocess.TimeoutExpired:
                _log.warning("hook.pre_tool.timeout", extra={"tool": tool_name, "cmd": cmd})
            except Exception as exc:  # noqa: BLE001
                _log.warning("hook.pre_tool.error", extra={"tool": tool_name, "error": str(exc)})
            else:
                if result.returncode != 0:
                    msg = (result.stdout or result.stderr or "").strip()[:500]
                    _log.info(
                        "hook.pre_tool.blocked",
                        extra={"tool": tool_name, "returncode": result.returncode, "message": msg},
                    )
                    return HookDecision.deny(msg or "blocked by hook")
        # Discovered in-process handlers (event == "pre_tool").
        if self._handlers.get("pre_tool") or self._handlers.get(f"pre_{tool_name}"):
            handler_payload = {"tool": tool_name, "args": args, "ctx": ctx or {}}
            specific = self._run_handlers(f"pre_{tool_name}", handler_payload)
            if not specific.allow:
                return specific
            generic = self._run_handlers("pre_tool", handler_payload)
            if not generic.allow:
                return generic
            # Merge mutate/inject from both tiers (specific then generic).
            return self._merge_pre_tool_tiers(specific, generic)
        return HookDecision.allow_all()

    def run_stop(self, ctx: dict[str, Any] | None = None) -> HookDecision:
        """Run the turn-end ``Stop`` gate.

        Discovered ``stop`` handlers may veto the loop's exit (``allow=
        False``) and/or supply an ``inject_message`` continuation prompt
        (claude-code Stop-hook parity). A shell-command ``stop`` hook is
        also honored when configured: non-zero exit vetoes the stop and
        the hook's stdout becomes the ``inject_message``.

        Default (no hook) → allow (the loop stops normally). Declarative
        ``Stop`` groups run after the legacy paths (``command``/``http``
        kinds only in this sync form — see :meth:`run_stop_async`).
        """
        ctx = ctx or {}
        base = self._legacy_stop(ctx)
        if not base.allow or not self._declarative.has("stop"):
            return base
        decl = self._declarative.run_sync("stop", "", {}, ctx, extra=dict(ctx))
        if not decl.allow:
            return decl
        return self._merge_pre_tool_tiers(base, decl)

    async def run_stop_async(self, ctx: dict[str, Any] | None = None) -> HookDecision:
        """Async variant of :meth:`run_stop` (preferred in the loop).

        The legacy shell hook runs off-thread so it cannot stall the event
        loop, and declarative ``Stop`` groups get the full executor set —
        including ``prompt``/``agent`` kinds, which the sync form skips.
        """
        ctx = ctx or {}
        shell = await asyncio.to_thread(self._legacy_stop_shell, ctx)
        base = shell
        if base.allow and self._handlers.get("stop"):
            base = self._run_handlers("stop", ctx)
        if not base.allow or not self._declarative.has("stop"):
            return base
        decl = await self._declarative.run("stop", "", {}, ctx, extra=dict(ctx))
        if not decl.allow:
            return decl
        return self._merge_pre_tool_tiers(base, decl)

    def _legacy_stop(self, ctx: dict[str, Any]) -> HookDecision:
        shell = self._legacy_stop_shell(ctx)
        if not shell.allow:
            return shell
        if self._handlers.get("stop"):
            return self._run_handlers("stop", ctx)
        return HookDecision.allow_all()

    def _legacy_stop_shell(self, ctx: dict[str, Any]) -> HookDecision:
        cmd = self._hooks.get("stop")
        if cmd:
            payload = json.dumps(ctx, ensure_ascii=False)
            try:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    input=payload,
                    capture_output=True,
                    text=True,
                    timeout=_HOOK_TIMEOUT,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning("hook.stop.error", extra={"error": str(exc)})
            else:
                if result.returncode != 0:
                    msg = (result.stdout or result.stderr or "").strip()[:1000]
                    _log.info("hook.stop.veto", extra={"returncode": result.returncode})
                    return HookDecision(allow=False, reason="stop vetoed by hook", inject_message=msg or None)
        return HookDecision.allow_all()

    def run_post_tool(self, tool_name: str, args: dict[str, Any], result_json: str) -> None:
        """Run the ``post_{tool_name}`` or ``post_tool`` hook (fire-and-forget).

        The hook's exit code is ignored. Errors are logged and suppressed so
        a misbehaving post-hook never affects the agent.

        The hook process receives a JSON payload on stdin::

            {"tool": "<name>", "args": {...}, "result": "<json>"}
        """
        cmd = self._hooks.get(f"post_{tool_name}") or self._hooks.get("post_tool")
        if not cmd:
            return
        payload = json.dumps(
            {"tool": tool_name, "args": args, "result": result_json},
            ensure_ascii=False,
        )
        try:
            subprocess.run(
                cmd,
                shell=True,
                input=payload,
                capture_output=True,
                text=True,
                timeout=_HOOK_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("hook.post_tool.error", extra={"tool": tool_name, "error": str(exc)})

    async def run_post_tool_async(
        self,
        tool_name: str,
        args: dict[str, Any],
        result_json: str,
        ctx: dict[str, Any] | None = None,
    ) -> None:
        """Async, fire-and-forget post-tool hooks (legacy + declarative).

        Nothing here can block or fail the tool call by construction: the
        legacy shell hook is scheduled as a background task, and
        declarative ``PostToolUse`` hooks default to async. Verdicts are
        discarded. Await :meth:`drain` to flush (tests / shutdown).
        """
        cmd = self._hooks.get(f"post_{tool_name}") or self._hooks.get("post_tool")
        if cmd:
            payload = json.dumps(
                {"tool": tool_name, "args": args, "result": result_json},
                ensure_ascii=False,
            )
            self._declarative.track(self._shell_fire_and_forget(cmd, payload))
        # Discovered in-process post handlers (HOOK.yaml ``events:
        # [post_tool]``) — previously advertised but never invoked
        # (Codex #109). Verdicts are ignored per the post-tool contract;
        # ``_run_handlers`` isolates a raising handler.
        if self._handlers.get(f"post_{tool_name}") or self._handlers.get("post_tool"):
            handler_payload = {
                "tool": tool_name,
                "args": args,
                "result": result_json,
                "ctx": ctx or {},
            }
            self._run_handlers(f"post_{tool_name}", handler_payload)
            self._run_handlers("post_tool", handler_payload)
        if self._declarative.has("post_tool"):
            await self._declarative.run(
                "post_tool", tool_name, args, ctx, extra={"tool_result": result_json}
            )

    async def run_event_async(
        self,
        event: str,
        payload: dict[str, Any] | None = None,
        ctx: dict[str, Any] | None = None,
    ) -> HookDecision:
        """Generic lifecycle-event entry (``session_*``, ``pre_compact``,
        ``post_compact``, ``user_prompt_submit``, ``notification``).

        Runs, in order: discovered in-process handlers (which historically
        never fired for these events), an optional legacy shell hook keyed
        by the exact event name (fire-and-forget), and declarative groups.
        The folded decision is returned for the caller to interpret —
        lifecycle call sites typically treat a deny/inject as advisory
        (e.g. a ``user_prompt_submit`` block becomes a system note), not
        as an abort.
        """
        payload = dict(payload or {})
        base = self._run_handlers(event, payload)
        if not base.allow:
            return base
        cmd = self._hooks.get(event)
        if cmd and event not in ("pre_tool", "post_tool", "stop"):
            body = json.dumps({"event": event, **payload}, ensure_ascii=False, default=str)
            self._declarative.track(self._shell_fire_and_forget(cmd, body))
        if self._declarative.has(event):
            decl = await self._declarative.run(
                event, str(payload.get("tool_name") or ""), {}, ctx, extra=payload
            )
            if not decl.allow:
                return decl
            return self._merge_pre_tool_tiers(base, decl)
        return base

    async def _shell_fire_and_forget(self, cmd: str, payload: str) -> None:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.communicate(payload.encode()), timeout=_HOOK_TIMEOUT)
        except TimeoutError:
            proc.kill()
            await proc.wait()

    async def drain(self) -> None:
        """Await all scheduled fire-and-forget hook tasks."""
        await self._declarative.drain()

    def run_notification(
        self,
        event_or_payload: str | dict[str, Any],
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Run the ``notification`` hook (fire-and-forget).

        Two call shapes are accepted:

        * ``run_notification(event, payload)`` — C3 contract form: the
          ``event`` string is folded into the payload under ``"event"``.
        * ``run_notification(payload)`` — legacy form: the single dict is
          sent verbatim.

        The hook receives the resolved payload dict as JSON on stdin.
        Exit code and output are ignored.
        """
        if isinstance(event_or_payload, str):
            data: dict[str, Any] = dict(payload or {})
            data.setdefault("event", event_or_payload)
        else:
            data = event_or_payload or {}
        cmd = self._hooks.get("notification")
        if not cmd:
            return
        try:
            subprocess.run(
                cmd,
                shell=True,
                input=json.dumps(data, ensure_ascii=False),
                capture_output=True,
                text=True,
                timeout=_HOOK_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("hook.notification.error", extra={"error": str(exc)})

    # ------------------------------------------------------------------
    # Async API (preferred inside async tool dispatch).
    # ------------------------------------------------------------------

    async def run_pre_tool_async(
        self,
        tool_name: str,
        args: dict[str, Any],
        ctx: dict[str, Any] | None = None,
    ) -> HookDecision:
        """Async variant of :meth:`run_pre_tool`.

        Runs the shell hook subprocess via
        :func:`asyncio.create_subprocess_shell` so the event loop is not
        blocked. Discovered in-process handlers are then run (they are
        synchronous callables), then declarative hooks with the full
        executor set. Returns a tuple-compatible :class:`HookDecision`.
        """
        base = await self._legacy_pre_tool_async(tool_name, args, ctx)
        if not base.allow or not self._declarative.has("pre_tool"):
            return base
        effective = base.mutated_args if base.mutated_args is not None else args
        decl = await self._declarative.run("pre_tool", tool_name, effective, ctx or {})
        if not decl.allow:
            return decl
        return self._merge_pre_tool_tiers(base, decl)

    async def _legacy_pre_tool_async(
        self,
        tool_name: str,
        args: dict[str, Any],
        ctx: dict[str, Any] | None = None,
    ) -> HookDecision:
        cmd = self._hooks.get(f"pre_{tool_name}") or self._hooks.get("pre_tool")
        if cmd:
            payload = json.dumps({"tool": tool_name, "args": args}, ensure_ascii=False)
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout_b, stderr_b = await asyncio.wait_for(
                        proc.communicate(payload.encode()),
                        timeout=_HOOK_TIMEOUT,
                    )
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
                    _log.warning("hook.pre_tool.timeout", extra={"tool": tool_name, "cmd": cmd})
                    stdout_b = stderr_b = b""
                    proc_rc = 0
                else:
                    proc_rc = proc.returncode or 0
            except Exception as exc:  # noqa: BLE001
                _log.warning("hook.pre_tool.error", extra={"tool": tool_name, "error": str(exc)})
            else:
                if proc_rc != 0:
                    msg = (stdout_b or stderr_b or b"").decode("utf-8", "replace").strip()[:500]
                    _log.info(
                        "hook.pre_tool.blocked",
                        extra={"tool": tool_name, "returncode": proc_rc, "message": msg},
                    )
                    return HookDecision.deny(msg or "blocked by hook")
        # In-process handlers are synchronous — reuse the sync fold so the
        # async path carries the specific tier's mutated_args/inject_message
        # exactly like ``run_pre_tool`` does (BUG-03).
        if self._handlers.get("pre_tool") or self._handlers.get(f"pre_{tool_name}"):
            handler_payload = {"tool": tool_name, "args": args, "ctx": ctx or {}}
            specific = self._run_handlers(f"pre_{tool_name}", handler_payload)
            if not specific.allow:
                return specific
            generic = self._run_handlers("pre_tool", handler_payload)
            if not generic.allow:
                return generic
            # Merge mutate/inject from both tiers (specific then generic).
            return self._merge_pre_tool_tiers(specific, generic)
        return HookDecision.allow_all()


def emit_collect(event: str, payload: dict[str, Any]) -> list[Any]:
    """Module-level collecting emit (C3 contract).

    Runs every registered process-global handler for ``event`` and
    returns the list of non-``None`` verdicts they produced. This is the
    decision counterpart to the fire-and-forget bus ``emit`` — call sites
    that don't hold a :class:`~corlinman_hooks.bus.HookBus` reference but
    still need a collecting fan-out (e.g. a lifecycle veto point) use this.

    Handlers are registered via :func:`register_global_handler`. When no
    handler objects, an empty list is returned (the safe abstain default,
    so every call site no-ops cleanly when nothing is wired).
    """
    handlers = _GLOBAL_HANDLERS.get(event, [])
    collected: list[Any] = []
    for handler in list(handlers):
        try:
            result = handler(event, payload)
        except Exception as exc:  # noqa: BLE001 — isolate a broken handler
            _log.warning("hook.emit_collect.error", extra={"event": event, "error": str(exc)})
            continue
        if result is not None:
            collected.append(result)
    return collected


# Process-global handler registry backing :func:`emit_collect`. Kept
# separate from a per-runner ``HookRunner._handlers`` so a call site that
# only has the module function (no runner instance in scope) can still
# participate in the decision path.
_GLOBAL_HANDLERS: dict[str, list[_Handler]] = {}


def register_global_handler(event: str, handler: _Handler) -> None:
    """Register a process-global handler for :func:`emit_collect`."""
    _GLOBAL_HANDLERS.setdefault(event, []).append(handler)


def clear_global_handlers() -> None:
    """Drop all process-global handlers (test isolation helper)."""
    _GLOBAL_HANDLERS.clear()
