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


def test_rewind_to_current_checkpoint_is_a_polite_noop(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _seed_workspace(ws)
    result = rewind_to("1", workspace=ws)
    assert not result.ok and not result.files_restored
    assert "already at checkpoint" in result.message
    assert (ws / "a.txt").read_text() == "v3"  # working tree untouched


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
