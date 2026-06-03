"""Module-level support code extracted from ``studio/personas.py``.

This holds the wire-shape models, constants, and helper functions that the
``personas`` route handlers depend on. It was split out of the route module
verbatim to keep that file focused on ``router()`` + the request handlers.

This module MUST NOT import the ``personas`` route module — doing so would
create an import cycle. The route module re-imports the names it needs from
here instead.
"""

from __future__ import annotations

import re
from typing import Any, Literal, cast

from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
)
from corlinman_server.persona import (
    AssetKind,
    AssetRecord,
    Persona,
    PersonaAssetStore,
    PersonaStore,
)

# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class PersonaOut(BaseModel):
    id: str
    display_name: str
    short_summary: str
    system_prompt: str
    is_builtin: bool
    created_at_ms: int
    updated_at_ms: int

    @classmethod
    def from_row(cls, p: Persona) -> PersonaOut:
        return cls(
            id=p.id,
            display_name=p.display_name,
            short_summary=p.short_summary,
            system_prompt=p.system_prompt,
            is_builtin=p.is_builtin,
            created_at_ms=p.created_at_ms,
            updated_at_ms=p.updated_at_ms,
        )


class ListOut(BaseModel):
    personas: list[PersonaOut]


class CreateBody(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    display_name: str = Field(min_length=1, max_length=200)
    short_summary: str = Field(default="", max_length=500)
    system_prompt: str = Field(min_length=1, max_length=200_000)


class PatchBody(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    short_summary: str | None = Field(default=None, max_length=500)
    system_prompt: str | None = Field(default=None, max_length=200_000)


class HumanlikeOut(BaseModel):
    enabled: bool
    persona_id: str | None


class HumanlikeIn(BaseModel):
    enabled: bool
    persona_id: str | None = None


class AssetOut(BaseModel):
    """Wire view of one persona asset record."""

    id: str
    persona_id: str
    kind: Literal["emoji", "reference"]
    label: str
    file_name: str
    mime: str
    size_bytes: int
    sha256: str
    created_at_ms: int
    # Convenience URL the UI uses directly in <img src=…>.
    url: str

    @classmethod
    def from_record(cls, r: AssetRecord) -> AssetOut:
        return cls(
            id=r.id,
            persona_id=r.persona_id,
            kind=r.kind,
            label=r.label,
            file_name=r.file_name,
            mime=r.mime,
            size_bytes=r.size_bytes,
            sha256=r.sha256,
            created_at_ms=r.created_at_ms,
            url=f"/admin/personas/{r.persona_id}/assets/{r.id}",
        )


class AssetListOut(BaseModel):
    assets: list[AssetOut]


# Slot label naming rule — same shape as persona ids. Forbidding
# slashes / dot-segments stops a malicious caller from prying open
# the persona dir structure via crafted labels.
_LABEL_PATTERN: re.Pattern[str] = re.compile(r"^[a-z0-9_-]{1,64}$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _persona_store(state: AdminState) -> PersonaStore:
    store = state.persona_store
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "persona_store_missing",
                "message": "gateway booted without a persona store",
            },
        )
    # AdminState holds the store as ``Any`` to avoid import coupling.
    return cast("PersonaStore", store)


def _asset_store(state: AdminState) -> PersonaAssetStore:
    store = state.persona_asset_store
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "persona_asset_store_missing",
                "message": "gateway booted without a persona asset store",
            },
        )
    # AdminState holds the store as ``Any`` to avoid import coupling.
    return cast("PersonaAssetStore", store)


async def _require_persona(
    persona_store: PersonaStore, persona_id: str
) -> None:
    """Raise 404 ``persona_not_found`` if the row doesn't exist. Used
    by every asset route so a typo in ``persona_id`` fails fast before
    we touch the asset store."""
    if await persona_store.get(persona_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "persona_not_found", "id": persona_id},
        )


def _validate_label(label: str) -> str:
    label = (label or "").strip()
    if not _LABEL_PATTERN.match(label):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_label",
                "message": "label must match [a-z0-9_-], 1-64 chars",
            },
        )
    return label


def _validate_kind(kind: str) -> AssetKind:
    if kind not in ("emoji", "reference"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_kind",
                "message": "kind must be 'emoji' or 'reference'",
            },
        )
    return kind  # type: ignore[return-value]


def _channels_writer(state: AdminState) -> Any:
    if state.channels_config is None or state.channels_writer is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "channels_writer_missing",
                "message": "no writable channels config wired",
            },
        )
    return state.channels_writer


def _qq_humanlike_block(state: AdminState) -> dict[str, Any]:
    """Read the live ``[channels.qq.humanlike]`` block. Returns an empty
    dict when the channel or the sub-section is missing.

    Kept for the legacy QQ-only routes; new code routes through
    :func:`_channel_humanlike_block` which takes the channel name."""
    return _channel_humanlike_block(state, "qq")


#: Channels that support the humanlike system-prompt injection. WeChat
#: Official + QQ Official are intentionally excluded — the former is
#: webhook-only and doesn't currently surface a persona path, and the
#: latter does its own per-platform message formatting that doesn't sit
#: alongside the spinner / footer machinery this initiative depends on.
SUPPORTED_HUMANLIKE_CHANNELS: frozenset[str] = frozenset(
    {"qq", "telegram", "discord", "slack", "feishu"}
)


def _validate_channel_name(channel: str) -> str:
    """Reject channel slugs that aren't in :data:`SUPPORTED_HUMANLIKE_CHANNELS`
    with a 404 ``unknown_channel`` — matches the rest of the gateway's
    "unknown {thing}" error envelope shape."""
    if channel not in SUPPORTED_HUMANLIKE_CHANNELS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "unknown_channel",
                "channel": channel,
                "supported": sorted(SUPPORTED_HUMANLIKE_CHANNELS),
            },
        )
    return channel


def _channel_humanlike_block(
    state: AdminState, channel: str
) -> dict[str, Any]:
    """Read the live ``[channels.{channel}.humanlike]`` block. Returns
    an empty dict when the channel or the sub-section is missing — same
    "missing == disabled" semantics the resolver in channels_runtime
    relies on so a half-configured TOML never crashes the GET path."""
    cfg = state.channels_config or {}
    section = cfg.get(channel)
    if not isinstance(section, dict):
        return {}
    hl = section.get("humanlike")
    return hl if isinstance(hl, dict) else {}
