"""Proto translation helpers extracted from
:mod:`corlinman_server.gateway.services.chat_service`.

Pure converters between the in-process gateway-API types and their
protobuf twins, plus the failover-reason lookup table. Moved here
verbatim to keep ``chat_service`` lean; this module MUST NOT import
``chat_service`` (no cycle).
"""

from __future__ import annotations

from corlinman_grpc._generated.corlinman.v1 import (
    agent_pb2,
    common_pb2,
)
from corlinman_grpc.agent_client.types import FailoverReason as GrpcFailoverReason

from corlinman_server.gateway_api import (
    Attachment as ApiAttachment,
)
from corlinman_server.gateway_api import (
    AttachmentKind as ApiAttachmentKind,
)
from corlinman_server.gateway_api import (
    ChannelBinding,
)
from corlinman_server.gateway_api import (
    Role as ApiRole,
)


def _binding_to_proto(b: ChannelBinding) -> common_pb2.ChannelBinding:
    """Convert the in-process :class:`ChannelBinding` to its protobuf
    twin. The ``session_key`` field on the proto side is the pre-derived
    key so the Python agent doesn't need to re-hash. Mirrors
    :rust:`binding_to_proto`."""
    return common_pb2.ChannelBinding(
        channel=b.channel,
        account=b.account,
        thread=b.thread,
        sender=b.sender,
        session_key=b.session_key(),
    )


def _attachment_to_proto(a: ApiAttachment) -> agent_pb2.Attachment:
    """Convert :class:`ApiAttachment` → protobuf ``Attachment``. The
    enum mapping is explicit — silently defaulting to ``UNSPECIFIED``
    would drop multimodal inputs without a trace. Mirrors
    :rust:`attachment_to_proto`."""
    if a.kind == ApiAttachmentKind.IMAGE:
        kind = agent_pb2.ATTACHMENT_KIND_IMAGE
    elif a.kind == ApiAttachmentKind.AUDIO:
        kind = agent_pb2.ATTACHMENT_KIND_AUDIO
    elif a.kind == ApiAttachmentKind.VIDEO:
        kind = agent_pb2.ATTACHMENT_KIND_VIDEO
    elif a.kind == ApiAttachmentKind.FILE:
        kind = agent_pb2.ATTACHMENT_KIND_FILE
    else:  # pragma: no cover — exhaustive over StrEnum
        kind = agent_pb2.ATTACHMENT_KIND_UNSPECIFIED
    return agent_pb2.Attachment(
        kind=kind,
        url=a.url or "",
        bytes=a.bytes_ or b"",
        mime=a.mime or "",
        file_name=a.file_name or "",
    )


def _role_to_proto(role: ApiRole) -> common_pb2.Role:
    if role == ApiRole.USER:
        return common_pb2.USER
    if role == ApiRole.ASSISTANT:
        return common_pb2.ASSISTANT
    if role == ApiRole.SYSTEM:
        return common_pb2.SYSTEM
    if role == ApiRole.TOOL:
        return common_pb2.TOOL
    return common_pb2.ROLE_UNSPECIFIED  # pragma: no cover


# Lowercase string discriminants matching ``InternalChatError.reason``.
# Same set as ``corlinman_grpc.agent_client.types.FailoverReason``.
_REASON_FROM_PROTO: dict[int, str] = {
    int(GrpcFailoverReason.UNSPECIFIED): "unspecified",
    int(GrpcFailoverReason.BILLING): "billing",
    int(GrpcFailoverReason.RATE_LIMIT): "rate_limit",
    int(GrpcFailoverReason.AUTH): "auth",
    int(GrpcFailoverReason.AUTH_PERMANENT): "auth_permanent",
    int(GrpcFailoverReason.TIMEOUT): "timeout",
    int(GrpcFailoverReason.MODEL_NOT_FOUND): "model_not_found",
    int(GrpcFailoverReason.FORMAT): "format",
    int(GrpcFailoverReason.CONTEXT_OVERFLOW): "context_overflow",
    int(GrpcFailoverReason.OVERLOADED): "overloaded",
    int(GrpcFailoverReason.UNKNOWN): "unknown",
}


def _reason_from_proto(code: int) -> str:
    """Mirror :rust:`reason_from_proto` — unknown codes fall back to
    ``"unspecified"`` so a future proto enum addition doesn't crash
    the event stream."""
    return _REASON_FROM_PROTO.get(code, "unspecified")
