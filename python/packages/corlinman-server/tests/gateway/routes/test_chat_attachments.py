"""W3 — multimodal content-parts acceptance on ``/v1/chat/completions``.

The pre-W3 ``ChatMessage.content: str`` rejected/coerced the OpenAI
content-parts array, so web-chat attachments never reached the model.
These tests pin the new contract:

* parts flatten to text for the internal ``Message`` list;
* the TRAILING user message's non-text parts become
  ``InternalChatRequest.attachments``;
* ``/v1/files/{id}`` references are inlined as bytes (providers cannot
  fetch gateway-private URLs) while the slim url is kept for the
  journal;
* plain string content keeps working byte-for-byte (channel safety).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from corlinman_server.gateway.routes import chat as chat_route
from corlinman_server.gateway.routes.chat import (
    ChatState,
    ModelRedirect,
    router,
)
from corlinman_server.gateway_api import (
    AttachmentKind,
    DoneEvent,
    InternalChatRequest,
    TokenDeltaEvent,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _RecordingService:
    """Captures the InternalChatRequest the route hands to ChatService."""

    def __init__(self) -> None:
        self.seen: InternalChatRequest | None = None

    def run(
        self, req: InternalChatRequest, cancel: asyncio.Event
    ) -> AsyncIterator[Any]:
        self.seen = req
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[Any]:
        yield TokenDeltaEvent(text="ok")
        yield DoneEvent(finish_reason="stop", usage=None)


def _post(service: _RecordingService, messages: list[dict[str, Any]]) -> None:
    app = FastAPI()
    state = ChatState(service=service, model_redirect=ModelRedirect())
    app.include_router(router(state))
    resp = TestClient(app).post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": messages, "stream": False},
    )
    assert resp.status_code == 200, resp.text


def test_plain_string_content_unchanged() -> None:
    service = _RecordingService()
    _post(service, [{"role": "user", "content": "hello"}])
    assert service.seen is not None
    assert service.seen.messages[-1].content == "hello"
    assert service.seen.attachments == []


def test_parts_flatten_and_extract_trailing_attachments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        chat_route,
        "_resolve_stored",
        lambda fid: (b"PNGBYTES", "image/png", "shot.png")
        if fid == "a" * 26
        else None,
    )
    service = _RecordingService()
    _post(
        service,
        [
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "sure"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look at this"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"/v1/files/{'a' * 26}"},
                    },
                ],
            },
        ],
    )
    req = service.seen
    assert req is not None
    # Text flattened for every message; trailing parts extracted.
    assert [m.content for m in req.messages][-1] == "look at this"
    assert len(req.attachments) == 1
    att = req.attachments[0]
    assert att.kind == AttachmentKind.IMAGE
    assert att.bytes_ == b"PNGBYTES"
    assert att.mime == "image/png"
    # Slim journal-able reference preserved alongside the inlined bytes.
    assert att.url == f"/v1/files/{'a' * 26}"


def test_history_parts_flatten_without_reattaching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attachments on EARLIER user messages must not re-attach to the
    current turn — only their text survives into history."""
    monkeypatch.setattr(
        chat_route,
        "_resolve_stored",
        lambda fid: (b"X", "image/png", "old.png"),
    )
    service = _RecordingService()
    _post(
        service,
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "old image"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"/v1/files/{'b' * 26}"},
                    },
                ],
            },
            {"role": "assistant", "content": "nice"},
            {"role": "user", "content": "plain follow-up"},
        ],
    )
    req = service.seen
    assert req is not None
    assert req.attachments == []
    assert req.messages[0].content == "old image"


def test_data_url_image_passes_through_as_url() -> None:
    service = _RecordingService()
    data_url = "data:image/png;base64,aGk="
    _post(
        service,
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "inline"},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    )
    req = service.seen
    assert req is not None
    assert len(req.attachments) == 1
    assert req.attachments[0].url == data_url
    assert req.attachments[0].bytes_ is None


def test_unknown_stored_file_skipped_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chat_route, "_resolve_stored", lambda fid: None)
    service = _RecordingService()
    _post(
        service,
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "ghost attachment"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"/v1/files/{'c' * 26}"},
                    },
                ],
            }
        ],
    )
    req = service.seen
    assert req is not None
    assert req.attachments == []
    assert req.messages[-1].content == "ghost attachment"


def test_file_part_resolves_via_file_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        chat_route,
        "_resolve_stored",
        lambda fid: (b"%PDF", "application/pdf", "doc.pdf"),
    )
    service = _RecordingService()
    _post(
        service,
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "summarise"},
                    {
                        "type": "file",
                        "file": {"file_id": "d" * 26, "filename": "报告.pdf"},
                    },
                ],
            }
        ],
    )
    req = service.seen
    assert req is not None
    assert len(req.attachments) == 1
    att = req.attachments[0]
    assert att.kind == AttachmentKind.FILE
    assert att.bytes_ == b"%PDF"
    assert att.file_name == "报告.pdf"


def test_per_turn_attachment_count_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More stored references than the per-turn cap → extras skipped, the
    turn still runs (memory-bound, Codex review follow-up)."""
    monkeypatch.setattr(
        chat_route, "_resolve_stored", lambda fid: (b"x", "image/png", "p.png")
    )
    parts: list[dict[str, object]] = [{"type": "text", "text": "many"}]
    parts += [
        {"type": "image_url", "image_url": {"url": f"/v1/files/{c * 26}"}}
        for c in "abcdefghijklm"  # 13 refs > cap of 10
    ]
    service = _RecordingService()
    _post(service, [{"role": "user", "content": parts}])
    req = service.seen
    assert req is not None
    assert len(req.attachments) == chat_route._MAX_ATTACHMENTS_PER_TURN


def test_per_turn_attachment_byte_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    big = b"x" * (40 * 1024 * 1024)  # 40 MiB each; cap is 64 MiB total
    monkeypatch.setattr(
        chat_route, "_resolve_stored", lambda fid: (big, "image/png", "big.png")
    )
    parts: list[dict[str, object]] = [
        {"type": "image_url", "image_url": {"url": f"/v1/files/{c * 26}"}}
        for c in "abc"
    ]
    service = _RecordingService()
    _post(service, [{"role": "user", "content": parts}])
    req = service.seen
    assert req is not None
    # Only the first 40 MiB blob fits under the 64 MiB aggregate cap.
    assert len(req.attachments) == 1


def test_input_audio_part_decodes_to_bytes() -> None:
    service = _RecordingService()
    _post(
        service,
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "transcribe"},
                    {
                        "type": "input_audio",
                        "input_audio": {"data": "aGVsbG8=", "format": "wav"},
                    },
                ],
            }
        ],
    )
    req = service.seen
    assert req is not None
    assert len(req.attachments) == 1
    att = req.attachments[0]
    assert att.kind == AttachmentKind.AUDIO
    assert att.bytes_ == b"hello"
    assert att.mime == "audio/wav"
