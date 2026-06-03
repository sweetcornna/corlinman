"""Module-level helpers + wire shapes for :mod:`corlinman_channel`.

Extracted verbatim from ``routes_admin_b.corlinman_channel`` to keep the
route module focused on ``router()`` + its handlers. Holds the Pydantic
IO models and the small request-shaping helpers the handlers call. No
behavior change — ``corlinman_channel`` re-imports every public name from
here.

This module intentionally does **not** import
``corlinman_server.gateway.routes_admin_b.corlinman_channel`` to avoid an
import cycle.
"""

from __future__ import annotations

import logging
from typing import Any

from corlinman_channels.common import (
    Attachment,
    AttachmentKind,
)
from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
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
