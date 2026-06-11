"""SSE payload parsing — must track ``gateway/routes/chat.py`` shapes."""

from __future__ import annotations

import json

from corlinman_server.console.attach import parse_sse_data
from corlinman_server.console.events import (
    TextDelta,
    ToolStarted,
    TurnDone,
    TurnError,
)


def test_token_delta_chunk() -> None:
    data = json.dumps(
        {
            "id": "chat-1",
            "object": "chat.completion.chunk",
            "model": "m",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "hel"},
                    "finish_reason": None,
                }
            ],
        }
    )
    assert parse_sse_data(data) == [TextDelta(text="hel")]


def test_tool_call_delta_chunk() -> None:
    data = json.dumps(
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_abc",
                                "type": "function",
                                "function": {
                                    "name": "run_shell",
                                    "arguments": '{"command": "ls"}',
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        }
    )
    (ev,) = parse_sse_data(data)
    assert isinstance(ev, ToolStarted)
    assert ev.tool == "run_shell"
    assert ev.call_id == "call_abc"
    assert ev.args_json == b'{"command": "ls"}'


def test_finish_chunk() -> None:
    data = json.dumps(
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    )
    assert parse_sse_data(data) == [TurnDone(finish_reason="stop")]


def test_error_frame() -> None:
    data = json.dumps(
        {
            "error": {
                "code": "upstream_error",
                "reason": "rate_limit",
                "message": "slow down",
            }
        }
    )
    assert parse_sse_data(data) == [
        TurnError(reason="rate_limit", message="slow down")
    ]


def test_error_frame_without_reason_uses_code() -> None:
    data = json.dumps({"error": {"code": "boom", "message": "m"}})
    (ev,) = parse_sse_data(data)
    assert isinstance(ev, TurnError) and ev.reason == "boom"


def test_malformed_payload_skipped() -> None:
    assert parse_sse_data("{not json") == []
    assert parse_sse_data('"a string"') == []
    assert parse_sse_data(json.dumps({"choices": [{"delta": {}}]})) == []
