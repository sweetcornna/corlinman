"""/rewind — checkpoint listing, restore, and dispatch behaviour.

Checkpoints are created through the *real* snapshot API
(:func:`corlinman_agent.coding._snapshot.snapshot`) against a tmp git
workspace — the same store the agent servicer writes per turn — so
these tests exercise the actual consumption path, not a mock.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from corlinman_agent.coding._snapshot import snapshot
from corlinman_server.console.brain import BrainSession
from corlinman_server.console.commands import dispatch
from corlinman_server.console.rewind import (
    format_checkpoints,
    list_checkpoints,
    rewind_to,
)

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary required for snapshot store"
)


class _IdleBrain:
    descriptor = "stub brain"

    def run_turn(self, **_kw: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("rewind must not run turns")

    async def aclose(self) -> None:  # pragma: no cover - unused
        pass


class _StubApp:
    """The slice of ConsoleApp that cmd_rewind touches."""

    def __init__(self, *, embedded: bool = True) -> None:
        self.embedded = embedded
        self.session = BrainSession(brain=_IdleBrain(), model="big")


async def _dispatch_text(app: Any, line: str) -> str:
    """Dispatch and narrow: /rewind always answers with plain text."""
    out = await dispatch(app, line)
    assert isinstance(out, str)
    return out


def _seed_workspace(ws: Path) -> dict[str, str]:
    """Replay three agent turns the way the servicer does.

    The servicer snapshots at the *start* of a turn (state before that
    turn's edits), labelled with the user text. Layout produced:

    * checkpoint "one"   — empty workspace
    * checkpoint "two"   — a.txt == v1   (turn one's edit)
    * checkpoint "three" — a.txt == v2   (turn two's edit)
    * working tree       — a.txt == v3   (turn three's edit, uncommitted)
    """
    shas: dict[str, str] = {}
    sha = snapshot(ws, "one")
    assert sha is not None
    shas["one"] = sha
    (ws / "a.txt").write_text("v1")
    sha = snapshot(ws, "two")
    assert sha is not None
    shas["two"] = sha
    (ws / "a.txt").write_text("v2")
    sha = snapshot(ws, "three")
    assert sha is not None
    shas["three"] = sha
    (ws / "a.txt").write_text("v3")
    return shas


def test_list_checkpoints_newest_first_with_sha_label_timestamp(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    shas = _seed_workspace(ws)
    cps = list_checkpoints(ws)
    # newest first: three, two, one, then ensure_repo's initial commit
    assert [c.label for c in cps] == ["three", "two", "one", "initial"]
    assert cps[0].sha == shas["three"]
    assert cps[1].sha == shas["two"]
    for cp in cps:
        assert len(cp.sha) >= 7
        assert all(ch in "0123456789abcdef" for ch in cp.sha)
        assert "T" in cp.timestamp  # ISO-8601 committer date


def test_list_checkpoints_empty_store(tmp_path: Path) -> None:
    assert list_checkpoints(tmp_path / "fresh") == []
    assert "no checkpoints" in format_checkpoints([])


def test_format_checkpoints_numbers_and_marks_current(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _seed_workspace(ws)
    text = format_checkpoints(list_checkpoints(ws))
    assert "1." in text and "4." in text
    assert "(current)" in text
    assert text.index("three") < text.index("two") < text.index("one")


def test_rewind_by_ordinal_restores_file_content(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    shas = _seed_workspace(ws)
    # ordinal 2 == checkpoint "two" == workspace before turn two's edit
    result = rewind_to("2", workspace=ws)
    assert result.ok and result.files_restored
    assert result.sha == shas["two"]
    assert (ws / "a.txt").read_text() == "v1"
    # discarded checkpoints are gone from the log (revert_last semantics)
    assert [c.label for c in list_checkpoints(ws)] == ["two", "one", "initial"]


def test_rewind_by_sha_restores_file_content(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    shas = _seed_workspace(ws)
    result = rewind_to(shas["one"], workspace=ws)
    assert result.ok and result.files_restored
    assert not (ws / "a.txt").exists()  # checkpoint "one" predates a.txt


def test_rewind_truncates_window_on_unique_label_match(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _seed_workspace(ws)
    session = BrainSession(brain=_IdleBrain(), model="big")
    session.window.extend(
        [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "r1"},
            {"role": "user", "content": "two"},
            {"role": "assistant", "content": "r2"},
            {"role": "user", "content": "three"},
            {"role": "assistant", "content": "r3"},
        ]
    )
    result = rewind_to("2", session=session, workspace=ws)  # checkpoint "two"
    assert result.ok and result.window_truncated
    assert result.dropped_messages == 4
    assert session.window == [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "r1"},
    ]
    assert "dropped 4 message(s)" in result.message


def test_rewind_window_unchanged_when_label_ambiguous(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _seed_workspace(ws)
    session = BrainSession(brain=_IdleBrain(), model="big")
    session.window.extend(
        [
            {"role": "user", "content": "two"},
            {"role": "user", "content": "two"},
        ]
    )
    before = list(session.window)
    result = rewind_to("2", session=session, workspace=ws)
    assert result.ok and result.files_restored
    assert not result.window_truncated
    assert session.window == before
    assert "conversation window unchanged" in result.message


def test_rewind_window_unchanged_when_no_matching_turn(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _seed_workspace(ws)
    session = BrainSession(brain=_IdleBrain(), model="big")
    session.window.append({"role": "user", "content": "unrelated"})
    result = rewind_to("2", session=session, workspace=ws)
    assert result.ok and not result.window_truncated
    assert "conversation window unchanged" in result.message


def test_rewind_to_current_checkpoint_discards_uncommitted_edits(
    tmp_path: Path,
) -> None:
    """Snapshots land at turn START, so /rewind 1 must hard-reset the
    edits made during the latest turn — not no-op (Codex review)."""
    ws = tmp_path / "ws"
    _seed_workspace(ws)
    # _seed_workspace leaves a.txt == v3 UNCOMMITTED (turn three's edit);
    # the newest checkpoint ("three") committed v2 — turn-start state.
    result = rewind_to("1", workspace=ws)
    assert result.ok and result.files_restored
    assert "discarded" in result.message
    assert (ws / "a.txt").read_text() == "v2"  # checkpoint state restored


def test_rewind_bad_target_errors_politely(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _seed_workspace(ws)
    result = rewind_to("99", workspace=ws)
    assert not result.ok
    assert "no checkpoint '99'" in result.message
    result = rewind_to("zzzzzzz", workspace=ws)
    assert not result.ok and "no checkpoint" in result.message


def test_rewind_empty_store_degrades_with_message(tmp_path: Path) -> None:
    result = rewind_to("1", workspace=tmp_path / "fresh")
    assert not result.ok
    assert "no checkpoints" in result.message


async def test_dispatch_no_args_lists_checkpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    _seed_workspace(ws)
    monkeypatch.setenv("CORLINMAN_AGENT_WORKSPACE", str(ws))
    text = await _dispatch_text(_StubApp(), "/rewind")
    assert "workspace checkpoints" in text
    assert "three" in text and "(current)" in text


async def test_dispatch_restores_by_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "ws"
    _seed_workspace(ws)
    monkeypatch.setenv("CORLINMAN_AGENT_WORKSPACE", str(ws))
    text = await _dispatch_text(_StubApp(), "/rewind 2")
    assert "rewound to checkpoint" in text
    assert (ws / "a.txt").read_text() == "v1"


async def test_dispatch_bad_index_is_polite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    _seed_workspace(ws)
    monkeypatch.setenv("CORLINMAN_AGENT_WORKSPACE", str(ws))
    text = await _dispatch_text(_StubApp(), "/rewind 99")
    assert "no checkpoint '99'" in text


async def test_dispatch_attach_mode_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORLINMAN_AGENT_WORKSPACE", str(tmp_path / "ws"))
    text = await _dispatch_text(_StubApp(embedded=False), "/rewind")
    assert "embedded" in text


async def test_dispatch_unavailable_store_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git 'missing' → the store is unavailable → /rewind says so."""
    monkeypatch.setenv("CORLINMAN_AGENT_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setattr("corlinman_agent.coding._snapshot.shutil.which", lambda _name: None)
    text = await _dispatch_text(_StubApp(), "/rewind")
    assert "no checkpoints" in text


# ---------------------------------------------------------------------------
# Turn-keyed rewind (ABSORB_MATRIX Dim 11 slice c)
# ---------------------------------------------------------------------------


def test_checkpoint_parses_turn_tag_and_legacy_labels(tmp_path: Path) -> None:
    """A ``[turn:<id>]``-tagged subject yields Checkpoint.turn_id + the clean
    label; legacy snapshots (no tag) keep turn_id=None (label-match fallback)."""
    ws = tmp_path / "ws"
    snapshot(ws, "legacy turn")  # old format
    snapshot(ws, "tagged turn", turn_id=42)
    cps = list_checkpoints(ws)
    assert cps[0].turn_id == 42 and cps[0].label == "tagged turn"
    assert cps[1].turn_id is None and cps[1].label == "legacy turn"


def test_rewind_skip_window_leaves_window_and_note_alone(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _seed_workspace(ws)
    session = BrainSession(brain=_IdleBrain(), model="big")
    session.window.extend(
        [
            {"role": "user", "content": "two"},
            {"role": "assistant", "content": "r"},
        ]
    )
    result = rewind_to("2", session=session, workspace=ws, skip_window=True)
    assert result.ok and not result.window_truncated
    assert len(session.window) == 2  # untouched
    assert "window unchanged" not in result.message  # caller owns the note


async def test_cmd_rewind_prefers_turn_keyed_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a tagged checkpoint + an app exposing replay_window_before, the
    window is rebuilt from the journal (exact), not label-matched."""
    ws = tmp_path / "ws"
    snapshot(ws, "one", turn_id=101)
    (ws / "f.txt").write_text("x")
    snapshot(ws, "two", turn_id=202)
    monkeypatch.setenv("CORLINMAN_AGENT_WORKSPACE", str(ws))

    calls: list[int] = []

    class _TurnApp(_StubApp):
        async def replay_window_before(self, turn_id: int) -> int:
            calls.append(turn_id)
            return 3

    text = await _dispatch_text(_TurnApp(), "/rewind 2")
    assert calls == [101]  # rebuilt strictly-before the chosen checkpoint's turn
    assert "rebuilt from the journal: 3 message(s)" in text
    assert "turns before turn 101" in text


async def test_cmd_rewind_turn_keyed_journal_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    snapshot(ws, "one", turn_id=7)
    monkeypatch.setenv("CORLINMAN_AGENT_WORKSPACE", str(ws))

    class _NoJournalApp(_StubApp):
        async def replay_window_before(self, turn_id: int) -> None:
            return None  # attach mode / journal missing

    text = await _dispatch_text(_NoJournalApp(), "/rewind 1")
    assert "window unchanged (journal unavailable)" in text


async def test_cmd_rewind_legacy_checkpoint_falls_back_to_label_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy (untagged) checkpoint keeps today's label-match path even when
    the app could replay the journal."""
    ws = tmp_path / "ws"
    _seed_workspace(ws)  # legacy snapshots, no turn tags
    monkeypatch.setenv("CORLINMAN_AGENT_WORKSPACE", str(ws))

    class _TurnApp(_StubApp):
        async def replay_window_before(self, turn_id: int) -> int:  # pragma: no cover
            raise AssertionError("must not be called for a legacy checkpoint")

    app = _TurnApp()
    app.session.window.extend(
        [
            {"role": "user", "content": "two"},
            {"role": "assistant", "content": "r"},
        ]
    )
    text = await _dispatch_text(app, "/rewind 2")
    assert "dropped 2 message(s)" in text  # label match did the truncation


async def test_cmd_rewind_turn_keyed_failure_falls_back_to_label_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the journal rebuild degrades (None), the label-match fallback
    still truncates the window (Codex #105 / workflow finding) — the old
    behavior left the window untouched with skip_window already applied."""
    ws = tmp_path / "ws"
    snapshot(ws, "two", turn_id=42)
    monkeypatch.setenv("CORLINMAN_AGENT_WORKSPACE", str(ws))

    class _DegradedApp(_StubApp):
        async def replay_window_before(self, turn_id: int) -> None:
            return None  # journal unavailable / foreign turn / stub backend

    app = _DegradedApp()
    app.session.window.extend(
        [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "r1"},
            {"role": "user", "content": "two"},
            {"role": "assistant", "content": "r2"},
        ]
    )
    text = await _dispatch_text(app, "/rewind 1")
    # Label "two" uniquely matches the second user message → truncated there.
    assert [m["content"] for m in app.session.window] == ["one", "r1"]
    assert "label match" in text


def test_user_text_starting_with_turn_tag_is_not_parsed_as_tag(tmp_path: Path) -> None:
    """User text beginning with ``[turn:99]`` must not masquerade as a
    journal tag on an UNTAGGED snapshot (Codex #105) — the sanitiser
    neutralizes the prefix at write time."""
    ws = tmp_path / "ws"
    snapshot(ws, "[turn:99] tricky prompt")  # untagged legacy-style snapshot
    cps = list_checkpoints(ws)
    assert cps[0].turn_id is None
    assert "tricky prompt" in cps[0].label


def test_tagged_snapshot_with_turn_like_user_text_keeps_real_tag(tmp_path: Path) -> None:
    """A tagged snapshot whose USER text also starts with a fake tag parses
    the REAL tag and keeps the neutralized user text as the label."""
    ws = tmp_path / "ws"
    snapshot(ws, "[turn:99] tricky prompt", turn_id=7)
    cps = list_checkpoints(ws)
    assert cps[0].turn_id == 7
    assert "tricky prompt" in cps[0].label
