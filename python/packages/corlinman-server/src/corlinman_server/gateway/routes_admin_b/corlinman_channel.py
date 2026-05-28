"""``/api/channels/corlinman/*`` — HTTP surface for the in-app chat channel.

Wave 3 of ``docs/PLAN_IN_APP_CHAT.md``. Pairs with
:class:`corlinman_channels.web.CorlinmanChannel`: this module exposes the
HTTP surface the browser hits while the channel object owns the
per-session outbound queues + the inbound event shape.

Routes (all under ``/api/channels/corlinman/...``):

* ``POST /send``                 — browser pushes a turn upstream.
* ``GET  /events``               — SSE stream of outbound frames.
* ``POST /typing``               — browser → typing indicator passthrough.
* ``POST /edit/{msg_id}``        — Wave 4 stub (503 ``edit_not_supported``).
* ``DELETE /delete/{msg_id}``    — Wave 4 stub (503 ``delete_not_supported``).
* ``POST /react/{msg_id}``       — Wave 4 stub (503 ``react_not_supported``).

The channel is shared via :class:`AdminState.corlinman_channel` — the gateway
bootstrapper constructs one :class:`CorlinmanChannel` per process and stores
the handle here so the chat-service dispatch loop and these HTTP routes
hit the same per-session queues. When the slot is ``None`` (env flag
off / Wave 3 dark) every route returns a typed 503
``corlinman_channel_disabled`` envelope.

Mounted from :mod:`routes_admin_b.__init__.build_router`; the ``/api``
prefix is encoded on each route directly (no router-level prefix) to
match the rest of the package's pattern.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from corlinman_channels.common import (
    Attachment,
    AttachmentKind,
    UnsupportedError,
)
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    get_admin_state,
    require_admin,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Wire shapes — Pydantic IO so the OpenAPI schema is self-documenting.
# ---------------------------------------------------------------------------


class AttachmentIn(BaseModel):
    """One attachment in :class:`SendBody`.

    Mirrors :class:`corlinman_channels.common.Attachment` with a
    string-typed ``kind`` so the browser can send the bare slug
    (``"image"``, ``"audio"``, ...) without round-tripping through the
    enum class. Either ``url`` or ``data`` is populated; the channel's
    dispatch layer is responsible for handling both.

    ``data`` is base64-encoded when present so JSON can carry the bytes.
    We decode at the boundary so internal callers see a normal
    :class:`bytes` object.
    """

    kind: str = Field(
        ...,
        description="One of 'image' / 'audio' / 'video' / 'document'.",
    )
    url: str | None = Field(default=None, description="Pre-uploaded URL.")
    data_b64: str | None = Field(
        default=None, description="Base64-encoded raw bytes (alternative to url)."
    )
    mime: str | None = Field(default=None)
    file_name: str | None = Field(default=None)


class SendBody(BaseModel):
    """Body for ``POST /api/channels/corlinman/send``."""

    session_key: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Stable per-conversation key the browser owns.",
    )
    text: str = Field(
        ...,
        description="User turn body (may be empty when attachments-only).",
    )
    attachments: list[AttachmentIn] = Field(default_factory=list)
    user_id: str | None = Field(
        default=None,
        description=(
            "Optional canonical actor id (admin username, tenant slug). "
            "Falls back to 'anonymous' when omitted."
        ),
    )


class SendResponse(BaseModel):
    """Response for ``POST /api/channels/corlinman/send``."""

    message_id: str
    accepted_at: int  # unix seconds — channel.ingest sets this
    session_key: str


class TypingBody(BaseModel):
    """Body for ``POST /api/channels/corlinman/typing``."""

    session_key: str = Field(..., min_length=1, max_length=128)
    typing: bool = Field(
        default=True,
        description="``True`` shows the indicator, ``False`` clears it.",
    )


class EditBody(BaseModel):
    """Body for ``POST /api/channels/corlinman/edit/{msg_id}``."""

    session_key: str = Field(..., min_length=1, max_length=128)
    text: str


class ReactBody(BaseModel):
    """Body for ``POST /api/channels/corlinman/react/{msg_id}``."""

    session_key: str = Field(..., min_length=1, max_length=128)
    emoji: str = Field(..., min_length=1, max_length=32)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _corlinman_channel_disabled() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "error": "corlinman_channel_disabled",
            "message": (
                "CorlinmanChannel is not wired on this gateway "
                "(set CORLINMAN_CHANNEL_ENABLED=1 and restart)."
            ),
        },
    )


def _resolve_channel(state: AdminState) -> Any:
    """Pull the live :class:`CorlinmanChannel` off state or 503."""
    ch = getattr(state, "corlinman_channel", None)
    if ch is None:
        raise _corlinman_channel_disabled()
    return ch


def _normalize_attachment(att: AttachmentIn) -> Attachment:
    """Convert wire :class:`AttachmentIn` → channel :class:`Attachment`.

    Validates the ``kind`` enum membership (so a malformed client doesn't
    surface a 500 deep in the dispatch loop) and decodes base64 lazily.
    """
    try:
        kind = AttachmentKind(att.kind)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_attachment_kind",
                "kind": att.kind,
                "allowed": [k.value for k in AttachmentKind],
            },
        ) from exc

    data: bytes | None = None
    if att.data_b64 is not None:
        import base64

        try:
            data = base64.b64decode(att.data_b64, validate=True)
        except Exception as exc:  # noqa: BLE001 — narrowed below
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "invalid_attachment_data_b64",
                    "message": str(exc),
                },
            ) from exc

    if att.url is None and data is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "attachment_missing_payload",
                "message": "Either url or data_b64 must be set.",
            },
        )

    return Attachment(
        kind=kind,
        url=att.url,
        data=data,
        mime=att.mime,
        file_name=att.file_name,
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    """Sub-router for ``/api/channels/corlinman/*``.

    Mounted by :func:`routes_admin_b.build_router`. Auth uses the same
    ``require_admin`` dependency the rest of the admin tree uses — the
    in-app chat is operator-facing and shares the admin login surface.
    """
    r = APIRouter(
        dependencies=[Depends(require_admin)],
        tags=["channels", "corlinman"],
    )

    # ------------------------------------------------------------------
    # POST /api/channels/corlinman/send
    # ------------------------------------------------------------------

    @r.post(
        "/api/channels/corlinman/send",
        response_model=SendResponse,
        summary="Push one browser-originated turn into the CorlinmanChannel",
    )
    async def send_handler(
        body: SendBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> SendResponse:
        ch = _resolve_channel(state)
        attachments = [_normalize_attachment(a) for a in body.attachments]
        try:
            event = await ch.ingest(
                session_key=body.session_key,
                text=body.text,
                attachments=attachments,
                user_id=body.user_id,
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "invalid_ingest", "message": str(exc)},
            ) from exc
        logger.info(
            "corlinman_channel.send session=%s message_id=%s len=%d attachments=%d",
            body.session_key,
            event.message_id,
            len(body.text),
            len(attachments),
        )
        return SendResponse(
            message_id=event.message_id or "",
            accepted_at=event.timestamp,
            session_key=body.session_key,
        )

    # ------------------------------------------------------------------
    # GET /api/channels/corlinman/events
    # ------------------------------------------------------------------

    @r.get(
        "/api/channels/corlinman/events",
        summary="Subscribe to outbound SSE frames for one session",
    )
    async def events_handler(
        session_key: Annotated[
            str,
            Query(
                ...,
                min_length=1,
                max_length=128,
                description="Session key whose outbound queue to drain.",
            ),
        ],
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> StreamingResponse:
        ch = _resolve_channel(state)
        # ``subscribe`` is itself an ``AsyncIterator[bytes]`` — feed it
        # straight into :class:`StreamingResponse`. The channel handles
        # its own connect/disconnect bookkeeping.
        return StreamingResponse(
            ch.subscribe(session_key),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                # nginx reverse-proxy hint: do not buffer this body.
                "X-Accel-Buffering": "no",
            },
        )

    # ------------------------------------------------------------------
    # POST /api/channels/corlinman/typing
    # ------------------------------------------------------------------

    @r.post(
        "/api/channels/corlinman/typing",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Emit a typing indicator on the session's outbound stream",
    )
    async def typing_handler(
        body: TypingBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> None:
        ch = _resolve_channel(state)
        await ch.typing(body.session_key, body.typing)
        # FastAPI maps `None` returns to an empty 204 body when the
        # decorator declares status_code=204.
        return None

    # ------------------------------------------------------------------
    # POST /api/channels/corlinman/edit/{msg_id} — Wave 4 stub
    # ------------------------------------------------------------------

    @r.post(
        "/api/channels/corlinman/edit/{msg_id}",
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        summary="(Wave 4) Edit a previously-sent message in place",
    )
    async def edit_handler(
        msg_id: str,
        body: EditBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> dict[str, Any]:
        ch = _resolve_channel(state)
        try:
            await ch.edit(body.session_key, msg_id, body.text)
        except UnsupportedError as exc:
            # Convert to the typed 503 envelope so the front-end can
            # match on the error code rather than parsing the message.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "edit_not_supported",
                    "message": str(exc),
                    "wave": 4,
                },
            ) from exc
        # The success path here is reserved for Wave 4 — when ``edit``
        # stops raising, swap the decorator status_code to 204 and let
        # the route return ``None``.
        return {"ok": True, "msg_id": msg_id}

    # ------------------------------------------------------------------
    # DELETE /api/channels/corlinman/delete/{msg_id} — Wave 4 stub
    # ------------------------------------------------------------------

    @r.delete(
        "/api/channels/corlinman/delete/{msg_id}",
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        summary="(Wave 4) Delete a previously-sent message",
    )
    async def delete_handler(
        msg_id: str,
        session_key: Annotated[
            str,
            Query(
                ...,
                min_length=1,
                max_length=128,
                description="Session whose message to delete.",
            ),
        ],
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> dict[str, Any]:
        ch = _resolve_channel(state)
        try:
            await ch.delete(session_key, msg_id)
        except UnsupportedError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "delete_not_supported",
                    "message": str(exc),
                    "wave": 4,
                },
            ) from exc
        return {"ok": True, "msg_id": msg_id}

    # ------------------------------------------------------------------
    # POST /api/channels/corlinman/react/{msg_id} — Wave 4 stub
    # ------------------------------------------------------------------

    @r.post(
        "/api/channels/corlinman/react/{msg_id}",
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        summary="(Wave 4) Attach an emoji reaction to a message",
    )
    async def react_handler(
        msg_id: str,
        body: ReactBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> dict[str, Any]:
        ch = _resolve_channel(state)
        try:
            await ch.react(body.session_key, msg_id, body.emoji)
        except UnsupportedError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "react_not_supported",
                    "message": str(exc),
                    "wave": 4,
                },
            ) from exc
        return {"ok": True, "msg_id": msg_id}

    return r
