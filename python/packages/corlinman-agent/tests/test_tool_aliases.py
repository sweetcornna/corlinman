"""Tool-name canonicalization + its effect on subagent allowlist gating.

Regression for the prod bug where pulling a scoped skill (deep-research /
web_search) blocked every real tool, and a subagent spawned with
``tool_allowlist=["web.search", "web.fetch"]`` was rejected as privilege
escalation — both caused by the dotted skill namespace not matching the
underscore wire tool names.
"""

from __future__ import annotations

import pytest

from corlinman_agent.subagent.runner import (
    _filter_tools_for_child,
    _ToolAllowlistEscalationError,
)
from corlinman_agent.tool_aliases import (
    canonicalize_tool_name,
    canonicalize_tool_names,
)


@pytest.mark.parametrize(
    ("dotted", "wire"),
    [
        # Generic dotted-namespace fold.
        ("web.search", "web_search"),
        ("web.fetch", "web_fetch"),
        ("memory.search", "memory_search"),
        ("memory.write", "memory_write"),
        ("kb.search", "kb_search"),
        # Reversed namespace/verb family — needs the explicit alias.
        ("file.read", "read_file"),
        ("file.write", "write_file"),
        ("file.edit", "edit_file"),
        ("file.list", "list_files"),
        ("shell.run", "run_shell"),
        # Already-canonical / dotless names pass through unchanged.
        ("web_search", "web_search"),
        ("subagent_spawn", "subagent_spawn"),
        ("Skill", "Skill"),
    ],
)
def test_canonicalize_maps_dotted_to_wire(dotted: str, wire: str) -> None:
    assert canonicalize_tool_name(dotted) == wire


def test_canonicalize_is_idempotent() -> None:
    for name in ("file.read", "web.search", "web_search", "blackboard.read"):
        once = canonicalize_tool_name(name)
        assert canonicalize_tool_name(once) == once


def test_genuinely_dotted_tool_matches_on_both_sides() -> None:
    # ``blackboard.read`` is a real dotted runtime tool. The skill list and
    # the model's call canonicalize the SAME way, so they still match.
    assert canonicalize_tool_name("blackboard.read") == canonicalize_tool_name(
        "blackboard.read"
    )


def test_canonicalize_names_drops_falsy() -> None:
    assert canonicalize_tool_names(["web.search", "", None, "file.read"]) == {
        "web_search",
        "read_file",
    }
    assert canonicalize_tool_names(None) == set()


# ---------------------------------------------------------------------------
# subagent allowlist gating now matches dotted requests against wire parents
# ---------------------------------------------------------------------------

_PARENT = frozenset({"web_search", "web_fetch", "read_file", "write_file"})


def test_dotted_allowlist_no_longer_escalates() -> None:
    # The exact prod repro: parent holds wire tools, child requests dotted.
    effective = _filter_tools_for_child(
        parent_tool_names=_PARENT,
        card_tools_allowed=None,
        requested_allowlist=["web.search", "web.fetch"],
        child_depth=0,
        max_depth=3,
    )
    # Resolved to the parent's REAL wire names (dispatchable downstream).
    assert effective == {"web_search", "web_fetch"}


def test_dotted_card_narrowing_intersects_in_canon_space() -> None:
    effective = _filter_tools_for_child(
        parent_tool_names=_PARENT,
        card_tools_allowed=["web.search", "file.read"],
        requested_allowlist=None,
        child_depth=0,
        max_depth=3,
    )
    assert effective == {"web_search", "read_file"}


def test_genuine_escalation_still_rejected() -> None:
    # A tool the parent genuinely lacks must still be refused.
    with pytest.raises(_ToolAllowlistEscalationError) as exc:
        _filter_tools_for_child(
            parent_tool_names=_PARENT,
            card_tools_allowed=None,
            requested_allowlist=["web.search", "delete_everything"],
            child_depth=0,
            max_depth=3,
        )
    # The original (un-canonicalized) offending name is surfaced.
    assert "delete_everything" in exc.value.offending


def test_wire_request_against_wire_parent_unaffected() -> None:
    # Pre-existing behaviour: a plain wire allowlist keeps working.
    effective = _filter_tools_for_child(
        parent_tool_names=_PARENT,
        card_tools_allowed=None,
        requested_allowlist=["web_search"],
        child_depth=0,
        max_depth=3,
    )
    assert effective == {"web_search"}
