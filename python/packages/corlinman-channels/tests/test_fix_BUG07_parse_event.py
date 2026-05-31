"""BUG-07 — OneBot parse_event must not raise on a malformed numeric field.

A frame with a non-numeric ``self_id`` (or ``user_id`` / ``message_id`` /
``time``) currently makes :func:`parse_event` raise ``ValueError`` from the
bare ``int()`` coercion. That unwinds ``_pump``, gets caught by the reader
loop's bare except, and the WS is closed + reconnected — message loss and
reconnect churn.

Acceptance: a bad numeric field collapses to a usable event (the numeric
coercion falls back to 0) rather than raising, so the reader loop stays up.
"""

from __future__ import annotations

from corlinman_channels.onebot import (
    MessageEvent,
    UnknownEvent,
    parse_event,
)


def test_non_numeric_self_id_does_not_raise() -> None:
    raw = {
        "post_type": "message",
        "message_type": "private",
        "self_id": "not-a-number",
        "user_id": 12345,
        "message_id": 7,
        "time": 1_700_000_000,
        "message": [{"type": "text", "data": {"text": "hi"}}],
        "raw_message": "hi",
    }
    # Before the fix this raises ValueError: invalid literal for int().
    ev = parse_event(raw)
    assert isinstance(ev, (MessageEvent, UnknownEvent))
    if isinstance(ev, MessageEvent):
        # The bad self_id collapses to 0 — no crash.
        assert ev.self_id == 0
        assert ev.user_id == 12345


def test_non_numeric_time_and_message_id_do_not_raise() -> None:
    raw = {
        "post_type": "message",
        "message_type": "group",
        "self_id": 100,
        "group_id": 9999,
        "user_id": "weird",
        "message_id": "abc",
        "time": "later",
        "message": [{"type": "text", "data": {"text": "yo"}}],
    }
    ev = parse_event(raw)
    assert isinstance(ev, MessageEvent)
    assert ev.self_id == 100
    assert ev.user_id == 0
    assert ev.message_id == 0
    assert ev.time == 0


def test_non_numeric_notice_self_id_does_not_raise() -> None:
    raw = {
        "post_type": "notice",
        "notice_type": "group_increase",
        "self_id": "bad",
        "time": "bad",
    }
    ev = parse_event(raw)
    # Notice events are parsed but unused; they must still not crash.
    assert ev is not None
