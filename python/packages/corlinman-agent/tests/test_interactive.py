"""Tests for the ``ask_user`` interactive tool.

Pinned behaviours:

* OpenAI tool descriptor carries the required fields.
* :func:`dispatch_ask_user` returns the stub envelope with the question
  echoed back.
* An empty question yields an error envelope (so a buggy call surfaces
  cleanly instead of silently stalling the round).
* Option / multiple flags round-trip through the parsed envelope.
"""

from __future__ import annotations

import json

import pytest
from corlinman_agent.interactive import (
    ASK_USER_TOOL,
    ask_user_tool_schema,
    dispatch_ask_user,
    parse_ask_user_args,
)


def test_ask_user_tool_name_constant() -> None:
    assert ASK_USER_TOOL == "ask_user"


def test_ask_user_schema_shape() -> None:
    schema = ask_user_tool_schema()
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == "ask_user"
    # Required-field listing is the contract observers (model + tests)
    # rely on; pin the exact set.
    params = fn["parameters"]
    assert params["required"] == ["question"]
    # All optional fields surface as JSON-Schema properties so the model
    # knows it can pass them.
    assert {"question", "options", "multiple"} <= set(params["properties"])
    # ``options`` is an array of strings — Telegram's inline keyboard
    # button labels.
    assert params["properties"]["options"]["type"] == "array"
    assert params["properties"]["options"]["items"]["type"] == "string"


def test_dispatch_ask_user_returns_stub_envelope() -> None:
    """Happy path: the question echo + the awaiting marker are present."""
    args = json.dumps({"question": "should I overwrite README.md?"}).encode()
    out = dispatch_ask_user(args)
    obj = json.loads(out)
    assert obj["ok"] is True
    assert obj["status"] == "awaiting_user_reply"
    assert obj["question"] == "should I overwrite README.md?"
    assert obj["options"] == []
    assert obj["multiple"] is False


def test_dispatch_ask_user_propagates_options() -> None:
    args = json.dumps(
        {
            "question": "pick one",
            "options": ["yes", "no", "let me think"],
            "multiple": False,
        }
    ).encode()
    obj = json.loads(dispatch_ask_user(args))
    assert obj["ok"] is True
    assert obj["options"] == ["yes", "no", "let me think"]


def test_dispatch_ask_user_caps_options_at_eight() -> None:
    """Telegram's inline keyboard pattern + UX both want ≤8 options."""
    args = json.dumps(
        {"question": "many?", "options": [f"opt_{i}" for i in range(20)]}
    ).encode()
    obj = json.loads(dispatch_ask_user(args))
    assert len(obj["options"]) == 8
    assert obj["options"][0] == "opt_0"
    assert obj["options"][-1] == "opt_7"


def test_dispatch_ask_user_multiple_flag_passes_through() -> None:
    args = json.dumps(
        {"question": "select many", "options": ["a", "b"], "multiple": True}
    ).encode()
    obj = json.loads(dispatch_ask_user(args))
    assert obj["multiple"] is True


def test_dispatch_ask_user_empty_question_returns_error() -> None:
    """A blank question is a programmer error — surface it cleanly."""
    obj = json.loads(dispatch_ask_user(b'{"question": ""}'))
    assert obj["ok"] is False
    assert "question" in obj["error"]


def test_dispatch_ask_user_missing_question_returns_error() -> None:
    """Same shape when ``question`` is absent entirely."""
    obj = json.loads(dispatch_ask_user(b"{}"))
    assert obj["ok"] is False


def test_dispatch_ask_user_malformed_json_returns_error() -> None:
    """A non-JSON args blob falls into the empty-question branch."""
    obj = json.loads(dispatch_ask_user(b"not json"))
    assert obj["ok"] is False


def test_dispatch_ask_user_accepts_str_args() -> None:
    """Defensive: a unit test or shim may feed a str instead of bytes."""
    out = dispatch_ask_user('{"question": "via str"}')
    obj = json.loads(out)
    assert obj["ok"] is True
    assert obj["question"] == "via str"


def test_parse_ask_user_args_normalises_blank_options() -> None:
    """Whitespace-only / empty option entries are skipped, not echoed."""
    parsed = parse_ask_user_args(
        json.dumps({"question": "x", "options": ["", "  ", "real"]}).encode()
    )
    assert parsed["options"] == ["real"]


def test_parse_ask_user_args_truncates_long_question() -> None:
    long_q = "a" * 5000
    parsed = parse_ask_user_args(
        json.dumps({"question": long_q}).encode()
    )
    # Cap is internal but bounded; assert it shrunk and ends with ellipsis.
    assert len(parsed["question"]) < len(long_q)
    assert parsed["question"].endswith("…")


def test_parse_ask_user_args_handles_non_string_option() -> None:
    """A model that hands us a number / null in the options list shouldn't
    crash the dispatcher — coerce or skip."""
    parsed = parse_ask_user_args(
        json.dumps({"question": "x", "options": [1, None, "ok"]}).encode()
    )
    # Coerced strings + the real option survive (the empty 'None' string
    # is filtered).
    assert "ok" in parsed["options"]


@pytest.mark.parametrize("payload", [b"", b"   ", b"null", b"[]"])
def test_dispatch_ask_user_empty_payloads_return_error(payload: bytes) -> None:
    obj = json.loads(dispatch_ask_user(payload))
    assert obj["ok"] is False
