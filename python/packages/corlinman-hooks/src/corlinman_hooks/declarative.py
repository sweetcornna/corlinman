"""Declarative hooks — claude-code-style settings-driven hook definitions.

Layered over :class:`~corlinman_hooks.runner.HookRunner` (Dim 9 parity,
ADAPT-ADOPT): operators describe hooks in the config file instead of
writing code. Shape (TOML; JSON config maps identically)::

    [[hooks.declarative.PreToolUse]]
    matcher = "run_shell"                  # tool-name pattern: exact | A|B | prefix*
    if = "run_shell(git push*)"            # optional permission-rule refinement
    hooks = [
      { kind = "command", command = "./guard.sh", timeout = 10 },
      { kind = "http", url = "http://127.0.0.1:9911/hook" },
    ]

Event names accept both the claude-code spelling (``PreToolUse``) and the
runner's snake_case (``pre_tool``). Unknown events or malformed hook defs
are collected as warnings — configuration mistakes must never brick the
agent, so parsing is total and the engine fails open everywhere except an
explicit block verdict.

Four executor kinds:

* ``command`` — shell subprocess, JSON payload on stdin. Exit-code table
  (declarative hooks only; the legacy flat ``[hooks]`` keys keep their
  historical 0=allow / non-zero=deny contract):
  ``0`` = allow (stdout may carry a JSON verdict, and an explicit
  ``{"decision": "block"}`` wins over the exit code); ``2`` = block with
  stderr (fallback stdout) as the reason; any other code = non-blocking
  error, logged, fail-open; timeout = kill, fail-open.
* ``http`` — POST the JSON payload; a 2xx JSON body
  ``{"decision": "allow"|"block", reason?, mutated_args?, inject_message?}``
  is the verdict; anything else fails open.
* ``prompt`` / ``agent`` — delegated to evaluator callables injected at
  wiring time (an LLM judge / a verifier subagent). Unwired kinds log
  once and fail open, so a config written for the gateway also loads in
  a context that cannot evaluate prompts.

Dependency rule: this package must not import ``corlinman-agent`` (the
agent depends on us), so the ``if`` permission-rule grammar arrives as an
injected ``rule_matcher(rule, tool, args) -> bool`` callable. Groups with
an ``if`` clause are skipped (non-matching) when no matcher is wired.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import subprocess
import urllib.request
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from corlinman_hooks.runner import HookDecision, _coerce_decision

__all__ = [
    "DeclarativeConfig",
    "DeclarativeEngine",
    "HookDef",
    "MatcherGroup",
    "match_tool",
    "parse_declarative",
]

_log = logging.getLogger("corlinman.hooks.declarative")

_KINDS = ("command", "http", "prompt", "agent")

# Canonical event key by normalized (lowercase, alnum-only) config name.
# Both the claude-code spelling and the runner's snake_case normalize in.
_EVENT_ALIASES: dict[str, str] = {
    "pretooluse": "pre_tool",
    "pretool": "pre_tool",
    "posttooluse": "post_tool",
    "posttool": "post_tool",
    "stop": "stop",
    "userpromptsubmit": "user_prompt_submit",
    "sessionstart": "session_start",
    "sessionend": "session_end",
    "sessionreset": "session_reset",
    "precompact": "pre_compact",
    "postcompact": "post_compact",
    "notification": "notification",
    "filechanged": "file_changed",
    "setup": "setup",
}

# Whether hooks on an event default to fire-and-forget. Blocking-capable
# events (pre_tool / stop / pre_compact) default to awaited-sync so their
# verdicts count; pure observation events default to async so they can
# never add latency. A hook's explicit ``async`` key overrides.
_EVENT_DEFAULT_ASYNC: dict[str, bool] = {
    "pre_tool": False,
    "stop": False,
    "pre_compact": False,
    "post_tool": True,
    "post_compact": True,
    "user_prompt_submit": True,
    "session_start": True,
    "session_end": True,
    "session_reset": True,
    "notification": True,
    "file_changed": True,
    "setup": True,
}

# Per-kind default timeout (seconds). LLM-backed kinds need more headroom
# than a shell guard; all are operator-overridable per hook.
_KIND_DEFAULT_TIMEOUT: dict[str, float] = {
    "command": 5.0,
    "http": 5.0,
    "prompt": 30.0,
    "agent": 60.0,
}

# Injected executor callables. ``rule_matcher`` evaluates a permission-rule
# string against (tool, args); the evaluators judge a prompt/instruction
# against the event payload and return a ``{"ok": bool, "reason": str}``
# verdict dict (awaitable or plain).
RuleMatcher = Callable[[str, str, dict], bool]
Evaluator = Callable[[str, dict], Any]
HttpPost = Callable[[str, dict, float], tuple[int, str]]


@dataclass
class HookDef:
    kind: str
    command: str | None = None
    url: str | None = None
    prompt: str | None = None
    instructions: str | None = None
    timeout: float = 5.0
    fire_async: bool = False


@dataclass
class MatcherGroup:
    event: str
    matcher: str = "*"
    if_rule: str | None = None
    hooks: list[HookDef] = field(default_factory=list)


@dataclass
class DeclarativeConfig:
    groups: dict[str, list[MatcherGroup]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def canonical_event(name: str) -> str | None:
    """Normalize a config/user event spelling to the runner's snake_case key.

    Accepts claude-code names (``PreToolUse``) and snake_case
    (``pre_tool``); returns ``None`` for unknown events. Public — the
    ``/hooks test`` console command canonicalizes user input with this.
    """
    normalized = "".join(ch for ch in str(name).lower() if ch.isalnum())
    return _EVENT_ALIASES.get(normalized)


# Backwards-compatible private alias (internal call sites predate the rename).
_canonical_event = canonical_event


def match_tool(pattern: str, tool: str) -> bool:
    """Match a tool name against a matcher pattern.

    ``""`` / ``"*"`` match everything (including tool-less events, which
    pass ``tool=""``). Otherwise the pattern is ``|``-separated
    alternatives, each an exact name or a ``prefix*`` glob. Case-sensitive.
    """
    pattern = (pattern or "").strip()
    if pattern in ("", "*"):
        return True
    for alt in pattern.split("|"):
        alt = alt.strip()
        if not alt:
            continue
        if alt == "*":
            return True
        if alt.endswith("*"):
            if tool.startswith(alt[:-1]):
                return True
        elif tool == alt:
            return True
    return False


def _parse_hook_def(raw: Any, event: str, warnings: list[str]) -> HookDef | None:
    if not isinstance(raw, dict):
        warnings.append(f"{event}: hook def must be a table, got {type(raw).__name__}")
        return None
    kind = str(raw.get("kind") or raw.get("type") or "").strip().lower()
    if kind not in _KINDS:
        warnings.append(f"{event}: unknown hook kind {kind or raw!r}")
        return None
    hook = HookDef(kind=kind)
    hook.command = str(raw["command"]) if raw.get("command") else None
    hook.url = str(raw["url"]) if raw.get("url") else None
    hook.prompt = str(raw["prompt"]) if raw.get("prompt") else None
    hook.instructions = str(raw.get("instructions") or raw.get("prompt") or "") or None
    required = {"command": hook.command, "http": hook.url, "prompt": hook.prompt, "agent": hook.instructions}
    field_name = {"command": "command", "http": "url", "prompt": "prompt", "agent": "instructions"}[kind]
    if not required[kind]:
        warnings.append(f"{event}: {kind} hook missing '{field_name}' field")
        return None
    try:
        hook.timeout = float(raw.get("timeout", _KIND_DEFAULT_TIMEOUT[kind]))
    except (TypeError, ValueError):
        warnings.append(f"{event}: invalid timeout {raw.get('timeout')!r}, using default")
        hook.timeout = _KIND_DEFAULT_TIMEOUT[kind]
    if "async" in raw:
        hook.fire_async = bool(raw["async"])
    else:
        hook.fire_async = _EVENT_DEFAULT_ASYNC.get(event, True)
    return hook


def parse_declarative(section: Any) -> DeclarativeConfig:
    """Parse the ``hooks.declarative`` config sub-table.

    Total function: every malformed shape becomes a warning string (for
    boot logs + ``/hooks``), never an exception. Groups whose hooks all
    fail validation are dropped.
    """
    cfg = DeclarativeConfig()
    if section is None:
        return cfg
    if not isinstance(section, dict):
        cfg.warnings.append(f"hooks.declarative must be a table, got {type(section).__name__}")
        return cfg
    for raw_event, raw_groups in section.items():
        event = _canonical_event(raw_event)
        if event is None:
            cfg.warnings.append(f"unknown hook event {raw_event!r} (ignored)")
            continue
        if not isinstance(raw_groups, (list, tuple)):
            cfg.warnings.append(f"{raw_event}: expected a list of matcher groups")
            continue
        for raw_group in raw_groups:
            if not isinstance(raw_group, dict):
                cfg.warnings.append(f"{raw_event}: matcher group must be a table")
                continue
            group = MatcherGroup(
                event=event,
                matcher=str(raw_group.get("matcher", "*") or "*"),
                if_rule=str(raw_group["if"]) if raw_group.get("if") else None,
            )
            raw_hooks = raw_group.get("hooks")
            if not isinstance(raw_hooks, (list, tuple)) or not raw_hooks:
                cfg.warnings.append(f"{raw_event}: matcher group has no hooks")
                continue
            for raw_hook in raw_hooks:
                hook = _parse_hook_def(raw_hook, event, cfg.warnings)
                if hook is not None:
                    group.hooks.append(hook)
            if group.hooks:
                cfg.groups.setdefault(event, []).append(group)
    return cfg


def _verdict_to_decision(value: Any) -> HookDecision | None:
    """Normalize an executor verdict into a :class:`HookDecision`.

    Accepts the evaluator shape (``{"ok": bool, "reason": ...}``), the
    wire shape (``{"decision": "allow"|"block", ...}``), and everything
    :func:`~corlinman_hooks.runner._coerce_decision` already handles.
    """
    if isinstance(value, dict):
        if "decision" in value:
            allow = str(value.get("decision", "allow")).strip().lower() != "block"
            return HookDecision(
                allow=allow,
                reason=value.get("reason"),
                mutated_args=value.get("mutated_args"),
                inject_message=value.get("inject_message"),
                stop=bool(value.get("stop", False)),
            )
        if "ok" in value:
            ok = bool(value["ok"])
            return HookDecision(
                allow=ok,
                reason=value.get("reason"),
                mutated_args=value.get("mutated_args"),
                inject_message=value.get("inject_message"),
            )
    return _coerce_decision(value)


def _default_http_post(url: str, body: dict, timeout: float) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return int(response.status), response.read().decode("utf-8", "replace")


class DeclarativeEngine:
    """Evaluates declarative matcher groups into a folded verdict.

    Fold semantics mirror ``HookRunner._run_handlers``: groups run in
    config order, hooks in listed order, the first explicit deny
    short-circuits, and allow-path mutations merge last-write-wins (a
    later hook sees the earlier hook's ``mutated_args`` as its input).
    Hooks marked async are scheduled fire-and-forget; their verdicts are
    ignored by construction so they can never block or slow the caller.
    """

    def __init__(
        self,
        config: DeclarativeConfig,
        *,
        rule_matcher: RuleMatcher | None = None,
        prompt_evaluator: Evaluator | None = None,
        agent_evaluator: Evaluator | None = None,
        http_post: HttpPost | None = None,
    ) -> None:
        self._config = config
        self._rule_matcher = rule_matcher
        self._prompt_evaluator = prompt_evaluator
        self._agent_evaluator = agent_evaluator
        self._http_post = http_post or _default_http_post
        self._pending: set[asyncio.Task[Any]] = set()
        self._warned_unwired: set[str] = set()

    # -- introspection --------------------------------------------------

    @property
    def warnings(self) -> list[str]:
        return list(self._config.warnings)

    def has(self, event: str) -> bool:
        return bool(self._config.groups.get(event))

    def describe(self) -> list[dict[str, Any]]:
        """Serializable summary for ``/hooks`` and ``GET /admin/hooks``."""
        out: list[dict[str, Any]] = []
        for event, groups in self._config.groups.items():
            for group in groups:
                out.append(
                    {
                        "event": event,
                        "matcher": group.matcher,
                        "if": group.if_rule,
                        "kinds": [h.kind for h in group.hooks],
                        "async": [h.fire_async for h in group.hooks],
                    }
                )
        return out

    # -- task tracking ---------------------------------------------------

    def track(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Schedule ``coro`` fire-and-forget, holding a strong reference."""

        async def swallow() -> None:
            try:
                await coro
            except Exception as exc:  # noqa: BLE001 — async hooks are isolated
                _log.warning("hook.declarative.async_error", extra={"error": str(exc)})

        task = asyncio.get_running_loop().create_task(swallow())
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def drain(self) -> None:
        """Await all scheduled fire-and-forget hooks (test/shutdown helper)."""
        while self._pending:
            tasks = list(self._pending)
            await asyncio.gather(*tasks, return_exceptions=True)
            self._pending.difference_update(tasks)

    # -- evaluation ------------------------------------------------------

    @staticmethod
    def _payload(
        event: str,
        tool: str,
        args: dict[str, Any],
        ctx: dict[str, Any] | None,
        extra: dict[str, Any] | None,
    ) -> dict[str, Any]:
        ctx = ctx or {}
        payload: dict[str, Any] = {
            "event": event,
            "tool_name": tool,
            "tool_input": args,
            "session_key": str(ctx.get("session_key", "") or ""),
            "tenant_id": ctx.get("tenant_id"),
            "user_id": ctx.get("user_id"),
        }
        if extra:
            payload.update(extra)
        return payload

    def _group_matches(self, group: MatcherGroup, tool: str, args: dict[str, Any]) -> bool:
        """Evaluate one group's ``matcher`` + ``if`` rule against a call.

        Called per group INSIDE the fold loop, against the args as
        mutated so far — an earlier hook's ``mutated_args`` must be what
        a later group's ``if`` rule gates on, or a rewrite could bypass
        the configured policy (Codex #109).
        """
        if not match_tool(group.matcher, tool):
            return False
        if group.if_rule is not None:
            if self._rule_matcher is None:
                self._warn_once(
                    f"if:{group.event}", "hook 'if' rule set but no rule matcher wired; group skipped"
                )
                return False
            try:
                return bool(self._rule_matcher(group.if_rule, tool, args))
            except Exception as exc:  # noqa: BLE001 — a bad rule must not brick dispatch
                _log.warning(
                    "hook.declarative.if_rule_error",
                    extra={"rule": group.if_rule, "error": str(exc)},
                )
                return False
        return True

    def _warn_once(self, key: str, message: str) -> None:
        if key not in self._warned_unwired:
            self._warned_unwired.add(key)
            _log.warning("hook.declarative.%s", message)

    async def run(
        self,
        event: str,
        tool: str,
        args: dict[str, Any],
        ctx: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> HookDecision:
        """Run every matching declarative hook for ``event`` and fold."""
        result = HookDecision.allow_all()
        current_args = args
        for group in self._config.groups.get(event, []):
            if not self._group_matches(group, tool, current_args):
                continue
            for hook in group.hooks:
                payload = self._payload(event, tool, current_args, ctx, extra)
                if hook.fire_async:
                    self.track(self._execute(hook, payload))
                    continue
                decision = await self._execute(hook, payload)
                if decision is None:
                    continue
                if not decision.allow:
                    return decision
                result, current_args = self._merge(result, decision, current_args)
        return result

    def run_sync(
        self,
        event: str,
        tool: str,
        args: dict[str, Any],
        ctx: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> HookDecision:
        """Synchronous fold — ``command``/``http`` only.

        ``prompt``/``agent`` kinds need an event loop and are skipped
        (fail-open, logged once). Async-designated hooks still execute —
        synchronously, verdict ignored — so their side effects are kept.
        """
        result = HookDecision.allow_all()
        current_args = args
        for group in self._config.groups.get(event, []):
            if not self._group_matches(group, tool, current_args):
                continue
            for hook in group.hooks:
                if hook.kind in ("prompt", "agent"):
                    self._warn_once(f"sync:{hook.kind}", f"{hook.kind} hook skipped in sync context")
                    continue
                payload = self._payload(event, tool, current_args, ctx, extra)
                decision = self._execute_sync(hook, payload)
                if hook.fire_async or decision is None:
                    continue
                if not decision.allow:
                    return decision
                result, current_args = self._merge(result, decision, current_args)
        return result

    @staticmethod
    def _merge(
        result: HookDecision, decision: HookDecision, current_args: dict[str, Any]
    ) -> tuple[HookDecision, dict[str, Any]]:
        if decision.mutated_args is not None:
            result.mutated_args = decision.mutated_args
            current_args = decision.mutated_args
        if decision.inject_message is not None:
            result.inject_message = decision.inject_message
        if decision.stop:
            result.stop = True
        if decision.reason and not result.reason:
            result.reason = decision.reason
        return result, current_args

    # -- executors ---------------------------------------------------------

    async def _execute(self, hook: HookDef, payload: dict[str, Any]) -> HookDecision | None:
        if hook.kind == "command":
            return await self._exec_command(hook, payload)
        if hook.kind == "http":
            return await self._exec_http(hook, payload)
        if hook.kind == "prompt":
            return await self._exec_evaluator(self._prompt_evaluator, hook.prompt or "", hook, payload)
        if hook.kind == "agent":
            return await self._exec_evaluator(self._agent_evaluator, hook.instructions or "", hook, payload)
        return None

    def _execute_sync(self, hook: HookDef, payload: dict[str, Any]) -> HookDecision | None:
        if hook.kind == "command":
            return self._exec_command_sync(hook, payload)
        if hook.kind == "http":
            return self._exec_http_verdict(hook, payload)
        return None

    @staticmethod
    def _command_decision(rc: int, stdout: str, stderr: str, command: str) -> HookDecision | None:
        if rc == 0:
            body = stdout.strip()
            if body.startswith("{"):
                try:
                    return _verdict_to_decision(json.loads(body))
                except (json.JSONDecodeError, ValueError):
                    _log.debug("hook.declarative.command_stdout_not_json", extra={"cmd": command})
            return None
        if rc == 2:
            message = (stderr or stdout or "").strip()[:500]
            return HookDecision.deny(message or "blocked by hook (exit 2)")
        _log.warning(
            "hook.declarative.command_error_exit",
            extra={"cmd": command, "returncode": rc, "stderr": stderr.strip()[:200]},
        )
        return None

    async def _exec_command(self, hook: HookDef, payload: dict[str, Any]) -> HookDecision | None:
        command = hook.command or ""
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(json.dumps(payload, ensure_ascii=False).encode()),
                    timeout=hook.timeout,
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                _log.warning("hook.declarative.command_timeout", extra={"cmd": command})
                return None
        except Exception as exc:  # noqa: BLE001 — a broken hook never bricks dispatch
            _log.warning("hook.declarative.command_error", extra={"cmd": command, "error": str(exc)})
            return None
        return self._command_decision(
            proc.returncode or 0,
            stdout_b.decode("utf-8", "replace"),
            stderr_b.decode("utf-8", "replace"),
            command,
        )

    def _exec_command_sync(self, hook: HookDef, payload: dict[str, Any]) -> HookDecision | None:
        command = hook.command or ""
        try:
            result = subprocess.run(
                command,
                shell=True,
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True,
                text=True,
                timeout=hook.timeout,
            )
        except subprocess.TimeoutExpired:
            _log.warning("hook.declarative.command_timeout", extra={"cmd": command})
            return None
        except Exception as exc:  # noqa: BLE001
            _log.warning("hook.declarative.command_error", extra={"cmd": command, "error": str(exc)})
            return None
        return self._command_decision(result.returncode, result.stdout or "", result.stderr or "", command)

    def _exec_http_verdict(self, hook: HookDef, payload: dict[str, Any]) -> HookDecision | None:
        url = hook.url or ""
        try:
            status, body = self._http_post(url, payload, hook.timeout)
        except Exception as exc:  # noqa: BLE001 — network failure fails open
            _log.warning("hook.declarative.http_error", extra={"url": url, "error": str(exc)})
            return None
        if not 200 <= status < 300:
            _log.warning("hook.declarative.http_status", extra={"url": url, "status": status})
            return None
        try:
            return _verdict_to_decision(json.loads(body))
        except (json.JSONDecodeError, ValueError):
            _log.warning("hook.declarative.http_bad_body", extra={"url": url})
            return None

    async def _exec_http(self, hook: HookDef, payload: dict[str, Any]) -> HookDecision | None:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._exec_http_verdict, hook, payload),
                timeout=hook.timeout + 1.0,
            )
        except TimeoutError:
            _log.warning("hook.declarative.http_timeout", extra={"url": hook.url})
            return None

    async def _exec_evaluator(
        self,
        evaluator: Evaluator | None,
        instruction: str,
        hook: HookDef,
        payload: dict[str, Any],
    ) -> HookDecision | None:
        if evaluator is None:
            self._warn_once(f"unwired:{hook.kind}", f"{hook.kind} hook configured but no evaluator wired; failing open")
            return None
        try:
            verdict = evaluator(instruction, payload)
            if inspect.isawaitable(verdict):
                verdict = await asyncio.wait_for(verdict, timeout=hook.timeout)
        except TimeoutError:
            _log.warning("hook.declarative.evaluator_timeout", extra={"kind": hook.kind})
            return None
        except Exception as exc:  # noqa: BLE001 — evaluator failure fails open
            _log.warning("hook.declarative.evaluator_error", extra={"kind": hook.kind, "error": str(exc)})
            return None
        return _verdict_to_decision(verdict)
