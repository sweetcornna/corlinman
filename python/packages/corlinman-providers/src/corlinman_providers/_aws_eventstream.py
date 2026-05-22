"""AWS ``vnd.amazon.eventstream`` binary frame decoder.

Bedrock's ``InvokeModelWithResponseStream`` returns its body as a sequence
of ``application/vnd.amazon.eventstream`` messages — the same binary
framing AWS uses for Kinesis / Transcribe / Lex streaming. Each message is:

    +--------------------+   4 bytes  total message length (big-endian)
    +--------------------+   4 bytes  headers byte length (big-endian)
    +--------------------+   4 bytes  prelude CRC32 (of the first 8 bytes)
    +--------------------+   N bytes  headers
    +--------------------+   M bytes  payload
    +--------------------+   4 bytes  message CRC32 (of everything above)

Each header is ``[name_len:1][name][value_type:1][value]``; the only
value-type we need is ``7`` (a UTF-8 string, length-prefixed by a 2-byte
big-endian count) — Bedrock's framing headers (``:event-type``,
``:content-type``, ``:message-type``, ``:exception-type``) are all strings.

For a Bedrock chunk message the payload is a JSON object ``{"bytes":
"<base64>"}`` whose decoded ``bytes`` is the per-vendor model output JSON
(for Anthropic-on-Bedrock that is a Messages-API SSE event).

We implement a streaming decoder that accepts arbitrary byte chunks (as
they arrive from ``httpx.aiter_bytes``) and yields whole
:class:`EventStreamMessage` values once each is fully buffered. CRC32 is
verified — a mismatch raises :class:`EventStreamError` so a corrupted
stream fails loudly rather than feeding the model garbage.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field

__all__ = ["EventStreamDecoder", "EventStreamError", "EventStreamMessage"]

_PRELUDE_LEN = 12  # 3 * uint32
_HEADER_STRING_TYPE = 7


class EventStreamError(RuntimeError):
    """Raised on a malformed frame or a CRC32 mismatch."""


@dataclass(slots=True)
class EventStreamMessage:
    """One decoded event-stream message."""

    headers: dict[str, str] = field(default_factory=dict)
    payload: bytes = b""

    @property
    def event_type(self) -> str | None:
        """The ``:event-type`` header — ``"chunk"`` for Bedrock output."""
        return self.headers.get(":event-type")

    @property
    def message_type(self) -> str | None:
        """The ``:message-type`` header — ``"event"`` or ``"exception"``."""
        return self.headers.get(":message-type")

    @property
    def exception_type(self) -> str | None:
        """The ``:exception-type`` header on an error frame, if any."""
        return self.headers.get(":exception-type")


def _parse_headers(raw: bytes) -> dict[str, str]:
    """Parse the headers blob — only the string value-type is decoded."""
    headers: dict[str, str] = {}
    offset = 0
    n = len(raw)
    while offset < n:
        name_len = raw[offset]
        offset += 1
        name = raw[offset : offset + name_len].decode("utf-8", "replace")
        offset += name_len
        value_type = raw[offset]
        offset += 1
        if value_type == _HEADER_STRING_TYPE:
            (value_len,) = struct.unpack_from(">H", raw, offset)
            offset += 2
            value = raw[offset : offset + value_len].decode("utf-8", "replace")
            offset += value_len
            headers[name] = value
        else:
            # Non-string header types (bool / int / timestamp / uuid /
            # bytes) never appear in the headers Bedrock uses for chunk
            # framing. Bail loudly rather than silently mis-parse the
            # rest of the blob at a wrong offset.
            raise EventStreamError(
                f"unsupported event-stream header value type {value_type}"
            )
    return headers


class EventStreamDecoder:
    """Incremental decoder — feed bytes, drain whole messages.

    Usage::

        dec = EventStreamDecoder()
        async for raw in response.aiter_bytes():
            for msg in dec.feed(raw):
                ...
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[EventStreamMessage]:
        """Append ``data`` and return every message now fully buffered."""
        self._buffer.extend(data)
        messages: list[EventStreamMessage] = []
        while True:
            msg = self._try_decode_one()
            if msg is None:
                break
            messages.append(msg)
        return messages

    def _try_decode_one(self) -> EventStreamMessage | None:
        buf = self._buffer
        if len(buf) < _PRELUDE_LEN:
            return None
        total_len, headers_len, prelude_crc = struct.unpack_from(">III", buf, 0)
        if len(buf) < total_len:
            return None  # message not fully arrived yet

        prelude = bytes(buf[0:8])
        if zlib.crc32(prelude) & 0xFFFFFFFF != prelude_crc:
            raise EventStreamError("event-stream prelude CRC32 mismatch")

        message = bytes(buf[0:total_len])
        expected_crc = struct.unpack_from(">I", message, total_len - 4)[0]
        if zlib.crc32(message[: total_len - 4]) & 0xFFFFFFFF != expected_crc:
            raise EventStreamError("event-stream message CRC32 mismatch")

        headers_start = _PRELUDE_LEN
        headers_end = headers_start + headers_len
        payload_end = total_len - 4
        headers = _parse_headers(message[headers_start:headers_end])
        payload = message[headers_end:payload_end]

        del buf[0:total_len]
        return EventStreamMessage(headers=headers, payload=payload)
