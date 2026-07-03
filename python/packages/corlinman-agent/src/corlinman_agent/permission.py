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
   tool (write_file, edit_file, apply_patch, run_shell, revert_changes,
   qzone_publish, memory_write, send_attachment, text_to_speech) unless
   rule (1) explicitly allows it.
3. otherwise the default is ``allow`` for every tool.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shlex
from dataclasses import dataclass
from enum import Enum
from typing import Any

ALLOW: str = "allow"
DENY: str = "deny"
LOG: str = "log"
#: ``ask`` — defer to an interactive approval prompt (routed through the
#: :class:`~corlinman_agent.approval_gate.ApprovalGate`). gap
#: ``permissions-no-ask-action``: previously the gate only knew
#: allow/deny/log, so there was no way to express "let a human decide".
ASK: str = "ask"
_VALID_ACTIONS = (ALLOW, DENY, LOG, ASK)


class PermissionMode(str, Enum):
    """Coarse operating mode layered ABOVE the per-tool rule list.

    gap ``permissions-no-permission-mode``. Mirrors Claude Code's
    permission modes. The mode is consulted only when the rule list does
    not produce an explicit ``allow`` / ``deny`` / ``ask`` for a tool:

    * :attr:`DEFAULT` — fall through to the gate's ``default_action`` /
      strict-mode fallback (legacy behaviour).
    * :attr:`ACCEPT_EDITS` — auto-allow file-edit tools (write/edit/patch)
      without prompting; everything else falls through to default.
    * :attr:`PLAN` — deny every mutating tool (planning only, no side
      effects); read-only tools still fall through to default.
    * :attr:`BYPASS` — allow everything (no gating). Operator opt-in for
      trusted automation.

    Stored as lowercase strings to match a ``mode = "..."`` config knob.
    """

    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    PLAN = "plan"
    BYPASS = "bypass"

    @classmethod
    def coerce(cls, raw: Any) -> PermissionMode:
        """Best-effort parse; unknown / falsy values map to :attr:`DEFAULT`."""
        if isinstance(raw, PermissionMode):
            return raw
        if isinstance(raw, str):
            for member in cls:
                if member.value.lower() == raw.strip().lower():
                    return member
        return cls.DEFAULT


#: File-editing tools that :attr:`PermissionMode.ACCEPT_EDITS` auto-allows.
_EDIT_TOOLS: frozenset[str] = frozenset(
    {"write_file", "edit_file", "notebook_edit", "apply_patch", "revert_changes"}
)

#: The "mutating" tools — strict mode flips these to ``deny`` by default.
#: Read-only tools (read/list/search/web/calc/todo/subagent/blackboard)
#: stay allowed in strict mode because they have no blast radius on
#: their own.
MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "write_file",
        "edit_file",
        "notebook_edit",
        "apply_patch",
        "run_shell",
        "revert_changes",
        # ``qzone_publish`` writes externally — posts a 说说 to QQ空间
        # via the user's logged-in QQ account. Treat as mutating so
        # strict-mode deployments must explicitly opt in.
        "qzone_publish",
        # ``memory_write`` persists state to the agent's long-term memory
        # store — a durable side effect that survives the turn.
        "memory_write",
        # ``send_attachment`` / ``text_to_speech`` push content OUT to the
        # chat channel (a file / a synthesised audio clip). Outbound side
        # effects with real blast radius, so strict mode must opt in.
        "send_attachment",
        "text_to_speech",
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

    gap ``permissions-no-per-arg-rules``: ``arg_pattern`` narrows a rule to
    a particular argument value. Two spellings, mirroring Claude Code's
    ``Bash(rm:*)`` / opencode's command globs:

    * The ``tool(pattern)`` sugar in a rule's ``tool`` string is parsed at
      construction — e.g. ``"run_shell(rm:*)"`` sets ``tool="run_shell"``
      and ``arg_pattern="rm:*"``.
    * ``arg_pattern`` may also be supplied explicitly.

    The pattern is matched (fnmatch, case-sensitive) against the call's
    *primary argument* — for ``run_shell`` that is the ``command`` arg's
    first token + ``:`` + the full command (so ``rm:*`` matches any
    ``rm ...`` invocation), and for file tools it is the ``path`` arg.
    Falling back to the whole JSON when no primary arg is found.
    """

    tool: str
    action: str
    match: RuleMatch | None = None
    arg_pattern: str | None = None

    def __post_init__(self) -> None:
        if self.action not in _VALID_ACTIONS:
            raise ValueError(
                f"invalid permission action {self.action!r}; "
                f"expected one of {_VALID_ACTIONS}"
            )
        # Parse the ``tool(pattern)`` sugar once at construction. ``frozen``
        # dataclasses forbid plain attribute assignment, so use
        # ``object.__setattr__`` to backfill the parsed fields.
        if self.arg_pattern is None and "(" in self.tool and self.tool.endswith(")"):
            head, _, tail = self.tool.partition("(")
            pattern = tail[:-1].strip()
            object.__setattr__(self, "tool", head.strip())
            if pattern:
                object.__setattr__(self, "arg_pattern", pattern)

    def applies_to(self, tool: str, ctx: PermissionContext) -> bool:
        """First-match-wins predicate combining tool + context check.

        Legacy / args-unaware: a rule carrying an ``arg_pattern`` only
        matches here when the pattern is the catch-all ``"*"`` (so the
        args-aware :meth:`PermissionGate.resolve_with_args` is required to
        honour a narrowing pattern). This keeps the old tool+ctx call sites
        from accidentally over-matching an arg-scoped rule.
        """
        if self.tool != tool and self.tool != "*":
            return False
        if self.arg_pattern is not None and self.arg_pattern != "*":
            return False
        if self.match is None or self.match.is_empty():
            return True
        return self.match.matches(ctx)

    def applies_to_args(
        self,
        tool: str,
        ctx: PermissionContext,
        arg_value: str | list[str] | None,
    ) -> bool:
        """Args-aware predicate: tool + context + optional arg pattern.

        ``arg_value`` may be a single string OR a list of candidate strings
        (e.g. ``run_shell`` resolves every command basename across compound
        segments — see :func:`extract_arg_candidates`). When a list is given
        the rule fires if its pattern matches **any** candidate, so a deny
        rule like ``run_shell(rm:*)`` catches ``cd /tmp && rm -rf x`` and
        ``sh -c "rm -rf /"`` as well as the bare ``rm`` form.
        """
        if self.tool != tool and self.tool != "*":
            return False
        if self.match is not None and not self.match.is_empty():
            if not self.match.matches(ctx):
                return False
        if self.arg_pattern is None or self.arg_pattern == "*":
            return True
        if arg_value is None:
            return False
        candidates = [arg_value] if isinstance(arg_value, str) else arg_value
        return any(
            fnmatch.fnmatchcase(candidate, self.arg_pattern)
            for candidate in candidates
        )


def extract_primary_arg(tool: str, args: dict[str, Any] | None) -> str | None:
    """Return the value a per-arg rule matches against for ``tool``.

    For ``run_shell`` the value is ``<first-token>:<full command>`` so a
    pattern like ``rm:*`` matches any ``rm ...`` invocation while ``*`` and
    a bare-command glob still work. For file tools (write/edit/read/patch)
    it is the ``path`` arg. Otherwise the first string value in ``args``.
    ``None`` when nothing usable is present.
    """
    if not isinstance(args, dict) or not args:
        return None
    if tool == "run_shell":
        command = args.get("command")
        if isinstance(command, str) and command.strip():
            try:
                tokens = shlex.split(command)
            except ValueError:
                tokens = command.split()
            head = tokens[0] if tokens else command.strip().split(" ", 1)[0]
            return f"{head}:{command.strip()}"
        return None
    if tool in _EDIT_TOOLS or tool in ("read_file", "list_files", "search_files"):
        path = args.get("path") or args.get("file") or args.get("filename")
        if isinstance(path, str) and path:
            return path
    for value in args.values():
        if isinstance(value, str) and value:
            return value
    return None


def extract_arg_candidates(
    tool: str, args: dict[str, Any] | None
) -> str | list[str] | None:
    """Return ALL per-arg match candidates for ``tool``.

    Like :func:`extract_primary_arg` but, for ``run_shell``, resolves EVERY
    command basename across compound / piped / sh-dash-c / env-prefixed /
    path-qualified forms (via
    :func:`corlinman_agent.coding.shell.extract_command_names`) and returns one
    ``"<basename>:<full command>"`` candidate per resolved command. A per-arg
    deny rule (``run_shell(rm:*)``) then fires if it matches ANY candidate —
    closing the SEC-05 bypass where only the first shlex token was matched.

    For every other tool it delegates to :func:`extract_primary_arg` (a single
    string). ``None`` when nothing usable is present.
    """
    if not isinstance(args, dict) or not args:
        return None
    if tool == "run_shell":
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return None
        command = command.strip()
        # Lazy import to avoid a hard coupling at module import time; the
        # shell helper lives in the coding subpackage.
        try:
            from corlinman_agent.coding.shell import extract_command_names

            names = extract_command_names(command)
        except Exception:  # noqa: BLE001 — degrade to the legacy single value
            names = []
        if not names:
            # Fall back to the legacy first-token form so an empty resolution
            # never silently disables a deny rule.
            single = extract_primary_arg(tool, args)
            return single
        return [f"{name}:{command}" for name in names]
    return extract_primary_arg(tool, args)


def match_hook_rule(rule: str, tool: str, args: dict[str, Any] | None = None) -> bool:
    """Evaluate one permission-rule string against a tool call.

    The declarative-hooks ``if`` matcher (``corlinman-hooks`` cannot import
    this package, so the grammar is injected as this callable). Reuses the
    exact ``tool(pattern)`` sugar and arg-candidate extraction the
    permission gate uses — the rule grammar is designed once and shared,
    per the parity-matrix contract.

    ``rule`` examples: ``"run_shell(git push*)"``, ``"write_file(*.ts)"``,
    ``"run_shell"`` (any args), ``"*"`` (any tool). Unparseable rules
    return ``False`` (the hook group is skipped, never the tool call).
    """
    text = str(rule or "").strip()
    if not text:
        return False
    try:
        parsed = PermissionRule(tool=text, action="allow")
    except ValueError:
        return False
    candidates = extract_arg_candidates(tool, args)
    return parsed.applies_to_args(tool, PermissionContext(), candidates)


class PermissionGate:
    """Decides whether a builtin tool call should run.

    Rules are checked first-match-wins. The first rule whose ``tool``
    matches (exact or ``"*"``) **and** whose optional ``match`` block
    satisfies the caller's context decides. If nothing matches:

    - in strict mode, ``MUTATING_TOOLS`` default to ``deny`` and the
      rest default to ``allow``;
    - otherwise the gate's ``default_action`` (constructor arg) decides.
    """

    __slots__ = ("_default", "_last_match_wins", "_mode", "_rules", "_strict")

    def __init__(
        self,
        rules: list[PermissionRule] | None = None,
        *,
        default_action: str = ALLOW,
        strict: bool = False,
        mode: PermissionMode | str = PermissionMode.DEFAULT,
        last_match_wins: bool = False,
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
        # gap permissions-no-per-arg-rules: when ``last_match_wins`` is set
        # the LAST matching rule decides (opencode / shell-config semantics)
        # instead of the first. Layered rule sources stack later (more
        # specific) layers AFTER earlier ones, so last-match-wins lets a
        # project-level rule override a global default.
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
        """Context-aware decision; first matching rule wins (or last when
        ``last_match_wins`` is set)."""
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

    def resolve_with_args(
        self,
        tool: str,
        ctx: PermissionContext,
        args: dict[str, Any] | None,
    ) -> tuple[str, int | None]:
        """Args-aware decision honouring per-argument / command patterns.

        gap ``permissions-no-per-arg-rules``. The primary argument value is
        extracted via :func:`extract_primary_arg` and matched against each
        rule's ``arg_pattern`` (fnmatch). Match order respects
        ``last_match_wins``. ``BYPASS`` mode short-circuits to ``allow``
        before any rule; ``acceptEdits`` / ``plan`` apply only when no rule
        matched.
        """
        # BYPASS wins over everything — operator opted out of gating.
        if self._mode is PermissionMode.BYPASS:
            return ALLOW, None

        arg_value = extract_arg_candidates(tool, args)
        matched: tuple[str, int] | None = None
        for idx, rule in enumerate(self._rules):
            if rule.applies_to_args(tool, ctx, arg_value):
                matched = (rule.action, idx)
                if not self._last_match_wins:
                    break
        if matched is not None:
            return matched

        # No explicit rule — consult the operating mode, then strict-mode,
        # then the gate default.
        mode_action = self._mode_override(tool)
        if mode_action is not None:
            return mode_action, None
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
            "mode": self._mode.value,
        }

    @classmethod
    def from_env(cls) -> PermissionGate:
        """Build a gate from environment configuration.

        - ``CORLINMAN_AGENT_PERMISSIONS`` — JSON list of rules.
        - ``CORLINMAN_AGENT_STRICT_MODE`` — truthy enables strict mode.
        - ``CORLINMAN_AGENT_PERMISSION_MODE`` — one of
          ``default``/``acceptEdits``/``plan``/``bypass``.
        - ``CORLINMAN_AGENT_PERMISSION_LAST_MATCH_WINS`` — truthy flips the
          gate to last-match-wins ordering.

        Malformed JSON or invalid actions log a warning and degrade to
        the default (allow-all) — never raises into agent boot.
        """
        rules = parse_rule_list(os.environ.get("CORLINMAN_AGENT_PERMISSIONS", ""))
        strict_raw = os.environ.get("CORLINMAN_AGENT_STRICT_MODE", "").strip().lower()
        strict = strict_raw in ("1", "true", "yes", "on")
        mode = PermissionMode.coerce(
            os.environ.get("CORLINMAN_AGENT_PERMISSION_MODE", "")
        )
        lmw_raw = (
            os.environ.get("CORLINMAN_AGENT_PERMISSION_LAST_MATCH_WINS", "")
            .strip()
            .lower()
        )
        last_match_wins = lmw_raw in ("1", "true", "yes", "on")
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
        rules: list[PermissionRule] = []
        for layer in layers:
            if layer is None:
                continue
            if isinstance(layer, str):
                rules.extend(parse_rule_list(layer))
            elif isinstance(layer, list):
                rules.extend(parse_rule_list(json.dumps(layer)))
        return cls(
            rules, strict=strict, mode=mode, last_match_wins=last_match_wins
        )


def parse_rule_list(raw: str) -> list[PermissionRule]:
    """Parse a JSON rule list (the ``CORLINMAN_AGENT_PERMISSIONS`` shape).

    Each entry is ``{"tool": ..., "action": ..., "match": {...}?,
    "arg_pattern": ...?}``. The ``tool(pattern)`` sugar inside ``tool`` is
    honoured by :class:`PermissionRule`. Tolerant: a non-list, a non-dict
    entry, an invalid action, or a missing tool is skipped — never raises.
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    rules: list[PermissionRule] = []
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
        arg_pattern = entry.get("arg_pattern")
        if not isinstance(arg_pattern, str) or not arg_pattern:
            arg_pattern = None
        try:
            rules.append(
                PermissionRule(
                    tool=tool.strip(),
                    action=action,
                    match=match_block,
                    arg_pattern=arg_pattern,
                )
            )
        except ValueError:
            continue
    return rules


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
    "parse_rule_list",
]
