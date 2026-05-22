"""AWS ``vnd.amazon.eventstream`` decoder tests.

We hand-encode frames (the decoder is the unit under test, so the encoder
lives here in the test) and verify: clean single-frame decode, multi-frame
decode, **split** frames arriving across multiple ``feed`` calls, and CRC32
rejection of a corrupted message.
"""

from __future__ import annotations

import struct
import zlib

import pytest
from corlinman_providers._aws_eventstream import (
    EventStreamDecoder,
    EventStreamError,
)


def _encode_string_header(name: str, value: str) -> bytes:
    """Encode one ``[name_len][name][type=7][value_len][value]`` header."""
    name_b = name.encode()
    value_b = value.encode()
    return (
        bytes([len(name_b)])
        + name_b
        + bytes([7])
        + struct.pack(">H", len(value_b))
        + value_b
    )


def encode_message(headers: dict[str, str], payload: bytes) -> bytes:
    """Encode a complete AWS event-stream message with valid CRC32s."""
    headers_blob = b"".join(_encode_string_header(k, v) for k, v in headers.items())
    headers_len = len(headers_blob)
    # prelude (8) + headers + payload + message-crc (4)
    total_len = 8 + 4 + headers_len + len(payload) + 4
    prelude = struct.pack(">II", total_len, headers_len)
    prelude_crc = zlib.crc32(prelude) & 0xFFFFFFFF
    head = prelude + struct.pack(">I", prelude_crc)
    without_crc = head + headers_blob + payload
    message_crc = zlib.crc32(without_crc) & 0xFFFFFFFF
    return without_crc + struct.pack(">I", message_crc)


def test_decode_single_chunk_message() -> None:
    raw = encode_message(
        {":event-type": "chunk", ":message-type": "event"},
        b'{"bytes":"eHl6"}',
    )
    dec = EventStreamDecoder()
    msgs = dec.feed(raw)
    assert len(msgs) == 1
    assert msgs[0].event_type == "chunk"
    assert msgs[0].message_type == "event"
    assert msgs[0].payload == b'{"bytes":"eHl6"}'


def test_decode_multiple_messages_in_one_feed() -> None:
    raw = encode_message({":event-type": "chunk"}, b"a") + encode_message(
        {":event-type": "chunk"}, b"b"
    )
    dec = EventStreamDecoder()
    msgs = dec.feed(raw)
    assert [m.payload for m in msgs] == [b"a", b"b"]


def test_decode_message_split_across_feeds() -> None:
    """A message arriving in fragments is held until fully buffered."""
    raw = encode_message({":event-type": "chunk"}, b"hello world payload")
    dec = EventStreamDecoder()
    # First few bytes — not even the prelude is complete.
    assert dec.feed(raw[:5]) == []
    # Up to mid-message — still incomplete.
    assert dec.feed(raw[5:-3]) == []
    # Final tail — now the whole frame is available.
    msgs = dec.feed(raw[-3:])
    assert len(msgs) == 1
    assert msgs[0].payload == b"hello world payload"


def test_corrupted_message_crc_raises() -> None:
    raw = bytearray(encode_message({":event-type": "chunk"}, b"payload"))
    raw[-1] ^= 0xFF  # flip a bit in the trailing message CRC
    dec = EventStreamDecoder()
    with pytest.raises(EventStreamError, match="CRC32"):
        dec.feed(bytes(raw))


def test_exception_frame_headers_exposed() -> None:
    raw = encode_message(
        {":message-type": "exception", ":exception-type": "ThrottlingException"},
        b'{"message":"slow down"}',
    )
    dec = EventStreamDecoder()
    msg = dec.feed(raw)[0]
    assert msg.message_type == "exception"
    assert msg.exception_type == "ThrottlingException"
