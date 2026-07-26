"""Neutral typed request shapes accepted by the gateway chat bridge."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ChannelChatMessage:
    role: str
    content: str = ""


@dataclass(slots=True)
class ChannelChatRequest:
    model: str
    messages: list[ChannelChatMessage] = field(default_factory=list)
    session_key: str = ""
    stream: bool = True
    max_tokens: int | None = None
    temperature: float | None = None
    attachments: list[Any] = field(default_factory=list)
    binding: Any = None
    persona_id: str | None = None
    runtime_instance_id: str = ""
    scheduler_context: dict[str, str] = field(default_factory=dict)
    provider_hint: str | None = None
    provider_params: dict[str, Any] = field(default_factory=dict)
    tenant_id: str | None = None
