"""Tool-name canonicalization + its effect on subagent allowlist gating.

Regression for the prod bug where pulling a scoped skill (deep-research /
web_search) blocked every real tool, and a subagent spawned with
``tool_allowlist=["web.search", "web.fetch"]`` was rejected as privilege
escalation — both caused by the dotted skill namespace not matching the
underscore wire tool names.
"""

from __future__ import annotations

import pytest
import structlog
from corlinman_agent import tool_aliases
from corlinman_agent.subagent.runner import (
    _filter_tools_for_child,
    _ToolAllowlistEscalationError,
)
from corlinman_agent.tool_aliases import (
    canonicalize_tool_name,
    canonicalize_tool_names,
    detect_alias_collisions,
    warn_alias_collisions,
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


# ---------------------------------------------------------------------------
# collision detection — WARN, never reject (#108 item 3)
# ---------------------------------------------------------------------------


def test_detect_collision_finds_dotted_and_underscore_pair() -> None:
    # Two distinct spellings that fold onto the same wire name collide.
    collisions = detect_alias_collisions(["a.b", "a_b", "unrelated"])
    assert collisions == [("a_b", ["a.b", "a_b"])]


def test_detect_collision_dedupes_identical_spellings() -> None:
    # The SAME spelling repeated is not a collision (one distinct source).
    assert detect_alias_collisions(["web.search", "web.search"]) == []


def test_detect_collision_ignores_falsy_names() -> None:
    assert detect_alias_collisions(["a.b", "", None, "a_b"]) == [  # type: ignore[list-item]
        ("a_b", ["a.b", "a_b"])
    ]


def test_detect_collision_no_false_positive_on_shipped_tool_set() -> None:
    # The real dispatchable tool set is wire names plus the two genuinely
    # dotted blackboard tools — none of which fold onto each other.
    shipped = [
        "web_search",
        "web_fetch",
        "read_file",
        "write_file",
        "edit_file",
        "list_files",
        "search_files",
        "apply_patch",
        "run_shell",
        "memory_search",
        "memory_write",
        "kb_search",
        "calculator",
        "blackboard.read",
        "blackboard.write",
        "subagent_spawn",
        "Skill",
        "subagent_stop",
    ]
    assert detect_alias_collisions(shipped) == []


def test_warn_alias_collisions_logs_one_structured_warning() -> None:
    tool_aliases._warned_collisions.clear()
    with structlog.testing.capture_logs() as captured:
        warn_alias_collisions(["ns.tool", "ns_tool"], gate="unit")
    events = [e for e in captured if e.get("event") == "tool_aliases.collision"]
    assert len(events) == 1
    assert events[0]["log_level"] == "warning"
    assert events[0]["gate"] == "unit"
    assert events[0]["canonical"] == "ns_tool"
    assert events[0]["sources"] == ["ns.tool", "ns_tool"]


def test_warn_alias_collisions_deduped_per_gate() -> None:
    tool_aliases._warned_collisions.clear()
    with structlog.testing.capture_logs() as captured:
        warn_alias_collisions(["ns.tool", "ns_tool"], gate="unit")
        warn_alias_collisions(["ns.tool", "ns_tool"], gate="unit")
    events = [e for e in captured if e.get("event") == "tool_aliases.collision"]
    assert len(events) == 1


def test_warn_alias_collisions_silent_without_collision() -> None:
    tool_aliases._warned_collisions.clear()
    with structlog.testing.capture_logs() as captured:
        warn_alias_collisions(["web_search", "read_file"], gate="unit")
    assert [e for e in captured if e.get("event") == "tool_aliases.collision"] == []


def test_runner_gate_warns_but_keeps_outcome() -> None:
    # A parent set holding two spellings of one tool: the gate WARNs but the
    # fold behaviour (and the child's effective set) is unchanged.
    tool_aliases._warned_collisions.clear()
    parent = frozenset({"blackboard.read", "blackboard_read", "web_search"})
    with structlog.testing.capture_logs() as captured:
        effective = _filter_tools_for_child(
            parent_tool_names=parent,
            card_tools_allowed=None,
            requested_allowlist=["web_search"],
            child_depth=0,
            max_depth=3,
        )
    # Allow/deny outcome unchanged — the requested wire tool still resolves.
    assert effective == {"web_search"}
    events = [e for e in captured if e.get("event") == "tool_aliases.collision"]
    assert len(events) == 1
    assert events[0]["gate"] == "subagent_allowlist"
    assert events[0]["canonical"] == "blackboard_read"
