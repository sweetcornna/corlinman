"""Protobuf message → provider-dict translation helpers.

Extracted verbatim from
:mod:`corlinman_server.gateway.services.direct_backend` (god-file split).
This module MUST NOT import the source module (no cycle).
"""

from __future__ import annotations

from typing import Any

from corlinman_grpc._generated.corlinman.v1 import (
    common_pb2,
)


def _messages_from_proto(
    messages: Any,
) -> list[dict[str, str]]:
    """Convert protobuf :class:`common_pb2.Message`s to provider dicts.

    Providers' ``chat_stream`` accepts either dicts or objects with
    ``role`` / ``content`` attributes (see
    :func:`corlinman_providers.openai_provider._normalise_message`); a
    dict is the simplest, vendor-agnostic shape. The proto ``Role`` enum
    is lowered to the OpenAI string discriminant.
    """
    out: list[dict[str, str]] = []
    for m in messages:
        msg: dict[str, str] = {
            "role": _role_to_str(m.role),
            "content": m.content or "",
        }
        if m.name:
            msg["name"] = m.name
        if m.tool_call_id:
            msg["tool_call_id"] = m.tool_call_id
        out.append(msg)
    return out


def _role_to_str(role: int) -> str:
    """Lower a proto :class:`common_pb2.Role` value to the OpenAI string."""
    if role == common_pb2.USER:
        return "user"
    if role == common_pb2.ASSISTANT:
        return "assistant"
    if role == common_pb2.SYSTEM:
        return "system"
    if role == common_pb2.TOOL:
        return "tool"
    return "user"
