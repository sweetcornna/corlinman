"""Internal event → ConsoleEvent normalisation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from corlinman_server.console.events import (
    ReasoningDelta,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnDone,
    TurnError,
    args_preview,
    from_internal_events,
)
from corlinman_server.gateway_api import (
    DoneEvent,
    ErrorEvent,
    InternalChatError,
    TokenDeltaEvent,
    ToolCallEvent,
    ToolResultEvent,
    Usage,
)


async def _stream(items: list[Any]) -> AsyncIterator[Any]:
    for item in items:
        yield item


async def _collect(items: list[Any]) -> list[Any]:
    return [ev async for ev in from_internal_events(_stream(items))]


async def test_token_and_done_mapping() -> None:
    out = await _collect(
        [
            TokenDeltaEvent(text="he"),
            TokenDeltaEvent(text="llo"),
            DoneEvent(
                finish_reason="stop",
                usage=Usage(prompt_tokens=3, completion_tokens=5, total_tokens=8),
            ),
        ]
    )
    assert out == [
        TextDelta(text="he"),
        TextDelta(text="llo"),
        TurnDone(
            finish_reason="stop",
            prompt_tokens=3,
            completion_tokens=5,
            total_tokens=8,
        ),
    ]


async def test_reasoning_tokens_split_out() -> None:
    out = await _collect(
        [
            TokenDeltaEvent(text="thinking…", is_reasoning=True),
            TokenDeltaEvent(text="answer"),
            DoneEvent(finish_reason="stop", usage=None),
        ]
    )
    assert out[0] == ReasoningDelta(text="thinking…")
    assert out[1] == TextDelta(text="answer")


async def test_tool_call_and_result_pairing() -> None:
    out = await _collect(
        [
            ToolCallEvent(
                plugin="", tool="run_shell", args_json=b'{"command":"ls"}', call_id="c1"
            ),
            ToolResultEvent(
                plugin="",
                tool="",  # older agents may omit — book fills it from call_id
                call_id="c1",
                duration_ms=1200,
                is_error=False,
                error_summary="",
            ),
            DoneEvent(finish_reason="stop", usage=None),
        ]
    )
    started, finished = out[0], out[1]
    assert isinstance(started, ToolStarted) and started.tool == "run_shell"
    assert isinstance(finished, ToolFinished)
    assert finished.tool == "run_shell"
    assert finished.duration_ms == 1200


async def test_error_terminal() -> None:
    out = await _collect(
        [ErrorEvent(error=InternalChatError(reason="billing", message="no credit"))]
    )
    assert out == [TurnError(reason="billing", message="no credit")]


async def test_missing_terminal_synthesised() -> None:
    out = await _collect([TokenDeltaEvent(text="hi")])
    assert isinstance(out[-1], TurnDone)


def test_args_preview_picks_informative_key() -> None:
    assert args_preview(b'{"command": "ls -la /tmp"}') == "ls -la /tmp"
    assert args_preview(b'{"path": "/etc/hosts", "other": 1}') == "/etc/hosts"


def test_args_preview_truncates_and_flattens() -> None:
    preview = args_preview(b'{"query": "' + b"a b " * 60 + b'"}', limit=40)
    assert len(preview) <= 40
    assert "\n" not in preview


def test_args_preview_tolerates_garbage() -> None:
    assert args_preview(b"not json") == "not json"
    assert args_preview(b"") == ""


def test_turn_error_cancelled_flag() -> None:
    assert TurnError(reason="unknown", message="cancelled").is_cancelled
    assert not TurnError(reason="unknown", message="boom").is_cancelled
