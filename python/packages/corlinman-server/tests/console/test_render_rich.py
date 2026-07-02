"""rich-UI renderer path (TTY): spinner + live markdown + tool blocks.

These drive realistic event sequences through ``Renderer(rich_ui=True)`` on
a forced-terminal console writing to a buffer, asserting the renderer never
crashes, always tears its single Live down (no dangling spinner/markdown
widget), keeps ``rich_ui`` on (no silent fallback), and surfaces the
expected content. The raw path is covered by the existing console suite.
"""

from __future__ import annotations

import io
import json

import pytest
from corlinman_server.console.events import (
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnDone,
    TurnError,
)
from corlinman_server.console.render import Renderer

rich_console = pytest.importorskip("rich.console")


def _term() -> tuple[Renderer, io.StringIO]:
    buf = io.StringIO()
    console = rich_console.Console(
        force_terminal=True, color_system="truecolor", file=buf, width=80
    )
    r = Renderer(console, tool_progress="all", rich_ui=True)
    assert r.rich_ui is True
    return r, buf


def _drive(r: Renderer, events: list) -> None:
    r.start_turn()
    for ev in events:
        r.on_event(ev, model="cornna", session_key="console:test")


def test_text_turn_renders_markdown_and_tears_down() -> None:
    r, buf = _term()
    _drive(
        r,
        [
            TextDelta("# Title\n\n"),
            TextDelta("Some **bold** and a list:\n\n"),
            TextDelta("- one\n- two\n"),
            TurnDone(total_tokens=42),
        ],
    )
    out = buf.getvalue()
    # No dangling Live; never fell back to raw.
    assert r._live is None
    assert r.rich_ui is True
    # Heading text + list items + footer reached the terminal.
    assert "Title" in out
    assert "one" in out and "two" in out
    assert "cornna" in out and "42 tok" in out


def test_tool_block_sequence() -> None:
    r, buf = _term()
    _drive(
        r,
        [
            TextDelta("Let me check.\n"),
            ToolStarted(tool="web_search", args_json=b'{"query":"hi"}'),
            ToolFinished(tool="web_search", duration_ms=1240),
            TextDelta("Done — found it.\n"),
            TurnDone(total_tokens=10),
        ],
    )
    out = buf.getvalue()
    assert r._live is None
    assert r.rich_ui is True
    assert "web_search" in out
    # claude-code-style start (⏺) + result (⎿) markers.
    assert "⏺" in out
    assert "⎿" in out


def test_tool_error_marks_failure() -> None:
    r, buf = _term()
    _drive(
        r,
        [
            ToolStarted(tool="web_fetch", args_json=b"{}"),
            ToolFinished(
                tool="web_fetch", duration_ms=20, is_error=True, error_summary="boom"
            ),
            TurnDone(),
        ],
    )
    out = buf.getvalue()
    assert r._live is None
    assert "web_fetch" in out
    assert "boom" in out


def test_todo_checklist_in_rich_mode() -> None:
    r, buf = _term()
    todos = {
        "todos": [
            {"content": "first task", "status": "completed"},
            {"content": "second task", "status": "in_progress",
             "activeForm": "doing second"},
            {"content": "third task", "status": "pending"},
        ]
    }
    _drive(
        r,
        [
            ToolStarted(tool="todo_write", args_json=json.dumps(todos).encode()),
            TurnDone(),
        ],
    )
    out = buf.getvalue()
    assert r._live is None
    assert "first task" in out
    assert "doing second" in out  # in_progress shows activeForm
    assert "third task" in out


def test_turn_error_tears_down_live() -> None:
    r, buf = _term()
    _drive(
        r,
        [
            TextDelta("partial answer "),
            TurnError(reason="rate_limit", message="429 slow down"),
        ],
    )
    out = buf.getvalue()
    assert r._live is None
    assert r.rich_ui is True
    assert "rate_limit" in out


def test_cancelled_turn_renders_interrupted() -> None:
    r, buf = _term()
    _drive(
        r,
        [
            TextDelta("thinking..."),
            TurnError(message="cancelled"),
        ],
    )
    out = buf.getvalue()
    assert r._live is None
    assert "interrupted" in out


def test_rich_failure_falls_back_to_raw() -> None:
    """A rich-path exception must flip ``rich_ui`` off and keep rendering on
    the raw path — never strand the REPL."""
    r, buf = _term()
    r.start_turn()

    # Sabotage the live machinery so the next rich render raises.
    def _boom() -> None:
        raise RuntimeError("simulated rich failure")

    r._ensure_text_live = _boom  # type: ignore[method-assign]
    r.on_event(TextDelta("hello after fallback\n"), model="cornna", session_key="s")
    # Fell back, and the raw path still emitted the text.
    assert r.rich_ui is False
    assert r._live is None
    r.on_event(TurnDone(total_tokens=3), model="cornna", session_key="s")
    out = buf.getvalue()
    assert "hello after fallback" in out
    assert "3 tok" in out


def test_empty_text_delta_is_noop() -> None:
    r, _ = _term()
    r.start_turn()
    assert r._live_kind == "spin"  # spinner running
    r.on_event(TextDelta(""), model="cornna", session_key="s")
    # empty delta neither starts a text Live nor paints an empty block
    assert r._live_kind == "spin"
    r.on_event(TextDelta("real text\n"), model="cornna", session_key="s")
    assert r._live_kind == "text"
    r.on_event(TurnDone(), model="cornna", session_key="s")
    assert r._live is None


def test_stop_live_clears_even_if_stop_raises() -> None:
    r, _ = _term()

    class _Boom:
        def stop(self) -> None:
            raise RuntimeError("stop failed")

    r._live = _Boom()
    r._live_kind = "spin"
    r._working = object()  # type: ignore[assignment]
    r._stop_live()  # must not raise
    assert r._live is None and r._live_kind is None and r._working is None


def test_long_reply_caps_reparse_but_renders_at_end() -> None:
    r, buf = _term()
    r.start_turn()
    big = "word " * 6000  # ~30k chars, past the live-reparse cap
    r.on_event(TextDelta(big), model="cornna", session_key="s")
    r.on_event(TurnDone(total_tokens=1), model="cornna", session_key="s")
    out = buf.getvalue()
    assert r._live is None
    assert "word" in out  # final flush renders the whole block


def test_working_spinner_renders_frame_label_and_elapsed() -> None:
    """The live spinner renderable appends the spinner frame (a ``Text``), the
    label, and the elapsed/interrupt hint without crashing — regression guard
    for the ``Spinner.render() -> RenderableType`` vs ``Text.append_text``
    (expects ``Text``) mismatch on ``render.py``. mypy is the primary gate;
    this locks the runtime render path too."""
    from corlinman_server.console.render import _Working

    buf = io.StringIO()
    console = rich_console.Console(
        force_terminal=True, color_system="truecolor", file=buf, width=80
    )
    console.print(_Working("正在思考", start=0.0))
    out = buf.getvalue()
    assert "正在思考" in out  # label rendered → __rich_console__ ran to completion
    assert "中断" in out  # elapsed + Ctrl-C hint line appended after the frame


def test_non_terminal_defaults_to_raw() -> None:
    buf = io.StringIO()
    console = rich_console.Console(file=buf, width=80)  # not a terminal
    r = Renderer(console, tool_progress="all")  # rich_ui auto-detects
    assert r.rich_ui is False
