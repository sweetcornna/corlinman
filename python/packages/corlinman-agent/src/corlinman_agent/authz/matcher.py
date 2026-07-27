"""The rule-matching core, extracted from ``corlinman_agent.permission``.

W3-1 moves the matching machinery here VERBATIM in semantics — the rule
grammar (``tool(pattern)`` sugar, fnmatch arg candidates, the SEC-05
multi-command resolution) is unchanged — and extends the context filter
with the two new scope dimensions (``tenant`` / ``surface``).

Matching keeps the two long-standing principles of ``RuleMatch.matches``:

(a) every declared filter must match (logical AND);
(b) a missing context value never matches a non-empty pattern — a rule
    keyed on ``user="admin*"`` must not fire on anonymous calls.

``surface`` deliberately uses ``|``-separated alternation with EXACT
segment comparison instead of fnmatch: surfaces are a closed set, and a
glob like ``qq*`` accidentally matching both ``qq`` and ``qq_official``
is a foreseeable operations accident.
"""

from __future__ import annotations

import fnmatch
import json
import shlex
from dataclasses import dataclass
from typing import Any

from corlinman_agent.authz.model import _VALID_ACTIONS, Subject

#: File-editing tools that ``PermissionMode.ACCEPT_EDITS`` auto-allows.
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
        # ``render_document`` writes a rendered artifact into the workspace
        # — same blast radius as write_file, so plan/strict deny it too.
        "render_document",
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
        # ``shell_task_kill`` terminates a running background shell process
        # group — a real side effect, so plan/strict mode must deny it by
        # default. (It also inherits run_shell's verdict via
        # ``_PERMISSION_TOOL_ALIAS`` below, so an explicit run_shell allow
        # rule lets the model terminate the tasks it started.)
        "shell_task_kill",
    }
)

#: Permission aliases — a tool that inherits another tool's verdict entirely
#: (rules + mode + strict). ``shell_task_kill`` is the teardown surface of
#: ``run_shell``, so it resolves WITH run_shell's identity: allowed wherever
#: run_shell is (an explicit allow rule) and denied in plan/strict by
#: default exactly like run_shell. Without this, run_shell — whose schema
#: now advertises ``run_in_background=true`` — could start a background task
#: the model is then forbidden to ``shell_task_kill`` (Codex #112 r6).
_PERMISSION_TOOL_ALIAS: dict[str, str] = {
    "shell_task_kill": "run_shell",
}

#: The background-shell task-control surface: poll a task's output and
#: terminate it. Both operate ONLY on tasks the caller's session already
#: started (the registry's session-ownership gate confines them), so their
#: permission tracks the *grant to start* tasks, not run_shell's per-command
#: scoping (Codex #112 r7).
_TASK_CONTROL_TOOLS: frozenset[str] = frozenset(
    {"shell_task_output", "shell_task_kill"}
)


def _surface_matches(pattern: str, value: str | None) -> bool:
    """``|``-separated alternation over the closed surface set."""
    if not value:
        return False
    segments = [seg.strip().lower() for seg in pattern.split("|") if seg.strip()]
    return value.strip().lower() in segments


#: Characters that make a rule's ``tool`` field a glob rather than a literal.
_GLOB_CHARS: tuple[str, ...] = ("*", "?", "[")


def tool_pattern_matches(pattern: str, tool: str) -> bool:
    """Match one rule ``tool`` field against a canonical tool key (C7).

    Semantics are a strict superset of the historical exact-or-``"*"``
    comparison: a pattern without glob characters still matches only its
    literal spelling (so every pre-W3-2 rule behaves identically), while a
    pattern carrying ``*`` / ``?`` / ``[`` is evaluated with
    ``fnmatch.fnmatchcase`` so namespaced keys like ``mcp:github/*`` or
    ``plugin:file-ops/*`` work as documented in the unified model.
    """
    if pattern == tool or pattern == "*":
        return True
    if any(ch in pattern for ch in _GLOB_CHARS):
        return fnmatch.fnmatchcase(tool, pattern)
    return False


def glob_escape(text: str) -> str:
    """Escape ``text`` so fnmatch treats every character literally.

    Used by the ``[approvals]`` translator: an exact session key / plugin
    name must self-match under fnmatch even if it happens to contain glob
    metacharacters. ``[`` must be escaped first — the escape sequences
    themselves introduce brackets.
    """
    return text.replace("[", "[[]").replace("*", "[*]").replace("?", "[?]")


def external_candidate_keys(plugin: str, tool: str) -> tuple[str, ...]:
    """Canonical candidate keys for an EXTERNAL (non-builtin) tool call (C7).

    The OpenAI function-call path collapses ``plugin == tool ==
    function.name`` before the call reaches the agent (see
    ``ReasoningLoop._finalise_tool_call``), so the agent process cannot
    always recover the real namespace. It therefore matches a rule against
    EVERY plausible canonical spelling; the first entry (the bare advertised
    name) doubles as the stable grant/audit key:

    * the bare name — existing name-keyed rules and the ``"*"`` wildcard;
    * ``plugin:<plugin>/<tool>`` — the gateway-plugin canonical form
      (both the collapsed and the explicit two-part identity);
    * ``mcp:<server>/<tool>`` for every underscore split of a
      ``{server}_{tool}`` advertised name (the gateway advertises MCP
      tools under that collapsed form; the agent cannot know which
      underscore is the separator, so each split is offered — an
      ``mcp:github/*`` rule then fires on ``github_create_issue``).

    The gateway-side second enforcement point (the plugin dispatcher)
    knows the registry and re-checks with the EXACT canonical key.
    """
    bare = (tool or plugin or "").strip()
    plugin_name = (plugin or "").strip()
    if not bare:
        return ()
    keys: list[str] = [bare]
    if plugin_name and plugin_name != bare:
        keys.append(f"plugin:{plugin_name}/{bare}")
    keys.append(f"plugin:{bare}/{bare}")
    parts = bare.split("_")
    for i in range(1, len(parts)):
        server = "_".join(parts[:i])
        name = "_".join(parts[i:])
        if server and name:
            keys.append(f"mcp:{server}/{name}")
    # De-dup while preserving order (dict is insertion-ordered).
    return tuple(dict.fromkeys(keys))


@dataclass(frozen=True)
class RuleMatch:
    """Optional context filters on a :class:`PermissionRule`.

    All non-empty fields must match the caller's context for the parent
    rule to fire (fnmatch for the glob fields, exact ``|`` alternation for
    ``surface``). An empty field is treated as "don't care".
    """

    model: str | None = None
    session_pattern: str | None = None
    user_pattern: str | None = None
    tenant: str | None = None
    surface: str | None = None

    def is_empty(self) -> bool:
        """True when no filter is declared — the rule matches any context."""
        return (
            self.model is None
            and self.session_pattern is None
            and self.user_pattern is None
            and self.tenant is None
            and self.surface is None
        )

    def matches(self, ctx: Subject) -> bool:
        """Return True if ``ctx`` satisfies every declared filter.

        A missing context value (``None`` / empty string) is treated as
        a non-match against any non-empty pattern — we don't want a rule
        keyed on ``user_pattern="admin*"`` to fire on anonymous calls.
        The new dimensions are read via ``getattr`` so a legacy context
        object without them behaves exactly like a missing value.
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
        if self.tenant is not None:
            tenant = getattr(ctx, "tenant_id", None)
            if not tenant or not fnmatch.fnmatchcase(tenant, self.tenant):
                return False
        if self.surface is not None:
            surface = getattr(ctx, "surface", None)
            if not _surface_matches(self.surface, surface):
                return False
        return True


@dataclass(frozen=True)
class PermissionRule:
    """One rule: a tool name (or ``"*"``) and the action to take.

    Optional ``match`` narrows the rule to a particular model / session /
    user / tenant / surface (the TOML spelling of the block is ``scope``;
    ``match`` remains the accepted legacy alias). ``arg_pattern`` narrows a
    rule to a particular argument value — the ``tool(pattern)`` sugar in a
    rule's ``tool`` string is parsed at construction, e.g.
    ``"run_shell(rm:*)"`` sets ``tool="run_shell"`` and
    ``arg_pattern="rm:*"``.

    ``memory`` (once/session/always) and ``note`` are carried verbatim for
    the approval pipeline / audit log; neither affects matching.
    """

    tool: str
    action: str
    match: RuleMatch | None = None
    arg_pattern: str | None = None
    memory: str | None = None
    note: str | None = None

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

    def applies_to(self, tool: str, ctx: Subject) -> bool:
        """Match-order-agnostic predicate combining tool + context check.

        Legacy / args-unaware: a rule carrying an ``arg_pattern`` only
        matches here when the pattern is the catch-all ``"*"`` (so the
        args-aware resolve path is required to honour a narrowing pattern).
        """
        if not tool_pattern_matches(self.tool, tool):
            return False
        if self.arg_pattern is not None and self.arg_pattern != "*":
            return False
        if self.match is None or self.match.is_empty():
            return True
        return self.match.matches(ctx)

    def applies_to_args(
        self,
        tool: str,
        ctx: Subject,
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
        if not tool_pattern_matches(self.tool, tool):
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

    ``rule`` examples: ``"run_shell(git push*)"`` (natural command-prefix
    form), ``"run_shell(git:*)"`` (the permission gate's basename form —
    both work here), ``"write_file(*.ts)"``, ``"run_shell"`` (any args),
    ``"*"`` (any tool). Unparseable rules return ``False`` (the hook
    group is skipped, never the tool call).

    Scope note: for ``run_shell`` the raw command string is added as an
    extra match candidate ON TOP of the gate's ``basename:command``
    candidates, so the documented claude-style spelling fires too. This
    widening is hook-local — it only selects which hook groups run; the
    permission gate's own rule semantics are untouched.
    """
    text = str(rule or "").strip()
    if not text:
        return False
    try:
        parsed = PermissionRule(tool=text, action="allow")
    except ValueError:
        return False
    candidates = extract_arg_candidates(tool, args)
    if tool == "run_shell" and isinstance(args, dict):
        raw_command = args.get("command")
        if isinstance(raw_command, str) and raw_command.strip():
            merged = [candidates] if isinstance(candidates, str) else list(candidates or [])
            merged.append(raw_command.strip())
            candidates = merged
    return parsed.applies_to_args(tool, Subject(), candidates)


#: Keys accepted inside a rule's ``scope`` block (the ``match`` legacy
#: aliases in parentheses). Values must be non-empty strings.
_SCOPE_KEY_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("model", ("model",)),
    ("session_pattern", ("session", "session_pattern")),
    ("user_pattern", ("user", "user_pattern")),
    ("tenant", ("tenant",)),
    ("surface", ("surface",)),
)


def _parse_match(raw: Any) -> RuleMatch | None:
    """Parse the optional ``scope`` / ``match`` block on a rule.

    Accepts both the new ``scope`` spelling (``user`` / ``session`` /
    ``model`` / ``tenant`` / ``surface``) and the legacy ``match`` spelling
    (``user_pattern`` / ``session_pattern`` / ``model``). Tolerant:
    anything that's not a dict, or whose declared fields are not strings,
    degrades to "no filter" (``None``) — the rule still fires for any
    context. We never raise here because permissions are config-driven and
    a typo shouldn't crash the agent.
    """
    if not isinstance(raw, dict):
        return None
    kwargs: dict[str, str] = {}
    for field, aliases in _SCOPE_KEY_ALIASES:
        for alias in aliases:
            value = raw.get(alias)
            if isinstance(value, str) and value:
                kwargs[field] = value
                break
    if not kwargs:
        return None
    return RuleMatch(**kwargs)


def parse_rule_list(raw: str) -> list[PermissionRule]:
    """Parse a JSON rule list (the ``CORLINMAN_AGENT_PERMISSIONS`` shape).

    Each entry is ``{"tool": ..., "action": ..., "scope": {...}?,
    "match": {...}?, "arg_pattern": ...?, "memory": ...?, "note": ...?}``.
    ``scope`` is the canonical spelling; ``match`` remains a deprecated
    alias (``scope`` wins when both are present). The ``tool(pattern)``
    sugar inside ``tool`` is honoured by :class:`PermissionRule`. Tolerant:
    a non-list, a non-dict entry, an invalid action, or a missing tool is
    skipped — never raises.
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
        match_block = _parse_match(entry.get("scope")) or _parse_match(
            entry.get("match")
        )
        arg_pattern = entry.get("arg_pattern")
        if not isinstance(arg_pattern, str) or not arg_pattern:
            arg_pattern = None
        memory = entry.get("memory")
        if not isinstance(memory, str) or memory.strip().lower() not in (
            "once",
            "session",
            "always",
        ):
            memory = None
        else:
            memory = memory.strip().lower()
        note = entry.get("note")
        if not isinstance(note, str) or not note.strip():
            note = None
        try:
            rules.append(
                PermissionRule(
                    tool=tool.strip(),
                    action=action,
                    match=match_block,
                    arg_pattern=arg_pattern,
                    memory=memory,
                    note=note,
                )
            )
        except ValueError:
            continue
    return rules


__all__ = [
    "MUTATING_TOOLS",
    "PermissionRule",
    "RuleMatch",
    "external_candidate_keys",
    "extract_arg_candidates",
    "extract_primary_arg",
    "glob_escape",
    "match_hook_rule",
    "parse_rule_list",
    "tool_pattern_matches",
]
