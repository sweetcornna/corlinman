"""Contract tests for neutral channel-to-gateway chat requests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from corlinman_channels.chat_request import ChannelChatMessage, ChannelChatRequest
from corlinman_channels.common import ChannelBinding
from corlinman_server.gateway.services.channel_bridge import (
    ChannelChatServiceBridge,
    to_internal_chat_request,
)
from corlinman_server.gateway_api import AttachmentKind, Role


@dataclass(slots=True)
class _Attachment:
    kind: str
    url: str | None = None
    bytes_: bytes | None = None
    mime: str | None = None
    file_name: str | None = None


class _RecordingService:
    def __init__(self) -> None:
        self.request: Any = None
        self.cancel: Any = None

    def run(self, request: Any, cancel: Any) -> str:
        self.request = request
        self.cancel = cancel
        return "stream"


def test_converts_complete_channel_contract() -> None:
    request = ChannelChatRequest(
        model="gpt-test",
        messages=[ChannelChatMessage(role="user", content="hello")],
        session_key="session-1",
        stream=True,
        max_tokens=321,
        temperature=0.4,
        attachments=[
            _Attachment(
                kind="image",
                url="https://example.invalid/a.png",
                mime="image/png",
                file_name="a.png",
            )
        ],
        binding=ChannelBinding("qq", "10001", "group-2", "sender-3"),
        persona_id="grantley",
        runtime_instance_id="work-bot",
        scheduler_context={"qq_instance_id": "work-bot"},
        provider_hint="relay",
        provider_params={"reasoning_effort": "high"},
        tenant_id="tenant-a",
        approval_capable=True,
    )

    converted = to_internal_chat_request(request)

    assert converted.model == "gpt-test"
    assert converted.messages[0].role is Role.USER
    assert converted.messages[0].content == "hello"
    assert converted.session_key == "session-1"
    assert converted.stream is True
    assert converted.max_tokens == 321
    assert converted.temperature == 0.4
    assert converted.attachments[0].kind is AttachmentKind.IMAGE
    assert converted.attachments[0].url == "https://example.invalid/a.png"
    assert converted.attachments[0].file_name == "a.png"
    assert converted.binding is not None
    assert converted.binding.account == "10001"
    assert converted.persona_id == "grantley"
    assert converted.runtime_instance_id == "work-bot"
    assert converted.scheduler_context == {"qq_instance_id": "work-bot"}
    assert converted.provider_hint == "relay"
    assert converted.provider_params == {"reasoning_effort": "high"}
    assert converted.tenant_id == "tenant-a"
    # W3-3 contract pin: the bridge reads every optional field via
    # getattr-with-default, so a typo'd/missing field is silently
    # swallowed — this explicit assertion is what proves the capability
    # flag actually crosses the package boundary instead of "looking
    # wired" while every channel stays fail-closed.
    assert converted.approval_capable is True


def test_bridge_passes_validated_request_and_cancel() -> None:
    service = _RecordingService()
    bridge = ChannelChatServiceBridge(service)
    cancel = asyncio.Event()
    request = ChannelChatRequest(
        model="gpt-test",
        messages=[ChannelChatMessage(role="user", content="hello")],
        runtime_instance_id="second-bot",
    )

    result = bridge.run(request, cancel)

    assert result == "stream"
    assert service.request.runtime_instance_id == "second-bot"
    assert service.cancel is cancel


def test_legacy_minimal_request_uses_safe_defaults() -> None:
    request = SimpleNamespace(
        model="gpt-test",
        messages=[SimpleNamespace(role="user", content="hello")],
    )

    converted = to_internal_chat_request(request)

    assert converted.stream is True
    assert converted.session_key == ""
    assert converted.attachments == []
    assert converted.binding is None
    assert converted.persona_id is None
    assert converted.runtime_instance_id == ""
    assert converted.scheduler_context == {}
    assert converted.provider_params == {}
    assert converted.tenant_id is None
    # Legacy duck-typed producers never set the capability → fail-closed.
    assert converted.approval_capable is False
