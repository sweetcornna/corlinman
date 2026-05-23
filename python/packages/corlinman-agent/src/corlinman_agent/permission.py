"""Declarative permission gate for builtin tool calls.

A small, opinionated layer between the model's tool call and the actual
dispatch. Mirrors the opencode / Claude Code allow/deny/log idea, scoped
to what is actually useful for a chat-bot deployment:

- a per-tool action: ``allow`` / ``deny`` / ``log``
- a wildcard rule (``*``) for "everything else"
- a "strict mode" preset that flips every mutating tool to ``deny``
  unless an explicit ``allow`` rule overrides it
- **context-aware matching** — a rule may optionally narrow itself to a
  particular model, channel session, or end-user by attaching a
  ``match`` block (all sub-fields must match for the rule to fire).

The gate is read at servicer construction (from env or an explicit rule
list) and consulted in ``_dispatch_builtin`` before any builtin tool
runs. A ``deny`` short-circuits with a ``permission_denied`` envelope
the model can read and react to.

Config sources (in precedence order):

1. **``$CORLINMAN_AGENT_PERMISSIONS``** — JSON list of rules; the first
   match wins. Each rule has shape::

       {
           "tool":   "<name>" | "*",
           "action": "allow" | "deny" | "log",
           "match":  {                              # OPTIONAL
               "model":            "<fnmatch glob>",
               "session_pattern":  "<fnmatch glob>",
               "user_pattern":     "<fnmatch glob>"
           }
       }

   When ``match`` is omitted the rule behaves like a legacy tool-only
   rule (matches every context). When ``match`` is present, **every**
   declared sub-field must match the caller's context via
   :func:`fnmatch.fnmatchcase` for the rule to apply. A missing context
   value (e.g. ``user_id=None``) never matches a non-empty pattern.

   Example — deny ``run_shell`` only when both the model is a Claude
   variant and the user_id is a guest::

       [
           {
               "tool":   "run_shell",
               "action": "deny",
               "match":  {"model": "claude-*", "user_pattern": "guest*"}
           }
       ]

2. **``$CORLINMAN_AGENT_STRICT_MODE``** = ``1`` — denies every mutating
   tool (write_file, edit_file, apply_patch, run_shell, revert_changes)
   unless rule (1) explicitly allows it.
3. otherwise the default is ``allow`` for every tool.
"""

from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass
from typing import Any

ALLOW: str = "allow"
DENY: str = "deny"
LOG: str = "log"
_VALID_ACTIONS = (ALLOW, DENY, LOG)

#: The "mutating" tools — strict mode flips these to ``deny`` by default.
#: Read-only tools (read/list/search/web/calc/todo/subagent/blackboard)
#: stay allowed in strict mode because they have no blast radius on
#: their own.
MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "write_file",
        "edit_file",
        "apply_patch",
        "run_shell",
        "revert_changes",
    }
)


@dataclass(frozen=True)
class RuleMatch:
    """Optional context filters on a :class:`PermissionRule`.

    All non-empty fields must match the caller's context via
    :func:`fnmatch.fnmatchcase` for the parent rule to fire. An empty
    field is treated as "don't care" (matches anything, including a
    missing context value).
    """

    model: str | None = None
    session_pattern: str | None = None
    user_pattern: str | None = None

    def is_empty(self) -> bool:
        """True when no filter is declared — the rule matches any context."""
        return (
            self.model is None
            and self.session_pattern is None
            and self.user_pattern is None
        )

    def matches(self, ctx: PermissionContext) -> bool:
        """Return True if ``ctx`` satisfies every declared filter.

        A missing context value (``None`` / empty string) is treated as
        a non-match against any non-empty pattern — we don't want a rule
        keyed on ``user_pattern="admin*"`` to fire on anonymous calls.
        """
        if self.model is not None:
            if not ctx.model or not fnmatch.fnmatchcase(ctx.model, self.model):
                return False
        if self.session_pattern is not None:
            if not ctx.session_key or not fnmatch.fnmatchcase(
                ctx.session_key, self.session_pattern
            ):
                return False
        if self.user_pattern is not None:
            if not ctx.user_id or not fnmatch.fnmatchcase(
                ctx.user_id, self.user_pattern
            ):
                return False
        return True


@dataclass(frozen=True)
class PermissionContext:
    """Caller context passed to :meth:`PermissionGate.decide_with_context`.

    All fields are optional — a tool-only rule (no ``match`` block) will
    match a fully-empty context. Channel-aware rules need
    ``session_key`` / ``user_id`` populated from the chat ``start``
    frame and its ``binding`` (the QQ/Telegram sender).
    """

    model: str | None = None
    session_key: str | None = None
    user_id: str | None = None


@dataclass(frozen=True)
class PermissionRule:
    """One rule: a tool name (or ``"*"``) and the action to take.

    Optional ``match`` narrows the rule to a particular model / session
    / user. When ``match`` is :data:`None` the rule fires for any
    context (legacy behaviour).
    """

    tool: str
    action: str
    match: RuleMatch | None = None

    def __post_init__(self) -> None:
        if self.action not in _VALID_ACTIONS:
            raise ValueError(
                f"invalid permission action {self.action!r}; "
                f"expected one of {_VALID_ACTIONS}"
            )

    def applies_to(self, tool: str, ctx: PermissionContext) -> bool:
        """First-match-wins predicate combining tool + context check."""
        if self.tool != tool and self.tool != "*":
            return False
        if self.match is None or self.match.is_empty():
            return True
        return self.match.matches(ctx)


class PermissionGate:
    """Decides whether a builtin tool call should run.

    Rules are checked first-match-wins. The first rule whose ``tool``
    matches (exact or ``"*"``) **and** whose optional ``match`` block
    satisfies the caller's context decides. If nothing matches:

    - in strict mode, ``MUTATING_TOOLS`` default to ``deny`` and the
      rest default to ``allow``;
    - otherwise the gate's ``default_action`` (constructor arg) decides.
    """

    __slots__ = ("_rules", "_default", "_strict")

    def __init__(
        self,
        rules: list[PermissionRule] | None = None,
        *,
        default_action: str = ALLOW,
        strict: bool = False,
    ) -> None:
        if default_action not in _VALID_ACTIONS:
            raise ValueError(
                f"invalid default_action {default_action!r}; "
                f"expected one of {_VALID_ACTIONS}"
            )
        self._rules: tuple[PermissionRule, ...] = tuple(rules or [])
        self._default = default_action
        self._strict = strict

    @property
    def rules(self) -> tuple[PermissionRule, ...]:
        return self._rules

    @property
    def strict(self) -> bool:
        return self._strict

    def decide(self, tool: str) -> str:
        """Return ``"allow" | "deny" | "log"`` for ``tool``.

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
        """Context-aware decision; first matching rule wins."""
        ctx = PermissionContext(
            model=model, session_key=session_key, user_id=user_id
        )
        for rule in self._rules:
            if rule.applies_to(tool, ctx):
                return rule.action
        if self._strict and tool in MUTATING_TOOLS:
            return DENY
        return self._default

    def resolve(
        self,
        tool: str,
        ctx: PermissionContext,
    ) -> tuple[str, int | None]:
        """Same decision as :meth:`decide_with_context` but also returns
        the index of the matched rule (or ``None`` when the default /
        strict-mode fallback fired). Used by :meth:`audit_log_entry`."""
        for idx, rule in enumerate(self._rules):
            if rule.applies_to(tool, ctx):
                return rule.action, idx
        if self._strict and tool in MUTATING_TOOLS:
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
        ``session_key``, ``user_id``, ``rule_index``, ``strict``.
        """
        if rule_index is None:
            _, rule_index = self.resolve(tool, ctx)
        return {
            "tool": tool,
            "decision": decision,
            "model": ctx.model,
            "session_key": ctx.session_key,
            "user_id": ctx.user_id,
            "rule_index": rule_index,
            "strict": self._strict,
        }

    @classmethod
    def from_env(cls) -> PermissionGate:
        """Build a gate from environment configuration.

        - ``CORLINMAN_AGENT_PERMISSIONS`` — JSON list of rules.
        - ``CORLINMAN_AGENT_STRICT_MODE`` — truthy enables strict mode.

        Malformed JSON or invalid actions log a warning and degrade to
        the default (allow-all) — never raises into agent boot.
        """
        rules: list[PermissionRule] = []
        raw = os.environ.get("CORLINMAN_AGENT_PERMISSIONS", "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                for entry in parsed:
                    if not isinstance(entry, dict):
                        continue
                    tool = entry.get("tool")
                    action = entry.get("action")
                    if not isinstance(tool, str) or not tool.strip():
                        continue
                    if action not in _VALID_ACTIONS:
                        continue
                    match_block = _parse_match(entry.get("match"))
                    rules.append(
                        PermissionRule(
                            tool=tool.strip(),
                            action=action,
                            match=match_block,
                        )
                    )
        strict_raw = os.environ.get("CORLINMAN_AGENT_STRICT_MODE", "").strip().lower()
        strict = strict_raw in ("1", "true", "yes", "on")
        return cls(rules, strict=strict)


def _parse_match(raw: Any) -> RuleMatch | None:
    """Parse the optional ``match`` block on an env-supplied rule.

    Tolerant: anything that's not a dict, or whose declared fields are
    not strings, degrades to "no filter" (``None``) — the rule still
    fires for any context. We never raise here because permissions are
    config-driven and a typo shouldn't crash the agent.
    """
    if not isinstance(raw, dict):
        return None
    model = raw.get("model")
    session = raw.get("session_pattern")
    user = raw.get("user_pattern")
    fields = [
        ("model", model),
        ("session_pattern", session),
        ("user_pattern", user),
    ]
    kwargs: dict[str, str] = {}
    for name, value in fields:
        if value is None:
            continue
        if not isinstance(value, str) or not value:
            continue
        kwargs[name] = value
    if not kwargs:
        return None
    return RuleMatch(**kwargs)


__all__ = [
    "ALLOW",
    "DENY",
    "LOG",
    "MUTATING_TOOLS",
    "PermissionContext",
    "PermissionGate",
    "PermissionRule",
    "RuleMatch",
]
