"""``POST /v1/chat/completions`` — OpenAI-compatible chat entry point.

Python port of ``rust/crates/corlinman-gateway/src/routes/chat.rs``.
The Rust file is the largest of the gateway routes (~2000 LoC) and
covers: model-alias resolution, request validation, session-history
load/persist, gRPC streaming bridge, OpenAI-shape SSE rendering,
tool-call placeholder ack, approval-gate wrapping.

In the Python plane the gRPC bridge collapses to the in-process
:class:`corlinman_server.gateway_api.ChatService` Protocol (W1) —
events arrive as an ``AsyncIterator`` of
:class:`~corlinman_server.gateway_api.InternalChatEvent` values. The
HTTP handler is responsible for:

* Request validation (``model`` + ``messages`` non-empty).
* Model-alias / unknown-model fallback (mirrors the Rust
  :class:`ModelRedirect` semantics).
* Session-key resolution: body wins over the
  ``X-Session-Key`` header. The handler doesn't persist sessions in
  this milestone — the in-process :class:`ChatService` impl already
  owns session storage in Python.
* Dispatching to :class:`ChatService.run` and rendering the resulting
  event stream as OpenAI-shaped SSE (``stream=true``) or a
  single-shot JSON body (``stream=false``).

Tool-call execution remains the gateway's responsibility in Rust; in
Python the :class:`ChatService` implementation already executes
tools internally (the gateway just observes
:class:`ToolCallEvent`s) so we surface them to the SSE consumer in
the OpenAI standard form and otherwise leave the loop alone.

See :class:`ChatState` for the wiring surface and
:func:`router` for the FastAPI APIRouter factory.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Header, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from corlinman_server import telemetry
from corlinman_server.gateway.services.chat_bootstrap import (
    rewrite_trailing_user_message,
)
from corlinman_server.gateway_api import (
    Attachment,
    AttachmentKind,
    ChatService,
    DoneEvent,
    ErrorEvent,
    InternalChatRequest,
    Message,
    Role,
    TokenDeltaEvent,
    ToolCallEvent,
)
from corlinman_server.gateway_api.types import AttachmentEvent, InternalChatEvent

_log = logging.getLogger(__name__)

__all__ = [
    "ChatMessage",
    "ChatRequest",
    "ChatState",
    "ModelRedirect",
    "ResolvedModel",
    "apply_model_aliases",
    "router",
]


# ─── Request / response shapes ───────────────────────────────────────


class ChatMessage(BaseModel):
    """OpenAI-shaped chat message. Mirrors the Rust ``ChatMessage`` struct.

    ``content`` accepts both the plain string form and the OpenAI
    multimodal content-parts array (``[{"type": "text", ...},
    {"type": "image_url", ...}, ...]``) — W3. Parts are flattened to
    text + :class:`~corlinman_server.gateway_api.Attachment` entries in
    :func:`_build_internal_request`; nothing downstream of that sees a
    list.
    """

    model_config = ConfigDict(extra="allow")

    role: str
    content: str | list[dict[str, Any]] = ""
    name: str | None = None
    tool_call_id: str | None = None


class ChatRequest(BaseModel):
    """OpenAI-compatible chat request body.

    Mirrors the Rust ``ChatRequest`` field-for-field. ``tools`` is
    typed as ``Any`` because the gateway treats it opaquely and hands
    it through to the reasoning loop. Extra fields are allowed so
    OpenAI clients that send ``user`` / ``logit_bias`` etc. don't
    400 — they're just ignored.
    """

    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessage] = Field(default_factory=list)
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    tools: object | None = None
    reasoning_effort: str | None = None
    session_key: str | None = None
    persona_id: str | None = None
    """Optional explicit persona binding for the web ``/chat`` path.

    When set (or sent as an ``X-Persona-Id`` header) it overrides the
    configured ``[web].humanlike.persona_id`` default and force-enables
    persona injection. OpenAI clients that don't know about it simply
    leave it unset (and ``extra="allow"`` keeps unknown fields harmless)."""


# ─── Model redirect ──────────────────────────────────────────────────


@dataclass(slots=True)
class ModelRedirect:
    """Alias / unknown-model fallback bundle.

    Mirrors the Rust ``ModelRedirect`` struct + its
    :func:`apply_model_aliases` resolution order.
    """

    aliases: dict[str, str] = field(default_factory=dict)
    default: str = ""
    known_models: set[str] = field(default_factory=set)


@dataclass(slots=True)
class ResolvedModel:
    """Outcome of :func:`apply_model_aliases`. ``kind`` discriminates the four
    cases the Rust enum surfaces: ``aliased`` / ``passthrough`` /
    ``fallback_default`` / ``unknown_no_default``.
    """

    kind: str
    resolved: str | None = None


def apply_model_aliases(model: str, redirect: ModelRedirect) -> ResolvedModel:
    """Pure resolution helper. Mirrors the Rust ``apply_model_aliases`` impl."""
    if model in redirect.aliases:
        return ResolvedModel(kind="aliased", resolved=redirect.aliases[model])
    if not redirect.known_models or model in redirect.known_models:
        return ResolvedModel(kind="passthrough", resolved=model)
    if redirect.default:
        return ResolvedModel(kind="fallback_default", resolved=redirect.default)
    return ResolvedModel(kind="unknown_no_default")


# ─── ChatState ───────────────────────────────────────────────────────


@dataclass(slots=True)
class ChatState:
    """State holder injected into every chat handler.

    Mirrors the Rust ``ChatState`` reduced to the surface a Python
    gateway needs: the in-process :class:`ChatService` (W1) plus the
    optional model redirect. Session storage, tool executor, approval
    gate, and identity store all live inside the ``ChatService``
    implementation on the Python side, so they don't need a separate
    wiring slot here.
    """

    service: ChatService
    model_redirect: ModelRedirect = field(default_factory=ModelRedirect)


# ─── Helpers ─────────────────────────────────────────────────────────


def _resolve_session_key(req: ChatRequest, header_val: str | None) -> str | None:
    """Body wins over header; empty / whitespace treated as absent.
    Mirrors the Rust ``resolve_session_key`` helper.
    """
    if req.session_key is not None:
        v = req.session_key.strip()
        if v:
            return v
    if header_val is not None:
        v = header_val.strip()
        if v:
            return v
    return None


def _role_from_str(s: str) -> Role:
    try:
        return Role(s)
    except ValueError:
        return Role.USER


def _flatten_content(content: str | list[dict[str, Any]] | None) -> str:
    """Collapse OpenAI content-parts to their text. Strings pass through."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    texts = [
        str(p.get("text") or "")
        for p in content
        if isinstance(p, dict) and p.get("type") == "text"
    ]
    return "\n".join(t for t in texts if t)


def _content_chars(content: str | list[dict[str, Any]] | None) -> int:
    """Size of a message's content for the request cap — counts text AND
    embedded payloads (a data-URL image inside a part must not slip past
    the cap just because it isn't a plain string)."""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    total = 0
    for p in content:
        if isinstance(p, dict):
            total += len(json.dumps(p, ensure_ascii=False, default=str))
    return total


_FILES_URL_PREFIX = "/v1/files/"

#: Per-turn bounds on resolved attachments. Each ``/v1/files/{id}``
#: reference inflates from a ~60-char string into the stored payload
#: (≤50 MiB each), so without these caps one request referencing many
#: prior uploads could pin unbounded gateway memory and then balloon
#: further at base64 time. Overflowing parts are skipped with a warning
#: — the turn still runs with what fits.
_MAX_ATTACHMENTS_PER_TURN = 10
_MAX_ATTACHMENT_TOTAL_BYTES = 64 * 1024 * 1024


def _resolve_stored(file_id: str) -> tuple[bytes, str, str] | None:
    """Indirection over :func:`files.load_stored_file` (patchable in tests)."""
    from corlinman_server.gateway.routes.files import load_stored_file

    return load_stored_file(file_id)


def _parts_to_attachments(parts: list[dict[str, Any]]) -> list[Attachment]:
    """Convert the trailing user message's non-text parts to attachments.

    Uploads referenced as ``/v1/files/{id}`` (or a bare ``file_id``)
    are inlined as bytes — model providers cannot fetch gateway-private
    URLs — while the slim ``/v1/files`` url is KEPT on the attachment so
    the journal records a re-fetchable reference instead of megabytes of
    base64. Unresolvable parts are skipped with a warning; one bad part
    must not fail the whole turn.
    """
    out: list[Attachment] = []
    total_bytes = 0

    def _over_budget(extra: int) -> bool:
        nonlocal total_bytes
        if len(out) >= _MAX_ATTACHMENTS_PER_TURN:
            _log.warning(
                "chat attachment: per-turn count cap (%d) reached; skipping",
                _MAX_ATTACHMENTS_PER_TURN,
            )
            return True
        if total_bytes + extra > _MAX_ATTACHMENT_TOTAL_BYTES:
            _log.warning(
                "chat attachment: per-turn byte cap (%d) reached; skipping",
                _MAX_ATTACHMENT_TOTAL_BYTES,
            )
            return True
        total_bytes += extra
        return False

    for p in parts:
        if not isinstance(p, dict):
            continue
        ptype = p.get("type")
        if ptype == "image_url":
            raw = p.get("image_url")
            url = str((raw or {}).get("url") or "").strip() if isinstance(raw, dict) else ""
            if not url:
                continue
            if url.startswith(_FILES_URL_PREFIX):
                loaded = _resolve_stored(url[len(_FILES_URL_PREFIX) :])
                if loaded is None:
                    _log.warning("chat attachment: unknown stored image %s", url)
                    continue
                blob, mime, name = loaded
                if _over_budget(len(blob)):
                    continue
                out.append(
                    Attachment(
                        kind=AttachmentKind.IMAGE,
                        url=url,
                        bytes=blob,
                        mime=mime,
                        file_name=name,
                    )
                )
            elif url.startswith(("http://", "https://", "data:image/", "data:")):
                # data: URLs carry the payload inline — budget them too.
                if _over_budget(len(url) if url.startswith("data:") else 0):
                    continue
                out.append(Attachment(kind=AttachmentKind.IMAGE, url=url))
            else:
                _log.warning("chat attachment: unsupported image url form")
        elif ptype == "file":
            raw_f = p.get("file")
            f = raw_f if isinstance(raw_f, dict) else {}
            file_id = str(f.get("file_id") or "").strip()
            if file_id.startswith(_FILES_URL_PREFIX):
                file_id = file_id[len(_FILES_URL_PREFIX) :]
            filename = str(f.get("filename") or "") or None
            if file_id:
                loaded = _resolve_stored(file_id)
                if loaded is None:
                    _log.warning("chat attachment: unknown stored file %s", file_id)
                    continue
                blob, mime, name = loaded
                if _over_budget(len(blob)):
                    continue
                out.append(
                    Attachment(
                        kind=AttachmentKind.FILE,
                        url=f"{_FILES_URL_PREFIX}{file_id}",
                        bytes=blob,
                        mime=mime,
                        file_name=filename or name,
                    )
                )
            elif f.get("file_data"):
                data = str(f.get("file_data") or "")
                decoded = _decode_b64_payload(data)
                if decoded is None:
                    _log.warning("chat attachment: undecodable file_data")
                    continue
                if _over_budget(len(decoded)):
                    continue
                out.append(
                    Attachment(
                        kind=AttachmentKind.FILE,
                        bytes=decoded,
                        # Keep the data-URL's declared mime — dropping it
                        # made the loop rebuild the payload as
                        # octet-stream, so providers couldn't recognise
                        # an inline PDF as a document.
                        mime=_data_url_mime(data),
                        file_name=filename,
                    )
                )
        elif ptype == "input_audio":
            raw_a = p.get("input_audio")
            ia = raw_a if isinstance(raw_a, dict) else {}
            decoded = _decode_b64_payload(str(ia.get("data") or ""))
            if decoded is None:
                _log.warning("chat attachment: undecodable input_audio")
                continue
            if _over_budget(len(decoded)):
                continue
            fmt = str(ia.get("format") or "").strip()
            out.append(
                Attachment(
                    kind=AttachmentKind.AUDIO,
                    bytes=decoded,
                    mime=f"audio/{fmt}" if fmt else None,
                )
            )
    return out


def _data_url_mime(data: str) -> str | None:
    """Extract the declared mime from a ``data:`` URL, else ``None``."""
    if not data.startswith("data:"):
        return None
    header = data.split(",", 1)[0]
    mime = header[5:].split(";", 1)[0].strip().lower()
    return mime or None


def _decode_b64_payload(data: str) -> bytes | None:
    """Decode a base64 payload that may arrive as a bare string or a
    ``data:`` URL. ``None`` on any decode failure."""
    if not data:
        return None
    if data.startswith("data:"):
        _, _, tail = data.partition(",")
        data = tail
    try:
        return base64.b64decode(data, validate=False)
    except (ValueError, binascii.Error):
        return None


_REASONING_EFFORTS: frozenset[str] = frozenset(
    {"low", "medium", "high", "xhigh"}
)


def _provider_params_from_chat_request(req: ChatRequest) -> dict[str, Any]:
    params: dict[str, Any] = {}
    raw_effort = req.reasoning_effort
    if isinstance(raw_effort, str):
        effort = raw_effort.strip().lower()
        if effort in _REASONING_EFFORTS:
            params["reasoning_effort"] = effort
    return params


def _build_internal_request(req: ChatRequest, session_key: str | None) -> InternalChatRequest:
    """Translate the OpenAI body into the internal protocol shape.

    Multimodal content-parts (W3) are split here: every message's parts
    flatten to their text, and the **trailing user message's** non-text
    parts become :class:`Attachment` entries on the request (the
    reasoning loop re-injects them into that same trailing turn — see
    ``corlinman_agent.reasoning_loop._inject_attachments``). Earlier
    messages keep text only: providers treat history as already
    normalised, and the journal re-serves historical attachments to the
    UI out-of-band.

    Before the conversion, the **trailing user message** is checked
    against the shared slash-command registry (W8 — Persona Studio). If
    it matches (e.g. the user typed ``/persona``), its content is
    swapped for the registry's wizard prelude so the agent sees a
    structured invocation instruction instead of the literal command.
    Older user messages in ``req.messages`` are intentionally left
    untouched — see :mod:`corlinman_server.gateway.services.chat_bootstrap`
    for why retroactive rewrites would corrupt the transcript.
    """
    attachments: list[Attachment] = []
    normalised: list[ChatMessage] = []
    trailing_user_idx: int | None = None
    for i in range(len(req.messages) - 1, -1, -1):
        if req.messages[i].role == "user":
            trailing_user_idx = i
            break
    for i, m in enumerate(req.messages):
        if isinstance(m.content, str):
            normalised.append(m)
            continue
        if i == trailing_user_idx:
            attachments = _parts_to_attachments(m.content)
        normalised.append(
            m.model_copy(update={"content": _flatten_content(m.content)})
        )
    rewritten = rewrite_trailing_user_message(normalised)
    return InternalChatRequest(
        model=req.model,
        messages=[
            Message(role=_role_from_str(m.role), content=_flatten_content(m.content))
            for m in rewritten
        ],
        session_key=session_key or "",
        stream=req.stream,
        max_tokens=req.max_tokens,
        temperature=req.temperature,
        attachments=attachments,
        provider_params=_provider_params_from_chat_request(req),
    )


# ─── Web persona injection ───────────────────────────────────────────
#
# The 5 chat channels (Telegram/Discord/Slack/Feishu/QQ) prepend a
# persona ``role="system"`` message to every inbound turn when their
# ``[channels.{name}.humanlike]`` block is on. The in-app ``/chat`` UI
# drives ``POST /v1/chat/completions`` and historically NEVER injected a
# persona — ``persona_id`` was always empty, so 格兰 was out of character
# on the web (root cause R5 / gap H19).
#
# We mirror the channel wiring with a ``[web].humanlike`` block carrying
# ``enabled`` + ``persona_id``. An explicit ``persona_id`` in the request
# body (OpenAI bodies allow extra fields) or an ``X-Persona-Id`` header
# overrides the configured default when present. The persona store +
# asset store live on the admin_a singleton state (Wave 1) — the same
# handles the channels read — so we resolve them the same way.
#
# We deliberately inline a minimal equivalent of
# ``corlinman_channels.persona_inject.inject_persona_if_enabled`` rather
# than calling it directly: that helper prepends a duck-typed
# ``SimpleNamespace(role="system", ...)`` which is fine for the channels'
# SimpleNamespace request but would smuggle a non-``Message`` /
# string-``role`` value into the strict pydantic ``InternalChatRequest``
# (``Message.role`` is the ``Role`` enum, ``extra="forbid"``). Building a
# real ``Message(role=Role.SYSTEM, ...)`` keeps the internal request
# well-typed for the downstream ``ChatService`` / gRPC agent path. We DO
# reuse the type-agnostic ``compose_persona_emoji_block`` string helper so
# the emoji block stays byte-identical to the channel output.


def _resolve_web_persona(
    config: Any,
    req: ChatRequest,
    header_persona_id: str | None,
) -> tuple[bool, str | None]:
    """Resolve ``(humanlike_enabled, persona_id)`` for the web chat path.

    Reads the static ``[web].humanlike`` block from the live config dict
    (``{enabled, persona_id}``), mirroring the channel
    ``_humanlike_initial`` reader. An explicit ``persona_id`` from the
    request body or the ``X-Persona-Id`` header wins over the configured
    default when present (and force-enables injection — an explicit
    request-level persona is an intentional opt-in).
    """
    enabled = False
    persona_id: str | None = None
    if isinstance(config, Mapping):
        web = config.get("web")
        if isinstance(web, Mapping):
            block = web.get("humanlike")
            if isinstance(block, Mapping):
                enabled = bool(block.get("enabled", False))
                cfg_pid = block.get("persona_id")
                persona_id = cfg_pid if isinstance(cfg_pid, str) else None

    # Explicit request override: body field (extra="allow") then header.
    override = getattr(req, "persona_id", None)
    if not isinstance(override, str) or not override.strip():
        override = header_persona_id
    if isinstance(override, str) and override.strip():
        return (True, override.strip())

    return (enabled, persona_id)


def _persona_stores() -> tuple[Any, Any]:
    """Return ``(persona_store, asset_store)`` off the admin_a singleton.

    Both live on the admin_a :class:`AdminState` (opened at boot, Wave 1)
    — the very handles the channels read. Best-effort: when the state
    isn't installed (degraded boot / router-only tests) or the slots are
    still ``None``, return ``(None, None)`` so persona injection silently
    no-ops rather than crashing the chat request.
    """
    try:
        from corlinman_server.gateway.routes_admin_a import get_admin_state

        admin_a_state = get_admin_state()
    except Exception:  # noqa: BLE001 — defensive; degraded mode
        return (None, None)
    return (
        getattr(admin_a_state, "persona_store", None),
        getattr(admin_a_state, "persona_asset_store", None),
    )


async def _inject_web_persona(
    internal_req: InternalChatRequest,
    config: Any,
    req: ChatRequest,
    header_persona_id: str | None,
) -> None:
    """Prepend the bound persona's system prompt to ``internal_req``.

    Mirrors :func:`corlinman_channels.persona_inject.inject_persona_if_enabled`
    but emits a well-typed :class:`Message` (``role=Role.SYSTEM``) so the
    strict ``InternalChatRequest`` stays valid for the downstream
    ``ChatService`` / gRPC agent path. Silently no-ops when the gate is
    off, no ``persona_id`` is bound, the store is missing, or the persona
    row is absent / has an empty ``system_prompt``. Any store failure logs
    a warning and returns without touching the request — persona is
    decorative; web chat must keep working when it breaks.
    """
    enabled, persona_id = _resolve_web_persona(config, req, header_persona_id)
    if not enabled or not persona_id:
        return

    persona_store, asset_store = _persona_stores()
    if persona_store is None:
        return
    try:
        persona = await persona_store.get(persona_id)
    except Exception as exc:  # noqa: BLE001 — never let store I/O kill chat
        _log.warning("web chat persona lookup failed: %s", exc)
        return
    if persona is None:
        return
    try:
        from corlinman_channels.persona_inject import (
            apply_persona_text_model_binding,
            persona_text_model_override,
        )

        text_model = persona_text_model_override(persona)
        apply_persona_text_model_binding(internal_req, persona)
    except Exception as exc:  # noqa: BLE001 — model override is best-effort
        _log.warning("web chat persona model binding failed: %s", exc)
        text_model = None
    if text_model:
        req.model = text_model

    body = getattr(persona, "system_prompt", "") or ""
    if not body.strip():
        return

    # Reuse the channels' type-agnostic emoji-block composer so the web
    # block is byte-identical to what the 5 channels produce. Importing
    # from corlinman_channels is layering-safe: corlinman-server already
    # depends on corlinman-channels (channels_runtime imports it), and the
    # package is not part of the .importlinter core-plane layer contract.
    try:
        from corlinman_channels.persona_inject import (
            compose_persona_emoji_block,
        )

        emoji_block = await compose_persona_emoji_block(persona_id, asset_store)
    except Exception as exc:  # noqa: BLE001 — emoji block is best-effort
        _log.warning("web chat emoji block failed: %s", exc)
        emoji_block = None

    if emoji_block:
        content = body + "\n\n" + emoji_block + "\n\n---\n"
    else:
        content = body + "\n\n---\n"

    sys_msg = Message(role=Role.SYSTEM, content=content)
    internal_req.messages = [sys_msg, *list(internal_req.messages)]
    internal_req.persona_id = persona_id


def _new_chat_id() -> str:
    """``chatcmpl-<uuid4>`` matches OpenAI + the Rust impl."""
    return f"chatcmpl-{uuid.uuid4()}"


def _normalise_finish_reason(raw: str, had_tool_calls: bool) -> str:
    """Mirror the Rust ``normalise_finish_reason`` mapping."""
    if raw in ("stop", "length", "tool_calls", "error"):
        return raw
    if raw == "tool_call":
        return "tool_calls"
    if raw == "":
        return "tool_calls" if had_tool_calls else "stop"
    return raw


def _tool_call_envelope(event: ToolCallEvent, call_id: str) -> dict[str, object]:
    """OpenAI non-streaming tool_call envelope."""
    # ``errors="replace"``: a provider emitting invalid UTF-8 in tool args
    # must degrade to mojibake, not kill the whole response generator.
    args = (
        event.args_json.decode("utf-8", errors="replace")
        if event.args_json
        else "{}"
    )
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": event.tool,
            "arguments": args,
        },
    }


def _tool_call_delta_chunk(
    chat_id: str, model: str, index: int, event: ToolCallEvent, call_id: str
) -> dict[str, object]:
    args = (
        event.args_json.decode("utf-8", errors="replace")
        if event.args_json
        else "{}"
    )
    return {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": event.tool,
                                "arguments": args,
                            },
                        }
                    ]
                },
                "finish_reason": None,
            }
        ],
    }


def _token_delta_chunk(
    chat_id: str, model: str, text: str, *, reasoning: bool = False
) -> dict[str, object]:
    # Reasoning deltas ride the OpenAI-compatible ``reasoning_content``
    # extension (DeepSeek/vLLM convention) — NEVER ``content``. Folding
    # them into ``content`` rendered the model's chain-of-thought as the
    # visible reply; the web chat already splits ``reasoning_content``
    # into its collapsible thinking block.
    delta: dict[str, object] = {"role": "assistant"}
    if reasoning:
        delta["reasoning_content"] = text
    else:
        delta["content"] = text
    return {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": None,
            }
        ],
    }


def _attachment_chunk(
    chat_id: str, model: str, event: AttachmentEvent
) -> dict[str, object]:
    # Live attachment metadata rides a vendor extension (``corlinman``
    # key) on an otherwise-empty delta chunk — OpenAI-compatible parsers
    # that fold ``choices[].delta`` see a no-op frame, while the web chat
    # renders the file card mid-turn.
    return {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
        "corlinman": {
            "attachment": {
                "kind": event.kind,
                "url": event.url,
                "name": event.name,
                "mime": event.mime,
                **({"size": event.size} if event.size else {}),
            }
        },
    }


def _finish_chunk(chat_id: str, model: str, finish_reason: str) -> dict[str, object]:
    return {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
    }


def _error_chunk(
    chat_id: str, model: str, code: str, reason: str, message: str
) -> dict[str, object]:
    """Streaming error rendered as a *valid* OpenAI chunk.

    The previous shape was a bare ``{"error": {...}}`` frame with no
    ``choices`` — stream reducers that fold ``chunk.choices`` (our web
    chat included) saw zero events and left the turn stuck in a loading
    state forever, with the HTTP status already locked at 200. Keep the
    ``error`` payload for API consumers, and add a terminal
    ``finish_reason="error"`` choice so every consumer observes a
    turn-ending event.
    """
    return {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
        "error": {"code": code, "reason": reason, "message": message},
    }


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status_code,
    )


# ─── Streaming + non-streaming bodies ────────────────────────────────


async def _run_nonstream(
    service: ChatService,
    internal_req: InternalChatRequest,
    model: str,
    cancel: asyncio.Event,
) -> JSONResponse:
    """Drain the event stream and assemble an OpenAI-shaped JSON body.
    Mirrors the Rust ``chat_nonstream`` implementation.
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict[str, object]] = []
    attachments: list[dict[str, object]] = []
    finish_reason = "stop"

    stream: AsyncIterator[InternalChatEvent] = service.run(internal_req, cancel)
    async for event in stream:
        if isinstance(event, TokenDeltaEvent):
            # Keep chain-of-thought out of the visible reply — surfaced
            # separately via the ``reasoning_content`` extension below.
            if getattr(event, "is_reasoning", False):
                reasoning_parts.append(event.text)
            else:
                content_parts.append(event.text)
        elif isinstance(event, AttachmentEvent):
            attachments.append(
                {
                    "kind": event.kind,
                    "url": event.url,
                    "name": event.name,
                    "mime": event.mime,
                    **({"size": event.size} if event.size else {}),
                }
            )
        elif isinstance(event, ToolCallEvent):
            # Reuse the agent's own call id so this surface and the
            # journal events (ToolState*, AwaitingApproval) describe one
            # tool call under one identity — a synthesised id made the
            # web chat render duplicate cards once journal events
            # started reaching it. Fallback for backends that omit it.
            call_id = event.call_id or f"call_{uuid.uuid4().hex[:16]}"
            tool_calls.append(_tool_call_envelope(event, call_id))
        elif isinstance(event, DoneEvent):
            finish_reason = _normalise_finish_reason(
                event.finish_reason, bool(tool_calls)
            )
            break
        elif isinstance(event, ErrorEvent):
            return _error_response(
                status.HTTP_502_BAD_GATEWAY,
                f"upstream_{event.error.reason}",
                event.error.message,
            )

    body: dict[str, object] = {
        "id": _new_chat_id(),
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "".join(content_parts),
                    **(
                        {"reasoning_content": "".join(reasoning_parts)}
                        if reasoning_parts
                        else {}
                    ),
                    **({"tool_calls": tool_calls} if tool_calls else {}),
                    **({"attachments": attachments} if attachments else {}),
                },
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }
    return JSONResponse(body)


# Upper bound on the *total* characters across all request messages.
# Generous (a full long conversation is well under this), but stops a
# malformed client from pushing arbitrarily large payloads into the
# reasoning loop. Mirrors OpenAI's own per-request content limits.
_MAX_TOTAL_CONTENT_CHARS = 2 * 1024 * 1024

# Seconds of event-stream silence before emitting an SSE comment
# heartbeat. Long-running tools produce no events while they execute
# (image generation can block for 120s+); without any bytes on the wire
# an idle-timeout proxy (30-60s is typical) kills the TCP connection
# mid-turn and the browser surfaces a bare "network error". Same 10s
# cadence as the admin sessions live stream (``sessions_events.py``).
_SSE_HEARTBEAT_SECS = 10.0

# Bounded hand-off between the service stream and the SSE writer. Keeps
# *some* backpressure on the producer while letting the writer wake up
# on a timer to heartbeat. ``asyncio.wait_for`` cannot safely wrap
# ``__anext__`` on an async generator (the timeout cancellation lands
# inside the generator frame and tears it down), so events are pumped
# through a queue whose ``get()`` is cancellation-safe instead.
_SSE_QUEUE_MAXSIZE = 256

# Queue sentinel: the pump task finished (stream exhausted or errored).
_STREAM_END = object()


async def _sse_iter(
    service: ChatService,
    internal_req: InternalChatRequest,
    model: str,
    cancel: asyncio.Event,
) -> AsyncIterator[bytes]:
    """Render the event stream as OpenAI-shaped SSE.
    Mirrors the Rust ``build_sse_stream`` implementation, plus a comment
    heartbeat every :data:`_SSE_HEARTBEAT_SECS` of silence so idle
    proxies don't drop the connection while a slow tool runs.
    """
    chat_id = _new_chat_id()
    next_index = 0
    tool_calls_seen = False

    queue: asyncio.Queue[object] = asyncio.Queue(maxsize=_SSE_QUEUE_MAXSIZE)

    async def _pump() -> None:
        try:
            stream: AsyncIterator[InternalChatEvent] = service.run(
                internal_req, cancel
            )
            async for event in stream:
                await queue.put(event)
        except Exception as exc:  # noqa: BLE001 — surfaced as an SSE error chunk
            await queue.put(exc)
        finally:
            await queue.put(_STREAM_END)

    pump_task = asyncio.create_task(_pump())
    try:
        while True:
            try:
                event = await asyncio.wait_for(
                    queue.get(), timeout=_SSE_HEARTBEAT_SECS
                )
            except TimeoutError:
                # SSE comment frame: ignored by every spec-compliant
                # parser, but keeps bytes flowing through idle proxies.
                yield b": ping\n\n"
                continue
            if event is _STREAM_END:
                break
            if isinstance(event, TokenDeltaEvent):
                chunk = _token_delta_chunk(
                    chat_id,
                    model,
                    event.text,
                    reasoning=bool(getattr(event, "is_reasoning", False)),
                )
                yield f"data: {json.dumps(chunk)}\n\n".encode()
            elif isinstance(event, AttachmentEvent):
                chunk = _attachment_chunk(chat_id, model, event)
                yield f"data: {json.dumps(chunk)}\n\n".encode()
            elif isinstance(event, ToolCallEvent):
                # The agent's call id, not a synthesised one — keeps this
                # surface and the journal events (ToolState*) describing
                # one tool call under one identity so the web chat can
                # merge them instead of rendering duplicate cards.
                call_id = event.call_id or f"call_{uuid.uuid4().hex[:16]}"
                chunk = _tool_call_delta_chunk(
                    chat_id, model, next_index, event, call_id
                )
                next_index += 1
                tool_calls_seen = True
                yield f"data: {json.dumps(chunk)}\n\n".encode()
            elif isinstance(event, DoneEvent):
                finish = _normalise_finish_reason(
                    event.finish_reason, tool_calls_seen
                )
                chunk = _finish_chunk(chat_id, model, finish)
                yield f"data: {json.dumps(chunk)}\n\n".encode()
                break
            elif isinstance(event, ErrorEvent):
                chunk = _error_chunk(
                    chat_id,
                    model,
                    "upstream_error",
                    event.error.reason,
                    event.error.message,
                )
                yield f"data: {json.dumps(chunk)}\n\n".encode()
                break
            elif isinstance(event, Exception):
                # The service stream itself raised. Pre-heartbeat this
                # propagated and killed the generator with no terminal
                # frame — the client hung on a half-open stream.
                _log.error("chat stream raised mid-turn", exc_info=event)
                chunk = _error_chunk(
                    chat_id,
                    model,
                    "internal_error",
                    "stream_exception",
                    str(event) or event.__class__.__name__,
                )
                yield f"data: {json.dumps(chunk)}\n\n".encode()
                break
    finally:
        pump_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pump_task
    yield b"data: [DONE]\n\n"


# ─── Router ──────────────────────────────────────────────────────────


def router(state: ChatState | None = None) -> APIRouter:
    """Build the ``/v1/chat/completions`` sub-router.

    :param state: :class:`ChatState` carrying the wired
        :class:`ChatService`. When ``None`` the route returns 501
        ``not_implemented`` — matches the Rust stub router.
    """
    api = APIRouter()

    @api.post("/v1/chat/completions", response_model=None)
    async def handle_chat(
        req: ChatRequest,
        request: Request,
        x_session_key: str | None = Header(default=None),
        x_persona_id: str | None = Header(default=None),
    ) -> JSONResponse | StreamingResponse:
        with telemetry.span(
            "chat.completions",
            attributes={
                "chat.model": req.model or "",
                "chat.stream": req.stream,
                "chat.messages": len(req.messages),
            },
        ) as _span:
            # Resolve the ChatService. Tests pass ``state`` directly; in the
            # gateway the router is composed before the lifespan runs, so
            # pull the live service the ``services.bootstrap`` sibling hook
            # attached to ``AppState.chat`` (docs/contracts/runtime-wiring.md).
            # ``app_state`` is also the live-config source for the
            # ``[web].humanlike`` persona block, so grab it even when ``state``
            # was passed directly (router-only tests leave it ``None``).
            app_state = getattr(request.app.state, "corlinman", None)
            chat_state = state
            if chat_state is None:
                svc = (
                    getattr(app_state, "chat", None)
                    if app_state is not None
                    else None
                )
                if svc is not None:
                    chat_state = ChatState(service=svc)
            if chat_state is None:
                _span.set_attribute("http.status_code", 501)
                return _error_response(
                    status.HTTP_501_NOT_IMPLEMENTED,
                    "not_implemented",
                    "no ChatService wired; build router(state=...)",
                )

            if not req.model:
                _span.set_attribute("http.status_code", 400)
                return _error_response(
                    status.HTTP_400_BAD_REQUEST,
                    "invalid_request",
                    "`model` is required",
                )
            if not req.messages:
                _span.set_attribute("http.status_code", 400)
                return _error_response(
                    status.HTTP_400_BAD_REQUEST,
                    "invalid_request",
                    "`messages` must be non-empty",
                )
            total_content = sum(_content_chars(m.content) for m in req.messages)
            if total_content > _MAX_TOTAL_CONTENT_CHARS:
                _span.set_attribute("http.status_code", 400)
                return _error_response(
                    status.HTTP_400_BAD_REQUEST,
                    "invalid_request",
                    "total `messages` content exceeds "
                    f"{_MAX_TOTAL_CONTENT_CHARS} characters",
                )

            # Model alias / unknown-model fallback. Pure function so the
            # logging tier sits in the handler.
            original_model = req.model
            resolution = apply_model_aliases(req.model, chat_state.model_redirect)
            if resolution.kind == "aliased":
                req.model = resolution.resolved or req.model
            elif resolution.kind == "fallback_default":
                req.model = resolution.resolved or req.model
            elif resolution.kind == "unknown_no_default":
                _span.set_attribute("http.status_code", 400)
                return _error_response(
                    status.HTTP_400_BAD_REQUEST,
                    "unknown_model",
                    f"model `{original_model}` is not a known alias or provider "
                    f"model, and no `models.default` fallback is configured",
                )

            _span.set_attribute("chat.resolved_model", req.model)
            _span.set_attribute("chat.model_resolution", resolution.kind)

            session_key = _resolve_session_key(req, x_session_key)
            internal_req = _build_internal_request(req, session_key)

            # H19: inject the bound persona's system prompt so the in-app
            # ``/chat`` UI is in character, mirroring the 5 chat channels.
            # Reads ``[web].humanlike`` off the live config + an optional
            # explicit ``persona_id`` (body field / ``X-Persona-Id`` header).
            # Best-effort + null-safe: no config / no store / gate off → no-op,
            # leaving the request exactly as it was (and never breaking the
            # admin-session auth bridge, which runs in middleware upstream).
            config = getattr(app_state, "config", None) if app_state else None
            await _inject_web_persona(internal_req, config, req, x_persona_id)
            _span.set_attribute("chat.resolved_model", req.model)
            if internal_req.persona_id:
                _span.set_attribute("chat.persona_id", internal_req.persona_id)

            cancel = asyncio.Event()

            if req.stream:
                _span.set_attribute("http.status_code", 200)

                async def _agen() -> AsyncIterator[bytes]:
                    try:
                        async for chunk in _sse_iter(
                            chat_state.service, internal_req, req.model, cancel
                        ):
                            if await request.is_disconnected():
                                cancel.set()
                                break
                            yield chunk
                    finally:
                        cancel.set()

                return StreamingResponse(_agen(), media_type="text/event-stream")

            result = await _run_nonstream(
                chat_state.service, internal_req, req.model, cancel
            )
            _span.set_attribute("http.status_code", result.status_code)
            return result

    return api
