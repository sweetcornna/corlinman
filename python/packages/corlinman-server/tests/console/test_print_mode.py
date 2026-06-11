"""``--print`` output contracts — json / stream-json envelopes + --max-turns.

Drives ``ConsoleApp.run_once`` against a scripted fake brain (the
``ScriptedBrain`` pattern from ``test_brain.py``, copied locally — test
modules don't import each other). stdout is the machine channel under
test (``capsys``); the renderer is parked on an in-memory rich console
standing in for the stderr it gets in real print mode.
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from corlinman_server.console.app import OUTPUT_FORMATS, ConsoleApp
from corlinman_server.console.brain import BrainSession
from corlinman_server.console.events import (
    ConsoleEvent,
    ReasoningDelta,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnDone,
    TurnError,
)
from corlinman_server.console.render import Renderer
from corlinman_server.console.router import ModelRouter
from rich.console import Console


class ScriptedBrain:
    """Yields a canned event list per turn; records what it was sent."""

    descriptor = "scripted"

    def __init__(self, script: list[list[ConsoleEvent]]) -> None:
        self._script = list(script)
        self.calls: list[dict] = []

    def run_turn(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        session_key: str,
        cancel: asyncio.Event,
    ) -> AsyncIterator[ConsoleEvent]:
        self.calls.append(
            {
                "model": model,
                "messages": [dict(m) for m in messages],
                "session_key": session_key,
            }
        )
        events = self._script.pop(0)

        async def _gen() -> AsyncIterator[ConsoleEvent]:
            for ev in events:
                yield ev

        return _gen()

    async def aclose(self) -> None:  # pragma: no cover - protocol filler
        pass


def _make_app(
    script: list[list[ConsoleEvent]],
    tmp_path: Path,
    *,
    output_format: str = "text",
    max_turns: int = 0,
) -> ConsoleApp:
    session = BrainSession(brain=ScriptedBrain(script), model="m")
    renderer = Renderer(Console(file=io.StringIO(), force_terminal=False, width=200))
    return ConsoleApp(
        session=session,
        renderer=renderer,
        router=ModelRouter.from_config({}, default_model="m"),
        data_dir=tmp_path,
        embedded=False,
        output_format=output_format,
        max_turns=max_turns,
    )


def _json_lines(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


# ── --output-format json ──────────────────────────────────────────────


async def test_json_success_envelope_is_sole_stdout(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    app = _make_app(
        [
            [
                ToolStarted(tool="bash", call_id="c1", args_json=b'{"command": "ls"}'),
                ToolFinished(tool="bash", call_id="c1", duration_ms=12),
                TextDelta(text="hello "),
                TextDelta(text="world"),
                TurnDone(prompt_tokens=3, completion_tokens=4, total_tokens=7),
            ]
        ],
        tmp_path,
        output_format="json",
    )
    code = await app.run_once("q")
    lines = _json_lines(capsys.readouterr().out)
    assert len(lines) == 1  # ONE object — nothing else may touch stdout
    assert lines[0] == {
        "type": "result",
        "subtype": "success",
        "result": "hello world",
        "session_id": app.session.session_key,
        "model": "m",
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        "num_turns": 1,
        "is_error": False,
        "error": None,
    }
    assert code == 0


async def test_json_error_envelope_and_exit_code(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    app = _make_app(
        [[TurnError(reason="rate_limit", message="429 too many requests")]],
        tmp_path,
        output_format="json",
    )
    code = await app.run_once("q")
    lines = _json_lines(capsys.readouterr().out)
    assert len(lines) == 1
    payload = lines[0]
    assert payload["type"] == "result"
    assert payload["subtype"] == "error"
    assert payload["is_error"] is True
    assert payload["result"] == ""
    assert payload["num_turns"] == 0  # errored turn never committed
    assert payload["usage"] == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    assert payload["error"] == {"reason": "rate_limit", "message": "429 too many requests"}
    assert code == 1


# ── --output-format stream-json ───────────────────────────────────────


async def test_stream_json_line_sequence(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    app = _make_app(
        [
            [
                ReasoningDelta(text="thinking"),
                ToolStarted(
                    tool="read",
                    plugin="fs",
                    call_id="c1",
                    args_json=b'{"path": "/tmp/x"}',
                ),
                ToolFinished(tool="read", plugin="fs", call_id="c1", duration_ms=42),
                TextDelta(text="par"),
                TextDelta(text="ity"),
                TurnDone(prompt_tokens=2, completion_tokens=3, total_tokens=5),
            ]
        ],
        tmp_path,
        output_format="stream-json",
    )
    code = await app.run_once("q")
    lines = _json_lines(capsys.readouterr().out)
    assert [line["type"] for line in lines] == [
        "reasoning_delta",
        "tool_started",
        "tool_finished",
        "text_delta",
        "text_delta",
        "result",
    ]
    assert lines[0] == {"type": "reasoning_delta", "text": "thinking"}
    assert lines[1] == {
        "type": "tool_started",
        "tool": "read",
        "plugin": "fs",
        "call_id": "c1",
        "args": {"path": "/tmp/x"},  # args_json parsed, not raw bytes
    }
    assert lines[2] == {
        "type": "tool_finished",
        "tool": "read",
        "call_id": "c1",
        "duration_ms": 42,
        "is_error": False,
    }
    assert lines[3] == {"type": "text_delta", "text": "par"}
    result = lines[-1]
    assert result["subtype"] == "success"
    assert result["result"] == "parity"
    assert result["session_id"] == app.session.session_key
    assert result["usage"]["total_tokens"] == 5
    assert code == 0


async def test_stream_json_error_still_ends_with_result(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    app = _make_app(
        [
            [
                TextDelta(text="partial"),
                ToolStarted(tool="bash", call_id="c2", args_json=b"not-json"),
                TurnError(reason="billing", message="quota exhausted"),
            ]
        ],
        tmp_path,
        output_format="stream-json",
    )
    code = await app.run_once("q")
    lines = _json_lines(capsys.readouterr().out)
    assert [line["type"] for line in lines] == ["text_delta", "tool_started", "result"]
    assert lines[1]["args"] == "not-json"  # unparseable args fall back to raw string
    assert lines[-1]["subtype"] == "error"
    assert lines[-1]["is_error"] is True
    assert lines[-1]["error"] == {"reason": "billing", "message": "quota exhausted"}
    assert code == 1


# ── text mode (default) regression ────────────────────────────────────


async def test_text_mode_stdout_is_answer_only(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    app = _make_app([[TextDelta(text="4"), TurnDone()]], tmp_path)
    code = await app.run_once("2+2?")
    assert capsys.readouterr().out == "4\n"
    assert code == 0


# ── --max-turns plumbing ──────────────────────────────────────────────


def test_output_formats_constant() -> None:
    assert OUTPUT_FORMATS == ("text", "json", "stream-json")


async def test_max_turns_counts_completed_turns(tmp_path: Path) -> None:
    app = _make_app(
        [
            [TextDelta(text="a"), TurnDone()],
            [TurnError(reason="unknown", message="boom")],
        ],
        tmp_path,
        max_turns=2,
    )
    assert app.max_turns_reached() is False
    await app.run_turn("one")
    assert app.turns_completed == 1
    assert app.max_turns_reached() is False
    await app.run_turn("two")  # errored turns consume budget too
    assert app.turns_completed == 2
    assert app.max_turns_reached() is True


async def test_max_turns_zero_means_unlimited(tmp_path: Path) -> None:
    app = _make_app([[TurnDone()]], tmp_path)
    await app.run_turn("one")
    assert app.turns_completed == 1
    assert app.max_turns_reached() is False
