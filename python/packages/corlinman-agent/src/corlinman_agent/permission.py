"""Declarative permission gate for builtin tool calls (legacy facade).

W3-1 (unified authorization): the matching core and the domain objects
moved to :mod:`corlinman_agent.authz` — this module re-exports them under
their historical names so every existing import keeps working, and keeps
:class:`PermissionGate` as the **compiled snapshot evaluator** the
call-time :class:`corlinman_agent.authz.gate.AuthzGate` rebuilds whenever
any config layer changes. New code should prefer ``corlinman_agent.authz``.

Config sources (in precedence order, decision C5):

1. the ``[permissions]`` config block (via the py-config sidecar);
2. **``$CORLINMAN_AGENT_PERMISSIONS``** — JSON list of rules. Each rule::

       {
           "tool":   "<name>" | "*" | "<name>(<arg glob>)",
           "action": "allow" | "deny" | "ask" | "log",
           "scope":  {                              # OPTIONAL
               "model":   "<fnmatch glob>",
               "session": "<fnmatch glob>",
               "user":    "<fnmatch glob>",
               "tenant":  "<fnmatch glob>",
               "surface": "qq|telegram"             # exact alternation
           }
       }

   ``match`` (with ``session_pattern`` / ``user_pattern``) remains the
   accepted legacy alias for ``scope``.
3. the project / user settings files (``permission_settings``).

Rule evaluation is **last-match-wins by default** (decision C3 — layered
precedence requires it; ``CORLINMAN_AGENT_PERMISSION_LAST_MATCH_WINS=0``
restores the historical first-match order explicitly).
"""

from __future__ import annotations

import os
from typing import Any

import structlog

from corlinman_agent import runtime_defaults as _limits
from corlinman_agent.authz.matcher import (
    _EDIT_TOOLS,
    _PERMISSION_TOOL_ALIAS,
    _TASK_CONTROL_TOOLS,
    MUTATING_TOOLS,
    PermissionRule,
    RuleMatch,
    extract_arg_candidates,
    extract_primary_arg,
    match_hook_rule,
    parse_rule_list,
    tool_pattern_matches,
)
from corlinman_agent.authz.model import (
    _VALID_ACTIONS,
    ALLOW,
    ASK,
    DENY,
    LOG,
    PermissionMode,
    Subject,
)

logger = structlog.get_logger(__name__)

#: Historical name — the caller context is now the authz ``Subject`` (which
#: adds ``tenant_id`` / ``surface`` / ``parent_surface`` on top of the
#: legacy model / session_key / user_id triple).
PermissionContext = Subject


class PermissionGate:
    """Decides whether a builtin tool call should run.

    Rules are checked **last-match-wins by default** (C3): with layered
    rule sources stacked least- to most-specific, the last matching rule —
    i.e. the most specific layer's — decides. Pass
    ``last_match_wins=False`` for the historical first-match order. If
    nothing matches:

    - in strict mode, ``MUTATING_TOOLS`` default to ``deny`` and the
      rest default to ``allow``;
    - otherwise the gate's ``default_action`` (constructor arg) decides.

    NOTE (W3-1): this class evaluates a FROZEN configuration snapshot. The
    production gate is :class:`corlinman_agent.authz.gate.AuthzGate`, which
    rebuilds one of these whenever any config layer changes — do not wire a
    long-lived ``PermissionGate`` directly into a servicer unless the
    configuration is intentionally pinned (tests).
    """

    __slots__ = ("_default", "_last_match_wins", "_mode", "_rules", "_strict")

    def __init__(
        self,
        rules: list[PermissionRule] | None = None,
        *,
        default_action: str = ALLOW,
        strict: bool = False,
        mode: PermissionMode | str = PermissionMode.DEFAULT,
        last_match_wins: bool = True,
    ) -> None:
        if default_action not in _VALID_ACTIONS:
            raise ValueError(
                f"invalid default_action {default_action!r}; "
                f"expected one of {_VALID_ACTIONS}"
            )
        self._rules: tuple[PermissionRule, ...] = tuple(rules or [])
        self._default = default_action
        self._strict = strict
        self._mode = PermissionMode.coerce(mode)
        # C3: ``last_match_wins`` now defaults to True everywhere — layered
        # rule sources stack later (more specific) layers AFTER earlier
        # ones, and the layer precedence contract (config > env > project >
        # user) only holds if a later layer's matching rule overrides an
        # earlier one's.
        self._last_match_wins = last_match_wins

    @property
    def rules(self) -> tuple[PermissionRule, ...]:
        return self._rules

    @property
    def strict(self) -> bool:
        return self._strict

    @property
    def mode(self) -> PermissionMode:
        return self._mode

    def set_mode(self, mode: PermissionMode | str) -> PermissionMode:
        """Swap the operating mode at runtime (normalizing via
        :meth:`PermissionMode.coerce`) and return the resolved mode. The gate
        re-reads ``_mode`` on every ``resolve``, so the change takes effect on
        the next tool call. Used by the console ``/permissions`` command."""
        self._mode = PermissionMode.coerce(mode)
        return self._mode

    def _mode_override(self, tool: str) -> str | None:
        """Return a mode-driven action for ``tool``, or ``None`` to fall
        through to the rule list / default. Consulted only when no explicit
        rule matched."""
        if self._mode is PermissionMode.BYPASS:
            return ALLOW
        if self._mode is PermissionMode.ACCEPT_EDITS and tool in _EDIT_TOOLS:
            return ALLOW
        if self._mode is PermissionMode.PLAN and tool in MUTATING_TOOLS:
            return DENY
        return None

    def decide(self, tool: str) -> str:
        """Return ``"allow" | "deny" | "log" | "ask"`` for ``tool``.

        Legacy shim — delegates to :meth:`decide_with_context` with an
        empty context. Rules carrying ``match`` filters never fire here
        unless their filters are all empty (i.e. legacy-equivalent).
        """
        return self.decide_with_context(tool)

    def decide_with_context(
        self,
        tool: str,
        *,
        model: str | None = None,
        session_key: str | None = None,
        user_id: str | None = None,
    ) -> str:
        """Context-aware decision; last matching rule wins (or first when
        ``last_match_wins`` is unset)."""
        ctx = PermissionContext(
            model=model, session_key=session_key, user_id=user_id
        )
        action, _ = self.resolve(tool, ctx)
        return action

    def resolve(
        self,
        tool: str,
        ctx: PermissionContext,
    ) -> tuple[str, int | None]:
        """Same decision as :meth:`decide_with_context` but also returns
        the index of the matched rule (or ``None`` when the default /
        mode / strict-mode fallback fired). Used by :meth:`audit_log_entry`.

        Args-unaware: arg-scoped rules only match here when their pattern is
        the catch-all ``"*"`` (see :meth:`PermissionRule.applies_to`)."""
        return self.resolve_with_args(tool, ctx, None)

    def _can_start_shell_tasks(self, ctx: PermissionContext) -> bool:
        """Would run_shell resolve to *allow* for at least one command here?

        Backs the task-control rescue: a session that may start SOME shell
        task may poll/kill its OWN tasks. Mirrors :meth:`resolve_with_args`'
        ordering (rules beat mode/strict/default) but treats ANY run_shell
        allow/log rule as sufficient while IGNORING arg scope — a scoped
        ``run_shell(npm:*)`` allow proves the session can start that command's
        task, so it counts. A scoped ``run_shell(rm:*)`` DENY only blocks that
        one command and is skipped; an *unscoped* run_shell/``*`` deny is
        decisive. The registry's session-ownership gate — not this predicate —
        is what confines control to the caller's own tasks.
        """
        if self._mode is PermissionMode.BYPASS:
            return True
        rules = (
            reversed(self._rules) if self._last_match_wins else iter(self._rules)
        )
        for rule in rules:
            if not tool_pattern_matches(rule.tool, "run_shell"):
                continue
            if (
                rule.match is not None
                and not rule.match.is_empty()
                and not rule.match.matches(ctx)
            ):
                continue
            scoped = rule.arg_pattern is not None and rule.arg_pattern != "*"
            if rule.action in (ALLOW, LOG, ASK):
                # A scoped OR unscoped allow proves at least one command runs.
                # ``ask`` counts too: an approved run_shell(run_in_background)
                # starts and returns a task_id, so the approval must carry
                # through to the poll/kill of that task (Codex #112 r8) — the
                # control surface is rescued to ``allow`` (not re-prompted) so
                # the model can manage the task the user just approved.
                return True
            # action == DENY: unscoped blocks all shell; scoped blocks only
            # that one command, so keep scanning for a surviving allow.
            if not scoped:
                return False
        # No decisive rule — run_shell (a mutating tool) settles on mode /
        # strict / default.
        if self._mode is PermissionMode.PLAN or self._strict:
            return False
        return self._default == ALLOW

    def resolve_with_args(
        self,
        tool: str,
        ctx: PermissionContext,
        args: dict[str, Any] | None,
    ) -> tuple[str, int | None]:
        """Args-aware decision honouring per-argument / command patterns.

        The primary argument value is extracted via
        :func:`extract_arg_candidates` and matched against each rule's
        ``arg_pattern`` (fnmatch). Match order respects
        ``last_match_wins``. ``BYPASS`` mode short-circuits to ``allow``
        before any rule; ``acceptEdits`` / ``plan`` apply only when no rule
        matched.
        """
        # BYPASS wins over everything — operator opted out of gating.
        if self._mode is PermissionMode.BYPASS:
            return ALLOW, None

        # The task-control surface (poll/kill of the session's OWN tasks)
        # tracks the *grant to start* tasks, not run_shell's per-command
        # scoping — see ``_TASK_CONTROL_TOOLS``. Remember the original identity
        # so a catch-all denial can be rescued once the normal resolution
        # (below) has run under run_shell's alias.
        is_task_control = tool in _TASK_CONTROL_TOOLS

        # A shell-task tool inherits run_shell's full verdict (rules + mode
        # + strict): the whole decision below runs under run_shell's
        # identity so the poll/kill surface tracks the run_shell grant.
        tool = _PERMISSION_TOOL_ALIAS.get(tool, tool)

        arg_value = extract_arg_candidates(tool, args)
        matched: tuple[str, int] | None = None
        for idx, rule in enumerate(self._rules):
            if rule.applies_to_args(tool, ctx, arg_value):
                matched = (rule.action, idx)
                if not self._last_match_wins:
                    break
        if matched is not None:
            action, matched_idx = matched
            # A control tool denied ONLY by a wildcard (``*``) rule is the
            # catch-all-swallow case — not an operator deny that names the
            # tool. Rescue it iff the session can start tasks at all.
            if (
                is_task_control
                and action == DENY
                and self._rules[matched_idx].tool == "*"
                and self._can_start_shell_tasks(ctx)
            ):
                return ALLOW, None
            return matched

        # No explicit rule — consult the operating mode, then strict-mode,
        # then the gate default. A control tool denied by any of these three
        # defaults is rescued when the session can start tasks (e.g. a
        # scoped-only ``run_shell(npm:*)`` grant the argless call couldn't
        # match, or a ``default=deny`` gate).
        mode_action = self._mode_override(tool)
        if mode_action is not None:
            if (
                is_task_control
                and mode_action == DENY
                and self._can_start_shell_tasks(ctx)
            ):
                return ALLOW, None
            return mode_action, None
        if self._strict and tool in MUTATING_TOOLS:
            if is_task_control and self._can_start_shell_tasks(ctx):
                return ALLOW, None
            return DENY, None
        if (
            is_task_control
            and self._default == DENY
            and self._can_start_shell_tasks(ctx)
        ):
            return ALLOW, None
        return self._default, None

    def resolve_external_with_args(
        self,
        keys: tuple[str, ...] | list[str],
        ctx: PermissionContext,
        args: dict[str, Any] | None,
    ) -> tuple[str, int | None]:
        """EP2 decision for an EXTERNAL (plugin/MCP/voice/sampling) tool.

        ``keys`` are the canonical candidate keys of the call (see
        :func:`corlinman_agent.authz.matcher.external_candidate_keys`); a
        rule fires when its ``tool`` pattern matches ANY of them — including
        the ``"*"`` wildcard, which since W3-2 (decision C4) really does
        cover everything. Differences from the builtin path, on purpose:

        * no tool aliasing / task-control rescue (builtin-only machinery);
        * ``plan`` mode denies every external tool when no rule matched —
          an external tool's blast radius is unknowable, so planning mode
          must not run it (C4: modes now apply to external tools);
        * ``acceptEdits`` / ``strict`` don't special-case external tools
          (both are defined over the builtin edit/mutating sets).
        """
        if self._mode is PermissionMode.BYPASS:
            return ALLOW, None
        primary = keys[0] if keys else ""
        arg_value = extract_arg_candidates(primary, args)
        matched: tuple[str, int] | None = None
        for idx, rule in enumerate(self._rules):
            if any(rule.applies_to_args(key, ctx, arg_value) for key in keys):
                matched = (rule.action, idx)
                if not self._last_match_wins:
                    break
        if matched is not None:
            return matched
        if self._mode is PermissionMode.PLAN:
            return DENY, None
        return self._default, None

    def audit_log_entry(
        self,
        tool: str,
        ctx: PermissionContext,
        decision: str,
        *,
        rule_index: int | None = None,
    ) -> dict[str, Any]:
        """Return a structured dict describing the decision for logging.

        Callers can either pass the ``rule_index`` they already resolved
        (cheap) or omit it and we'll re-resolve here. The returned dict
        is JSON-safe and contains: ``tool``, ``decision``, ``model``,
        ``session_key``, ``user_id``, ``rule_index``, ``strict``, ``mode``
        (plus the matched rule's ``note`` when one is set).
        """
        if rule_index is None:
            _, rule_index = self.resolve(tool, ctx)
        entry: dict[str, Any] = {
            "tool": tool,
            "decision": decision,
            "model": ctx.model,
            "session_key": ctx.session_key,
            "user_id": ctx.user_id,
            "rule_index": rule_index,
            "strict": self._strict,
            "mode": self._mode.value,
        }
        if rule_index is not None and 0 <= rule_index < len(self._rules):
            note = self._rules[rule_index].note
            if note:
                entry["note"] = note
        return entry

    @classmethod
    def from_env(cls) -> PermissionGate:
        """Build a gate from environment configuration.

        - ``CORLINMAN_AGENT_PERMISSIONS`` — JSON list of rules.
        - ``CORLINMAN_AGENT_STRICT_MODE`` — truthy enables strict mode.
        - ``CORLINMAN_AGENT_PERMISSION_MODE`` — one of
          ``default``/``acceptEdits``/``plan``/``bypass``.
        - ``CORLINMAN_AGENT_PERMISSION_LAST_MATCH_WINS`` — explicit override
          of the rule-evaluation order. **Default is now last-match-wins**
          (C3); set the var to ``0``/``false`` to restore first-match.

        Malformed JSON or invalid actions log a warning and degrade to
        the default (allow-all) — never raises into agent boot.
        """
        rules = parse_rule_list(os.environ.get("CORLINMAN_AGENT_PERMISSIONS", ""))
        # ``[permissions].strict`` / ``[agent_runtime].strict_mode`` outrank
        # the env var (the deduplicated resolve_strict chain).
        strict = _limits.strict_mode_enabled()
        mode = PermissionMode.coerce(
            os.environ.get("CORLINMAN_AGENT_PERMISSION_MODE", "")
        )
        lmw_raw = (
            os.environ.get("CORLINMAN_AGENT_PERMISSION_LAST_MATCH_WINS", "")
            .strip()
            .lower()
        )
        if lmw_raw:
            last_match_wins = lmw_raw in ("1", "true", "yes", "on")
        else:
            # C3: flipped default. Warn (with the verdict diff) when the
            # flip actually changes an env-only deployment's behaviour.
            last_match_wins = True
            warn_on_last_match_flip(rules)
        return cls(
            rules, strict=strict, mode=mode, last_match_wins=last_match_wins
        )

    @classmethod
    def from_layered_sources(
        cls,
        *layers: Any,
        strict: bool = False,
        mode: PermissionMode | str = PermissionMode.DEFAULT,
        last_match_wins: bool = True,
    ) -> PermissionGate:
        """Build a gate by STACKING rule layers (gap layered rule sources).

        ``layers`` is an ordered sequence of rule sources, each a JSON
        string OR an already-parsed ``list`` of rule dicts. Earlier layers
        are less specific (e.g. global defaults); later layers (e.g. a
        per-project / per-session overlay) stack AFTER them. With the
        default ``last_match_wins=True`` a later layer's matching rule
        overrides an earlier one — the standard "project beats global"
        precedence. Each layer is parsed tolerantly; a bad layer is skipped.
        """
        import json as _json  # noqa: PLC0415 — tiny helper, avoid module dep

        rules: list[PermissionRule] = []
        for layer in layers:
            if layer is None:
                continue
            if isinstance(layer, str):
                rules.extend(parse_rule_list(layer))
            elif isinstance(layer, list):
                rules.extend(parse_rule_list(_json.dumps(layer)))
        return cls(
            rules, strict=strict, mode=mode, last_match_wins=last_match_wins
        )


#: Fingerprints already warned about — once per rule set per process, so a
#: gate rebuilt on every config generation doesn't spam the log.
_FLIP_WARNED: set[tuple[tuple[str, str, str | None], ...]] = set()


def warn_on_last_match_flip(rules: list[PermissionRule]) -> None:
    """C3 migration WARN: log the verdict diff caused by the order flip.

    Called for env-only deployments (no explicit LAST_MATCH_WINS setting,
    no config/file layer). Compares each rule-named tool's args-unaware
    verdict under first-match vs last-match and logs one WARN with the
    per-tool diff when any changed. Cheap (only runs over the declared
    tools) and memoized per rule-set fingerprint.
    """
    if len(rules) < 2:
        return
    fingerprint = tuple((r.tool, r.action, r.arg_pattern) for r in rules)
    if fingerprint in _FLIP_WARNED:
        return
    _FLIP_WARNED.add(fingerprint)
    tools = {r.tool for r in rules if r.tool and r.tool != "*"}
    if not tools:
        return
    first = PermissionGate(list(rules), last_match_wins=False)
    last = PermissionGate(list(rules), last_match_wins=True)
    diff: dict[str, str] = {}
    for tool in sorted(tools):
        before = first.decide(tool)
        after = last.decide(tool)
        if before != after:
            diff[tool] = f"{before} -> {after}"
    if diff:
        logger.warning(
            "agent.permission.last_match_wins_flipped",
            detail=(
                "rule evaluation is now last-match-wins by default (C3); "
                "these env-configured verdicts changed — set "
                "CORLINMAN_AGENT_PERMISSION_LAST_MATCH_WINS=0 to restore "
                "the old order, or reorder the rules"
            ),
            diff=diff,
        )


__all__ = [
    "ALLOW",
    "ASK",
    "DENY",
    "LOG",
    "MUTATING_TOOLS",
    "PermissionContext",
    "PermissionGate",
    "PermissionMode",
    "PermissionRule",
    "RuleMatch",
    "extract_arg_candidates",
    "extract_primary_arg",
    "match_hook_rule",
    "parse_rule_list",
    "warn_on_last_match_flip",
]
