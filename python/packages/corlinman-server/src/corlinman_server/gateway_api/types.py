"""Request, event and error data types for ``gateway_api``.

Mirrors the Rust types in ``corlinman-gateway-api::lib`` 1:1 — field
names and semantics are kept identical so a future serde / pydantic
JSON round-trip between the Rust gateway and Python in-process callers
just works.

Modelling choices:

* ``InternalChatRequest`` / ``Attachment`` / ``Message`` / ``Usage`` are
  pydantic ``BaseModel`` subclasses — matches the existing
  ``corlinman_providers.specs`` convention.
* ``Role`` / ``AttachmentKind`` are ``StrEnum`` so the lowercase wire
  values (``"system"``, ``"image"`` …) match the Rust
  ``#[serde(rename_all = "lowercase")]`` derivations.
* ``InternalChatEvent`` is modelled as a sealed Union of frozen
  dataclasses (``TokenDeltaEvent`` / ``ToolCallEvent`` / ``DoneEvent`` /
  ``ErrorEvent``). The Rust side is a tagged enum that the gateway
  emits over an in-process channel — we follow the same "discriminate
  by variant" pattern via ``isinstance`` or the ``.kind`` literal field.
  We deliberately avoid pydantic here so ``bytes`` payloads in
  ``ToolCallEvent.args_json`` aren't copied through validation on every
  emitted token, matching the cheap-clone shape of the Rust enum.
* ``InternalChatError`` is a dataclass (not an ``Exception``) so the
  ``ErrorEvent`` stream variant stays clone-friendly — same rationale
  as the Rust ``#[derive(Clone)]`` comment in the source crate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Attachment",
    "AttachmentEvent",
    "AttachmentKind",
    "ChannelBinding",
    "DoneEvent",
    "ErrorEvent",
    "InternalChatError",
    "InternalChatEvent",
    "InternalChatRequest",
    "Message",
    "Role",
    "TokenDeltaEvent",
    "ToolCallEvent",
    "ToolResultEvent",
    "Usage",
    "internal_chat_error_from_corlinman_error",
]


# ─── Enums ────────────────────────────────────────────────────────────


class Role(StrEnum):
    """Chat message author. Lowercase wire values match Rust ``Role``."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class AttachmentKind(StrEnum):
    """Coarse-grained attachment category. Mirrors the proto enum
    ``corlinman.v1.AttachmentKind`` and Rust ``AttachmentKind``.

    Kept as a string-valued enum so JSON serialisation produces the
    same ``"image"`` / ``"audio"`` / ``"video"`` / ``"file"`` tokens
    the Rust ``#[serde(rename_all = "lowercase")]`` derive emits.
    """

    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    FILE = "file"


# ─── ChannelBinding ──────────────────────────────────────────────────


class ChannelBinding(BaseModel):
    """Transport-agnostic conversation locus.

    Mirrors :class:`corlinman_core::channel_binding::ChannelBinding`.
    The Rust struct also exposes a ``session_key()`` helper that hashes
    the four fields into a 16-hex-char stable id — we expose the same
    method here so Python-side callers can compute the key without
    round-tripping through the Rust crate.

    Field conventions (kept identical to Rust):

    * ``channel`` — lowercase transport name (``"qq"``, ``"telegram"``,
      ``"discord"``, ``"logstream"``).
    * ``account`` — the bot's own id on that transport.
    * ``thread`` — group id for group chats; peer user id for 1:1.
    * ``sender`` — user who sent the message. Equals ``thread`` for
      1:1 DMs.
    """

    model_config = ConfigDict(frozen=False, extra="forbid")

    channel: str
    account: str
    thread: str
    sender: str

    def session_key(self) -> str:
        """Compute the stable 16-hex-char session key for this binding.

        Mirrors the Rust ``ChannelBinding::session_key`` impl byte-for-byte:
        ``sha256("<channel>|<account>|<thread>|<sender>")`` truncated to
        the first 8 bytes formatted as lowercase hex.
        """
        # Local import — sha256 is part of stdlib so this is free; keeping
        # the import inside the method makes the module-level import
        # surface (and hence the public Python API of ``gateway_api``)
        # smaller.
        import hashlib

        h = hashlib.sha256()
        h.update(self.channel.encode("utf-8"))
        h.update(b"|")
        h.update(self.account.encode("utf-8"))
        h.update(b"|")
        h.update(self.thread.encode("utf-8"))
        h.update(b"|")
        h.update(self.sender.encode("utf-8"))
        return h.digest()[:8].hex()

    @classmethod
    def qq_group(cls, self_id: int, group_id: int, sender: int) -> ChannelBinding:
        """Convenience constructor for OneBot v11 group messages."""
        return cls(
            channel="qq",
            account=str(self_id),
            thread=str(group_id),
            sender=str(sender),
        )

    @classmethod
    def qq_private(cls, self_id: int, sender: int) -> ChannelBinding:
        """Convenience constructor for OneBot v11 private messages.

        Mirrors the Rust convention: ``thread == sender`` for 1:1 DMs.
        """
        return cls(
            channel="qq",
            account=str(self_id),
            thread=str(sender),
            sender=str(sender),
        )


# ─── Request models ───────────────────────────────────────────────────


class Message(BaseModel):
    """A single chat turn submitted to the internal pipeline.

    Mirrors the Rust ``Message`` struct — OpenAI-shaped minus the fields
    the internal caller never sets (``function_call`` / ``name``).
    """

    model_config = ConfigDict(frozen=False, extra="forbid")

    role: Role
    content: str = ""


class Attachment(BaseModel):
    """Non-text payload attached to a chat turn.

    ``url`` and ``bytes_`` are mutually complementary — see the docstring
    on the Rust ``Attachment`` struct for the cost-model rationale.

    NOTE: the Rust field is ``bytes``; we use ``bytes_`` here to avoid
    shadowing the Python builtin. A ``serialization_alias`` keeps the
    JSON wire format identical (``"bytes": "..."``).
    """

    model_config = ConfigDict(frozen=False, extra="forbid", populate_by_name=True)

    kind: AttachmentKind
    url: str | None = None
    bytes_: bytes | None = Field(
        default=None,
        alias="bytes",
        serialization_alias="bytes",
    )
    mime: str | None = None
    file_name: str | None = None


class InternalChatRequest(BaseModel):
    """Internal chat request submitted by a channel / scheduler / admin task.

    A deliberately thin shape — everything else (placeholders, provider
    config, tools json) is owned by the gateway and merged in by the
    real ``ChatService`` implementation before handing off to the
    Python reasoning loop. Mirrors the Rust ``InternalChatRequest``.
    """

    model_config = ConfigDict(frozen=False, extra="forbid")

    model: str
    messages: list[Message] = Field(default_factory=list)
    session_key: str = ""
    """Pre-derived session key (see :class:`ChannelBinding.session_key`).

    Empty string is allowed — callers without a binding (one-shot admin
    tests) can leave it blank and the implementation will synthesise an
    ephemeral key.
    """

    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    """Non-text inputs attached to the user turn (images, audio, files).

    Populated by channel adapters that parse multimodal segments; the
    HTTP REST surface currently leaves this empty.
    """

    binding: ChannelBinding | None = None
    """Transport-level conversation locus (channel / account / thread /
    sender) backfilled by channel adapters for audit, per-tool approval,
    and context-assembler scoping. The HTTP REST path leaves this
    ``None`` today — the gateway derives a synthetic binding on the fly
    when it needs one.
    """

    persona_id: str | None = None
    """Persona row id bound by channel humanlike injection.

    The gRPC agent path carries this into ``ChatStart.extra["persona_id"]``
    so persona-life placeholders and persona-life tools read/write the
    same persona-scoped state.
    """

    provider_hint: str | None = None
    """Preferred provider slot for ``model`` resolution.

    Persona Studio stores both provider and model per capability. The
    protobuf ``ChatStart`` has no dedicated provider-hint field, so the
    chat service serialises this into ``provider_config_json`` for the
    direct backend and gRPC agent path.
    """

    provider_params: dict[str, Any] = Field(default_factory=dict)
    """Per-request provider options merged over alias params.

    The public OpenAI-compatible route uses this for vendor-specific knobs
    such as GPT/Codex ``reasoning_effort`` without widening the protobuf
    schema for every provider option.
    """

    tenant_id: str | None = None
    """W8 — tenant the request was authenticated for.

    Stamped by the OpenAI-compatible route from ``request.state.tenant``
    (API-key auth or the admin-session bridge) and carried into
    ``ChatStart.tenant_id`` so the journal turn row is tenant-attributed.
    ``None`` / ``""`` = unattributed (channels, scheduler, console) —
    such turns are owned by the default tenant.
    """


# ─── Usage / events / errors ──────────────────────────────────────────


class Usage(BaseModel):
    """Token usage figures surfaced to the internal caller on completion."""

    model_config = ConfigDict(frozen=False, extra="forbid")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True, slots=True)
class InternalChatError:
    """Clone-friendly error view carried by :class:`ErrorEvent`.

    Mirrors the Rust ``InternalChatError`` struct. Kept as a dataclass
    (not an ``Exception``) so it can be embedded in a streaming event
    and copied around cheaply, matching the
    ``#[derive(Debug, Clone)]`` shape on the Rust side.

    ``reason`` is the lowercase ``FailoverReason`` discriminant string
    (``"billing"`` / ``"rate_limit"`` / …) — same set as
    :class:`corlinman_providers.failover.CorlinmanError.reason`.
    """

    reason: str
    message: str


def internal_chat_error_from_corlinman_error(
    exc: BaseException,
) -> InternalChatError:
    """Lift a ``CorlinmanError`` (or any exception) to an
    :class:`InternalChatError`.

    Mirrors the Rust ``impl From<CorlinmanError> for InternalChatError``:
    typed corlinman failures preserve their ``reason``, everything else
    falls back to ``"unknown"`` plus the stringified message.

    We accept ``BaseException`` rather than the concrete provider error
    type so this helper is usable without importing ``corlinman_providers``
    at module load time (keeps ``gateway_api`` dependency-light).
    """
    reason = getattr(exc, "reason", None)
    if isinstance(reason, str) and reason:
        return InternalChatError(reason=reason, message=str(exc))
    return InternalChatError(reason="unknown", message=str(exc))


# ─── Event sum type ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TokenDeltaEvent:
    """A fragment of assistant-visible text.

    Concatenate ``text`` across events to recover the full message body.
    Mirrors the Rust ``InternalChatEvent::TokenDelta(String)`` variant.

    ``is_reasoning=True`` marks the delta as a reasoning / thinking
    chunk (Anthropic ``thinking`` blocks, DeepSeek-R1 ``reasoning_content``)
    rather than user-visible reply text. UI consumers render these as a
    "💭 推理: …" status line rather than the final reply.
    """

    text: str
    is_reasoning: bool = False
    kind: Literal["token_delta"] = field(default="token_delta", init=False)


@dataclass(frozen=True, slots=True)
class ToolCallEvent:
    """A tool invocation emitted by the reasoning loop.

    Forwarded so consumers can log / observe; the gateway itself handles
    execution. Mirrors the Rust ``InternalChatEvent::ToolCall`` variant.

    ``args_json`` is kept as ``bytes`` (matching Rust's ``Bytes``) so
    callers that just want to forward the payload don't pay a
    decode/re-encode cost. Use ``args_json.decode("utf-8")`` to inspect.
    """

    plugin: str
    tool: str
    args_json: bytes
    call_id: str = ""
    kind: Literal["tool_call"] = field(default="tool_call", init=False)


@dataclass(frozen=True, slots=True)
class ToolResultEvent:
    """Companion to :class:`ToolCallEvent` — fires when an in-process
    builtin tool finishes. UI consumers render this as
    ``✅ tool (1.2s)`` (success) or ``❌ tool failed`` (error).

    Mirrors hermes-agent's ``tool_progress_callback("tool.completed",
    duration=..., is_error=...)`` shape.

    ``payload_json`` optionally carries the tool's parsed result envelope
    (the same dict the agent saw) as a JSON string, so observers such as
    the scheduler ``qzone.daily_publish`` builtin can harvest fields like
    ``tid`` / ``qzone_url`` without a second dispatch. It is best-effort:
    empty when the result wasn't a dict or exceeded the forwarding cap.
    Optional-with-default so duck-typed channel events stay compatible.
    """

    plugin: str
    tool: str
    call_id: str
    duration_ms: int
    is_error: bool = False
    error_summary: str = ""
    payload_json: str = ""
    kind: Literal["tool_result"] = field(default="tool_result", init=False)


@dataclass(frozen=True, slots=True)
class AttachmentEvent:
    """A tool-produced file registered into the gateway file store
    mid-turn — surfaced live so the web chat renders the attachment
    before the turn ends (history replay alone only covers reloads).

    NOTE: unlike the sibling events, ``kind`` here is the *media* kind
    (``"image" | "audio" | "video" | "file"``), not a variant
    discriminator — discriminate via ``isinstance``. ``url`` is
    gateway-relative (``/v1/files/{id}``).
    """

    kind: str
    url: str
    name: str
    mime: str
    size: int | None = None
    call_id: str = ""


@dataclass(frozen=True, slots=True)
class DoneEvent:
    """Terminal sentinel emitted exactly once at the end of a successful run.

    Mirrors the Rust ``InternalChatEvent::Done`` variant.
    """

    finish_reason: str
    usage: Usage | None = None
    kind: Literal["done"] = field(default="done", init=False)


@dataclass(frozen=True, slots=True)
class ErrorEvent:
    """Upstream failure. The stream ends after this event.

    Mirrors the Rust ``InternalChatEvent::Error`` variant.
    """

    error: InternalChatError
    kind: Literal["error"] = field(default="error", init=False)


# Sum type alias matching the Rust ``enum InternalChatEvent``. Discriminate
# via ``isinstance`` or the ``.kind`` literal field — both work.
InternalChatEvent = (
    TokenDeltaEvent
    | ToolCallEvent
    | ToolResultEvent
    | AttachmentEvent
    | DoneEvent
    | ErrorEvent
)
