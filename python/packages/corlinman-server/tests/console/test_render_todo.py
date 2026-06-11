"""Renderer checklist for ``todo_write`` — claude-code TodoWrite parity."""

from __future__ import annotations

import io
import json
from typing import Any

from corlinman_server.console.events import TextDelta, ToolStarted
from corlinman_server.console.render import TODO_TOOL_NAMES, Renderer
from rich.console import Console

_MODEL = "test-model"
_SESSION = "console:test"


def _make_renderer(**kwargs: Any) -> tuple[Renderer, io.StringIO]:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120)
    renderer = Renderer(console, **kwargs)
    renderer.start_turn()
    return renderer, buf


def _feed(renderer: Renderer, ev: TextDelta | ToolStarted) -> None:
    renderer.on_event(ev, model=_MODEL, session_key=_SESSION)


def _todo_event(todos: list[dict[str, str]], call_id: str = "c1") -> ToolStarted:
    return ToolStarted(
        tool="todo_write",
        call_id=call_id,
        args_json=json.dumps({"todos": todos}).encode(),
    )


_TODOS = [
    {"content": "Read the file", "activeForm": "Reading the file", "status": "completed"},
    {"content": "Run the tests", "activeForm": "Running the tests", "status": "in_progress"},
    {"content": "Write the docs", "activeForm": "Writing the docs", "status": "pending"},
]


def test_constant_matches_agent_tool_name() -> None:
    # Guard against drift: the renderer's literal must keep naming the
    # tool the agent actually advertises in BUILTIN_TOOLS.
    from corlinman_agent.coding.todo import TODO_WRITE_TOOL

    assert TODO_WRITE_TOOL in TODO_TOOL_NAMES


def test_checklist_markers_and_active_form() -> None:
    renderer, buf = _make_renderer()
    _feed(renderer, _todo_event(_TODOS))
    out = buf.getvalue()
    assert "☒ Read the file" in out
    assert "◐ Running the tests" in out  # activeForm, not content
    assert "☐ Write the docs" in out
    # The checklist replaces the generic "◐ todo_write …" line.
    assert "todo_write" not in out


def test_identical_list_skips_rerender() -> None:
    renderer, buf = _make_renderer()
    _feed(renderer, _todo_event(_TODOS, call_id="c1"))
    first = buf.getvalue()
    _feed(renderer, _todo_event(_TODOS, call_id="c2"))
    assert buf.getvalue() == first


def test_changed_list_rerenders() -> None:
    renderer, buf = _make_renderer()
    _feed(renderer, _todo_event(_TODOS, call_id="c1"))
    advanced = [dict(t) for t in _TODOS]
    advanced[1]["status"] = "completed"
    advanced[2]["status"] = "in_progress"
    _feed(renderer, _todo_event(advanced, call_id="c2"))
    out = buf.getvalue()
    assert "☒ Run the tests" in out
    assert "◐ Writing the docs" in out


def test_malformed_args_fall_back_to_generic_line() -> None:
    for args_json in (
        b"not json",
        b"[]",
        json.dumps({"todos": "nope"}).encode(),
        json.dumps({"todos": []}).encode(),
        json.dumps({"todos": [{"content": "x", "status": "bogus"}]}).encode(),
    ):
        renderer, buf = _make_renderer()
        ev = ToolStarted(tool="todo_write", call_id="c1", args_json=args_json)
        _feed(renderer, ev)
        out = buf.getvalue()
        assert "◐ todo_write" in out, args_json
        assert "☐" not in out and "☒" not in out


def test_mid_stream_text_gets_line_break_first() -> None:
    renderer, buf = _make_renderer()
    _feed(renderer, TextDelta(text="thinking out loud"))
    _feed(renderer, _todo_event(_TODOS))
    out = buf.getvalue()
    assert out.startswith("thinking out loud\n")
    assert "☐ Write the docs" in out


def test_tool_progress_off_stays_silent() -> None:
    renderer, buf = _make_renderer(tool_progress="off")
    _feed(renderer, _todo_event(_TODOS))
    assert buf.getvalue() == ""


def test_new_mode_repeat_bookkeeping_survives_checklist() -> None:
    # After a checklist, a different tool must still print, and a
    # consecutive repeat of *that* tool must still be deduped.
    renderer, buf = _make_renderer(tool_progress="new")
    _feed(renderer, _todo_event(_TODOS))
    other = ToolStarted(tool="read_file", call_id="c2", args_json=b'{"path": "a.py"}')
    _feed(renderer, other)
    out = buf.getvalue()
    assert "◐ read_file" in out
    _feed(renderer, ToolStarted(tool="read_file", call_id="c3", args_json=b'{"path": "a.py"}'))
    assert buf.getvalue() == out  # consecutive repeat deduped
