"""Canonicalize skill / allowlist tool names to their wire form.

Skill ``allowed-tools`` frontmatter — and the per-spawn ``tool_allowlist``
a parent model copies out of skill prose — historically use a dotted
"logical" namespace (``file.read``, ``web.search``, ``shell.run``) that
predates, and never matched, the underscore *wire* tool names the runtime
actually dispatches (``read_file``, ``web_search``, ``run_shell``). Because
the allowed-tools gate and the subagent escalation check compared those
strings literally, a skill that scoped itself to ``web.search`` silently
blocked the real ``web_search`` tool: pulling ``deep-research`` /
``web_search`` denied every web + file tool, and a subagent spawned with
``tool_allowlist=["web.search"]`` was rejected as privilege escalation.

The dotted→wire mapping is NOT a mechanical ``.`` → ``_`` swap:

* ``web.search`` → ``web_search`` (namespace kept, dot becomes underscore)
* ``file.read``  → ``read_file``  (namespace and verb REVERSED)
* ``blackboard.read`` is a genuinely-dotted runtime tool — it must survive
  unchanged so a skill that lists it still matches the real tool.

So we resolve the reversed ``file.*`` / ``shell.*`` family with an explicit
table, then fold the remaining dotted names with a plain ``.`` → ``_``.
Crucially this is applied to BOTH sides of every comparison, so a
dotted-native tool canonicalizes the same way on each side and keeps
matching (``blackboard.read`` → ``blackboard_read`` on both the skill list
and the model's call).
"""

from __future__ import annotations

from collections.abc import Iterable

# Dotted names whose verb/namespace order REVERSES into the wire name, so a
# plain ``.`` → ``_`` would produce the wrong string (``file_read`` instead
# of ``read_file``). Everything else is handled by the generic rule below.
_REVERSED_TOOL_ALIASES: dict[str, str] = {
    "file.read": "read_file",
    "file.write": "write_file",
    "file.edit": "edit_file",
    "file.list": "list_files",
    "file.search": "search_files",
    "file.apply_patch": "apply_patch",
    "shell.run": "run_shell",
    "shell.exec": "run_shell",
}


def canonicalize_tool_name(name: str) -> str:
    """Resolve a skill-declared tool ``name`` to its wire form.

    Total and idempotent: an already-canonical wire name (``web_search``),
    an unknown name, or a genuinely-dotted runtime tool all come back in a
    stable canonical form so comparing two canonicalized names is correct.
    """
    n = (name or "").strip()
    if not n:
        return n
    aliased = _REVERSED_TOOL_ALIASES.get(n)
    if aliased is not None:
        return aliased
    # Generic dotted-namespace fold (``web.search`` → ``web_search``,
    # ``memory.write`` → ``memory_write``, ``blackboard.read`` →
    # ``blackboard_read``). A bare wire name with no dot is unchanged.
    return n.replace(".", "_")


def canonicalize_tool_names(names: Iterable[str] | None) -> set[str]:
    """:func:`canonicalize_tool_name` lifted over an iterable into a set.

    ``None`` and falsy entries are dropped so callers can pass a raw
    allowed-tools / allowlist list straight through.
    """
    return {canonicalize_tool_name(n) for n in (names or ()) if n}


__all__ = ["canonicalize_tool_name", "canonicalize_tool_names"]
