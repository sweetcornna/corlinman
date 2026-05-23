"""Declarative permission gate for builtin tool calls.

A small, opinionated layer between the model's tool call and the actual
dispatch. Mirrors the opencode / Claude Code allow/deny/log idea, scoped
to what is actually useful for a chat-bot deployment:

- a per-tool action: ``allow`` / ``deny`` / ``log``
- a wildcard rule (``*``) for "everything else"
- a "strict mode" preset that flips every mutating tool to ``deny``
  unless an explicit ``allow`` rule overrides it

The gate is read at servicer construction (from env or an explicit rule
list) and consulted in ``_dispatch_builtin`` before any builtin tool
runs. A ``deny`` short-circuits with a ``permission_denied`` envelope
the model can read and react to.

Config sources (in precedence order):

1. **``$CORLINMAN_AGENT_PERMISSIONS``** — JSON list of
   ``{"tool": "<name>", "action": "allow|deny|log"}`` rules; the first
   match wins. ``"*"`` is a wildcard tool name.
2. **``$CORLINMAN_AGENT_STRICT_MODE``** = ``1`` — denies every mutating
   tool (write_file, edit_file, apply_patch, run_shell, revert_changes)
   unless rule (1) explicitly allows it.
3. otherwise the default is ``allow`` for every tool.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

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
class PermissionRule:
    """One rule: a tool name (or ``"*"``) and the action to take."""

    tool: str
    action: str

    def __post_init__(self) -> None:
        if self.action not in _VALID_ACTIONS:
            raise ValueError(
                f"invalid permission action {self.action!r}; "
                f"expected one of {_VALID_ACTIONS}"
            )


class PermissionGate:
    """Decides whether a builtin tool call should run.

    Rules are checked first-match-wins. The first rule whose ``tool``
    matches (exact or ``"*"``) decides. If nothing matches:

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
        """Return ``"allow" | "deny" | "log"`` for ``tool``."""
        for rule in self._rules:
            if rule.tool == tool or rule.tool == "*":
                return rule.action
        if self._strict and tool in MUTATING_TOOLS:
            return DENY
        return self._default

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
                    rules.append(PermissionRule(tool=tool.strip(), action=action))
        strict_raw = os.environ.get("CORLINMAN_AGENT_STRICT_MODE", "").strip().lower()
        strict = strict_raw in ("1", "true", "yes", "on")
        return cls(rules, strict=strict)


__all__ = [
    "ALLOW",
    "DENY",
    "LOG",
    "MUTATING_TOOLS",
    "PermissionGate",
    "PermissionRule",
]
