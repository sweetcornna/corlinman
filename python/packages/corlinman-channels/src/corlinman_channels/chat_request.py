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
    # W3-3 — this surface implements the approval reply loop (it renders
    # AwaitingApproval events and can send the decision back through the
    # gateway approval broker). Optional-with-default so every existing
    # construction site keeps working (slots dataclass: adding a REQUIRED
    # field here would crash cross-package constructors at call time).
    approval_capable: bool = False
