"""Typed conversion from neutral channel requests to gateway requests."""

from __future__ import annotations

from typing import Any

from corlinman_server.gateway_api import (
    Attachment,
    AttachmentKind,
    ChannelBinding,
    InternalChatRequest,
    Message,
    Role,
)


class ChannelChatServiceBridge:
    """Validate channel-owned request dataclasses before running the gateway."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def run(self, request: Any, cancel: Any) -> Any:
        return self._service.run(to_internal_chat_request(request), cancel)


def to_internal_chat_request(request: Any) -> InternalChatRequest:
    messages = [
        Message(role=Role(str(message.role)), content=str(message.content or ""))
        for message in request.messages
    ]
    attachments = [
        _convert_attachment(item) for item in getattr(request, "attachments", [])
    ]
    binding = _convert_binding(getattr(request, "binding", None))
    return InternalChatRequest(
        model=str(request.model),
        messages=messages,
        session_key=str(getattr(request, "session_key", "") or ""),
        stream=bool(getattr(request, "stream", True)),
        max_tokens=getattr(request, "max_tokens", None),
        temperature=getattr(request, "temperature", None),
        attachments=attachments,
        binding=binding,
        persona_id=getattr(request, "persona_id", None),
        runtime_instance_id=str(
            getattr(request, "runtime_instance_id", "") or ""
        ),
        scheduler_context=dict(getattr(request, "scheduler_context", None) or {}),
        provider_hint=getattr(request, "provider_hint", None),
        provider_params=dict(getattr(request, "provider_params", None) or {}),
        tenant_id=getattr(request, "tenant_id", None),
        # W3-3 — tolerant read like every other optional field here, BUT
        # covered by an explicit contract test: this bridge swallows a
        # missing attribute into the default silently, so "looks wired,
        # actually dropped" must be caught by the test, not in prod.
        approval_capable=bool(getattr(request, "approval_capable", False)),
    )


def _convert_binding(binding: Any) -> ChannelBinding | None:
    if binding is None:
        return None
    return ChannelBinding(
        channel=str(binding.channel),
        account=str(binding.account),
        thread=str(binding.thread),
        sender=str(binding.sender),
    )


def _convert_attachment(attachment: Any) -> Attachment:
    kind = getattr(attachment.kind, "value", attachment.kind)
    return Attachment(
        kind=AttachmentKind(str(kind)),
        url=getattr(attachment, "url", None),
        bytes=getattr(attachment, "bytes_", None),
        mime=getattr(attachment, "mime", None),
        file_name=getattr(attachment, "file_name", None),
    )
