"""The call-time authorization gate (audit W3-1 — replaces the frozen gate).

``PermissionGate`` froze ``_rules`` / ``_strict`` / ``_mode`` /
``_last_match_wins`` in ``__init__`` and was constructed exactly once per
process (``agent_servicer.__init__``), so NO configuration change — not the
``[permissions]`` block, not ``[agent_runtime].strict_mode`` (fact M5), not
an edited settings file — could take effect without a restart.

:class:`AuthzGate` re-reads every input on each ``resolve``:

* the ``[permissions]`` config layer via
  :mod:`corlinman_agent.authz.defaults` (a generation counter makes the
  common "nothing changed" case a tuple compare, not a re-parse);
* the ``CORLINMAN_AGENT_*`` env layer;
* the two settings files (mtime-cached);
* ``[agent_runtime].strict_mode`` via the deduplicated
  :func:`~corlinman_agent.authz.defaults.resolve_strict` chain.

Layer stacking (low → high): user file, project file, env, config — with
last-match-wins (decision C3) so a higher layer's matching rule overrides a
lower one. The compiled rule set is cached and rebuilt only when the
composed cache key changes, so the hot path costs two ``os.stat`` calls
plus a tuple comparison.

Evaluation order (§1.3 of the plan) is inherited verbatim from
``PermissionGate.resolve_with_args`` — bypass short-circuit, tool aliasing,
rule scan with the task-control rescue, then mode / strict / default — with
ONE addition: an ``ask`` verdict consults the
:class:`~corlinman_agent.authz.grants.GrantStore` first (step 5), so an
approved-and-remembered call stops re-prompting while a ``deny`` rule can
never be bypassed by a grant (the rule scan already returned before the
grant lookup runs).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, cast

import structlog

from corlinman_agent.authz.defaults import (
    generation as _defaults_generation,
)
from corlinman_agent.authz.defaults import (
    get_permissions_defaults,
    resolve_default_action,
    resolve_external_tools_enforced,
    resolve_last_match_wins,
    resolve_mode,
    resolve_strict,
)
from corlinman_agent.authz.grants import GrantStore, get_grant_store
from corlinman_agent.authz.model import ALLOW, ASK, PermissionMode, Subject

logger = structlog.get_logger(__name__)

__all__ = ["AuthzGate"]

#: Sidecar re-check throttle (risk R2): a long turn must pick up a rule the
#: operator just saved without paying an ``os.stat`` on every single call.
_SIDECAR_THROTTLE_S = 0.1

_FileSig = tuple[int, int] | None
_FileBlock = tuple[list[dict[str, Any]], str | None, bool | None]


class AuthzGate:
    """Call-time permission evaluator + grant-aware ``ask`` resolution.

    Duck-type compatible with the legacy ``PermissionGate`` surface the
    servicer and the approval gate consume (``resolve_with_args`` /
    ``resolve`` / ``decide_with_context`` / ``audit_log_entry`` /
    ``set_mode`` / ``mode`` / ``strict`` / ``rules``).
    """

    def __init__(
        self,
        *,
        data_dir: Path | str | None = None,
        project_dir: Path | str | None = None,
        grant_store: GrantStore | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._project_dir = project_dir
        self._grants = grant_store if grant_store is not None else get_grant_store(data_dir)
        #: Runtime mode override (console ``/permissions``, ``exit_plan_mode``).
        #: ``None`` = resolve from config/env/files. An interactive switch
        #: outranks the static layers until the process restarts — the same
        #: contract ``PermissionGate.set_mode`` had.
        self._mode_override: PermissionMode | None = None
        # Compiled-snapshot cache.
        self._snapshot_key: tuple[Any, ...] | None = None
        self._snapshot_gate: Any = None
        self._env_rules_cache: tuple[str, list[Any]] | None = None
        self._cfg_rules_cache: tuple[int, list[Any]] | None = None
        self._file_cache: dict[Path, tuple[_FileSig, _FileBlock]] = {}
        # Sidecar mid-turn refresh state (risk R2).
        self._sidecar_checked_at = 0.0
        self._sidecar_mtime: int | None = None

    # -- properties (legacy surface) -------------------------------------

    @property
    def grant_store(self) -> GrantStore:
        return self._grants

    @property
    def mode(self) -> PermissionMode:
        return cast("PermissionMode", self._compiled().mode)

    @property
    def strict(self) -> bool:
        return bool(self._compiled().strict)

    @property
    def rules(self) -> tuple[Any, ...]:
        return cast("tuple[Any, ...]", self._compiled().rules)

    def set_mode(self, mode: Any) -> PermissionMode:
        """Swap the runtime mode; takes effect on the next ``resolve``.

        ALSO invalidates every session grant (§1.3): explicit ``ask`` rules
        resolve before the mode override, so a grant cached under the old
        mode would otherwise bypass ``/plan`` entirely (Codex #104).
        """
        self._mode_override = PermissionMode.coerce(mode)
        self._grants.clear_session_grants()
        return self._mode_override

    # -- resolution -------------------------------------------------------

    def decide(self, tool: str) -> str:
        return self.decide_with_context(tool)

    def decide_with_context(
        self,
        tool: str,
        *,
        model: str | None = None,
        session_key: str | None = None,
        user_id: str | None = None,
    ) -> str:
        ctx = Subject(model=model, session_key=session_key, user_id=user_id)
        action, _ = self.resolve(tool, ctx)
        return action

    def resolve(self, tool: str, ctx: Subject) -> tuple[str, int | None]:
        return self.resolve_with_args(tool, ctx, None)

    def resolve_with_args(
        self,
        tool: str,
        ctx: Subject,
        args: dict[str, Any] | None,
    ) -> tuple[str, int | None]:
        """Args-aware decision — the one entry point every caller uses.

        Delegates the rule/mode/strict evaluation to the compiled snapshot
        (identical semantics to the legacy gate, including the
        task-control rescue), then applies grant memory to ``ask``.
        """
        self._maybe_refresh_from_sidecar()
        snapshot = self._compiled()
        action, rule_index = snapshot.resolve_with_args(tool, ctx, args)
        if action == ASK and self._grants.is_granted(ctx, tool, args):
            # Step 5 of the evaluation order: a remembered approval turns
            # ``ask`` into ``allow``. Deny rules returned above already —
            # a grant can never widen past them.
            return ALLOW, rule_index
        return action, rule_index

    def resolve_external(
        self,
        keys: tuple[str, ...] | list[str],
        ctx: Subject,
        args: dict[str, Any] | None = None,
    ) -> tuple[str, int | None]:
        """EP2: decide an EXTERNAL (plugin/MCP/voice/sampling) tool call.

        ``keys`` are the canonical candidate keys (C7) — see
        :func:`~corlinman_agent.authz.matcher.external_candidate_keys`; the
        first entry is the stable grant/audit key. Honours the
        ``[permissions].external_tools_enforced = false`` escape hatch
        (risk R4): when the operator opts out, every external call resolves
        ``allow`` with no rule index — exactly the pre-W3-2 behaviour.
        """
        self._maybe_refresh_from_sidecar()
        if not resolve_external_tools_enforced():
            return ALLOW, None
        snapshot = self._compiled()
        action, rule_index = snapshot.resolve_external_with_args(
            tuple(keys), ctx, args
        )
        if (
            action == ASK
            and keys
            and self._grants.is_granted(ctx, keys[0], args)
        ):
            return ALLOW, rule_index
        return action, rule_index

    def audit_log_entry(
        self,
        tool: str,
        ctx: Subject,
        decision: str,
        *,
        rule_index: int | None = None,
    ) -> dict[str, Any]:
        entry = cast(
            "dict[str, Any]",
            self._compiled().audit_log_entry(
                tool, ctx, decision, rule_index=rule_index
            ),
        )
        tenant = getattr(ctx, "tenant_id", None)
        surface = getattr(ctx, "surface", None)
        parent_surface = getattr(ctx, "parent_surface", None)
        if tenant:
            entry["tenant_id"] = tenant
        if surface:
            entry["surface"] = surface
        if parent_surface:
            # Subagent calls: the child resolves under surface="subagent"
            # while the originating (parent) surface rides along for audit.
            entry["parent_surface"] = parent_surface
        return entry

    # -- snapshot compilation ---------------------------------------------

    def _compiled(self) -> Any:
        """The current compiled ``PermissionGate`` snapshot (cached)."""
        # Local import: ``permission`` re-exports this package's model /
        # matcher, so a module-level import would be circular.
        from corlinman_agent.permission import (  # noqa: PLC0415
            PermissionGate,
            warn_on_last_match_flip,
        )
        from corlinman_agent.permission_settings import (  # noqa: PLC0415
            project_settings_path,
            user_settings_path,
        )

        user_path = user_settings_path(self._data_dir)
        proj_path = project_settings_path(self._project_dir)
        user_sig, (user_rules, user_mode, user_strict) = self._read_file(user_path)
        proj_sig, (proj_rules, proj_mode, proj_strict) = self._read_file(proj_path)

        env_rules_raw = os.environ.get("CORLINMAN_AGENT_PERMISSIONS", "")
        gen = _defaults_generation()
        strict = resolve_strict(proj_strict, user_strict)
        mode = self._mode_override or PermissionMode.coerce(
            resolve_mode(proj_mode, user_mode)
        )
        last_match_wins = resolve_last_match_wins()
        default_action = resolve_default_action()

        key = (
            gen,
            env_rules_raw,
            user_sig,
            proj_sig,
            id(user_rules),
            id(proj_rules),
            strict,
            mode,
            last_match_wins,
            default_action,
        )
        if key == self._snapshot_key and self._snapshot_gate is not None:
            return self._snapshot_gate

        rules: list[Any] = []
        rules.extend(self._parse_dict_rules_cached("user", user_rules))
        rules.extend(self._parse_dict_rules_cached("proj", proj_rules))
        rules.extend(self._parse_env_rules(env_rules_raw))
        rules.extend(self._parse_cfg_rules(gen))

        # C3 migration aid: an env-only deployment whose overlapping rules
        # now evaluate last-match-wins gets a WARN with the verdict diff.
        env_only = bool(env_rules_raw.strip()) and not (
            user_rules or proj_rules or get_permissions_defaults().rules
        )
        lmw_env_set = bool(
            os.environ.get("CORLINMAN_AGENT_PERMISSION_LAST_MATCH_WINS", "").strip()
        )
        if env_only and not lmw_env_set and last_match_wins:
            warn_on_last_match_flip(rules)

        try:
            snapshot = PermissionGate(
                rules,
                default_action=default_action,
                strict=strict,
                mode=mode,
                last_match_wins=last_match_wins,
            )
        except ValueError:
            # A bad configured default_action must not take dispatch down;
            # parsing already validated it, but stay fail-safe.
            snapshot = PermissionGate(
                rules, strict=strict, mode=mode, last_match_wins=last_match_wins
            )
        self._snapshot_key = key
        self._snapshot_gate = snapshot
        return snapshot

    def _read_file(self, path: Path) -> tuple[_FileSig, _FileBlock]:
        from corlinman_agent.permission_settings import (  # noqa: PLC0415
            _read_permissions_block,
        )

        try:
            st = path.stat()
            sig: _FileSig = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        cached = self._file_cache.get(path)
        if cached is not None and cached[0] == sig:
            return sig, cached[1]
        block: _FileBlock = ([], None, None) if sig is None else _read_permissions_block(path)
        self._file_cache[path] = (sig, block)
        return sig, block

    def _parse_dict_rules_cached(self, _label: str, raw: list[dict[str, Any]]) -> list[Any]:
        from corlinman_agent.authz.matcher import parse_rule_list  # noqa: PLC0415

        if not raw:
            return []
        # ``raw`` comes out of the mtime-keyed file cache, so identity is a
        # valid parse-cache key; re-parsing on every miss is fine (rare).
        return parse_rule_list(json.dumps(raw))

    def _parse_env_rules(self, raw: str) -> list[Any]:
        from corlinman_agent.authz.matcher import parse_rule_list  # noqa: PLC0415

        if not raw.strip():
            return []
        cached = self._env_rules_cache
        if cached is not None and cached[0] == raw:
            return cached[1]
        parsed = parse_rule_list(raw)
        self._env_rules_cache = (raw, parsed)
        return parsed

    def _parse_cfg_rules(self, gen: int) -> list[Any]:
        from corlinman_agent.authz.matcher import parse_rule_list  # noqa: PLC0415

        cached = self._cfg_rules_cache
        if cached is not None and cached[0] == gen:
            return cached[1]
        raw = get_permissions_defaults().rules
        parsed = parse_rule_list(json.dumps(list(raw))) if raw else []
        self._cfg_rules_cache = (gen, parsed)
        return parsed

    # -- mid-turn sidecar refresh (risk R2) -------------------------------

    def _maybe_refresh_from_sidecar(self) -> None:
        """Pick up a freshly-written ``py-config.json`` mid-turn.

        The normal sidecar apply runs on model resolution (next-turn
        granularity). For PERMISSION rules that is not enough: a deny the
        operator just saved must stop the remaining tool calls of a turn
        already in flight. Throttled ``os.stat`` (100ms); applies ONLY the
        ``permissions`` block — the other blocks keep their established
        next-turn cadence. Never raises.
        """
        path = os.environ.get("CORLINMAN_PY_CONFIG", "").strip()
        if not path:
            return
        now = time.monotonic()
        if now - self._sidecar_checked_at < _SIDECAR_THROTTLE_S:
            return
        self._sidecar_checked_at = now
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            return
        if mtime == self._sidecar_mtime:
            return
        self._sidecar_mtime = mtime
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — never break dispatch
            logger.warning("agent.authz.sidecar_refresh_failed", error=str(exc))
            return
        if not isinstance(data, dict):
            return
        block = data.get("permissions")
        try:
            from corlinman_agent.authz.defaults import (  # noqa: PLC0415
                apply_permissions_config,
            )

            apply_permissions_config(block if isinstance(block, dict) else None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent.authz.sidecar_apply_failed", error=str(exc))
