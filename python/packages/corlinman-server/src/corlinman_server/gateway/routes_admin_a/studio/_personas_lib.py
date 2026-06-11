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
    model_bindings: dict[str, PersonaModelBinding]
    is_builtin: bool
    created_at_ms: int
    updated_at_ms: int
    # Admin-blob URL of the persona's avatar — the first ``emoji`` asset,
    # else the first ``reference`` 立绘, else ``None`` when the persona has
    # no assets. Filled in by the route layer (it needs the asset store);
    # :meth:`from_row` leaves it ``None`` so non-asset-aware callers stay
    # correct.
    avatar_url: str | None = None

    @classmethod
    def from_row(cls, p: Persona, *, avatar_url: str | None = None) -> PersonaOut:
        return cls(
            id=p.id,
            display_name=p.display_name,
            short_summary=p.short_summary,
            system_prompt=p.system_prompt,
            model_bindings=_model_bindings_out(p.model_bindings),
            is_builtin=p.is_builtin,
            created_at_ms=p.created_at_ms,
            updated_at_ms=p.updated_at_ms,
            avatar_url=avatar_url,
        )


class ListOut(BaseModel):
    personas: list[PersonaOut]


class PersonaModelBinding(BaseModel):
    provider: str | None = None
    model: str | None = None


def _empty_model_bindings_out() -> dict[str, PersonaModelBinding]:
    return {
        "text": PersonaModelBinding(),
        "image": PersonaModelBinding(),
        "voice": PersonaModelBinding(),
    }


def _model_bindings_out(
    raw: dict[str, Any] | None,
) -> dict[str, PersonaModelBinding]:
    out = _empty_model_bindings_out()
    if not isinstance(raw, dict):
        return out
    for kind in out:
        item = raw.get(kind)
        if not isinstance(item, dict):
            continue
        provider = item.get("provider")
        model = item.get("model")
        out[kind] = PersonaModelBinding(
            provider=provider if isinstance(provider, str) and provider else None,
            model=model if isinstance(model, str) and model else None,
        )
    return out


def _model_bindings_plain(
    raw: dict[str, PersonaModelBinding] | None,
) -> dict[str, dict[str, str | None]] | None:
    if raw is None:
        return None
    return {
        kind: binding.model_dump()
        for kind, binding in _model_bindings_out(
            {k: v.model_dump() for k, v in raw.items()}
        ).items()
    }


class CreateBody(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    display_name: str = Field(min_length=1, max_length=200)
    short_summary: str = Field(default="", max_length=500)
    system_prompt: str = Field(min_length=1, max_length=200_000)
    model_bindings: dict[str, PersonaModelBinding] = Field(default_factory=dict)


class PatchBody(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    short_summary: str | None = Field(default=None, max_length=500)
    system_prompt: str | None = Field(default=None, max_length=200_000)
    model_bindings: dict[str, PersonaModelBinding] | None = None


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


class AssetLabelPatch(BaseModel):
    """Rename one asset's slot label (``PATCH …/assets/{aid}``)."""

    label: str


# ---- Persona-liveness wire shapes (R3 life-state / diary / seeds) --------


class LifeStateOut(BaseModel):
    """Wire view of one ``agent_persona_state`` row (the runtime
    persona-STATE store, keyed ``(tenant_id="default", agent_id=id)``).

    Mirrors :class:`corlinman_persona.PersonaState` but flattens it to the
    shared API contract: ``recent_topics`` as a string list, ``state_json``
    as the free-form dict, and ``updated_at_ms`` defaulting to ``0`` when no
    row exists yet."""

    mood: str
    fatigue: float
    recent_topics: list[str]
    state_json: dict[str, Any]
    updated_at_ms: int


class LifeStatePatch(BaseModel):
    """Partial upsert of the runtime persona-STATE row. Every field is
    optional; omitted fields are preserved from the existing row (or the
    contract defaults when no row exists). Doubles as a manual seed /
    override path for an operator priming a persona's mood/fatigue."""

    mood: str | None = Field(default=None, max_length=200)
    fatigue: float | None = Field(default=None, ge=0.0, le=1.0)
    recent_topics: list[str] | None = None


class DiaryEntryOut(BaseModel):
    """One diary line, normalised to the shared contract shape. The
    underlying ``state_json["diary"]`` records (written by the agent's
    ``persona_life_diary_add`` tool) carry an ISO ``ts`` + ``entry`` text;
    we surface them as ``ts`` (epoch-ms, ``0`` when unparseable) + ``text``."""

    ts: int
    text: str


class DiaryOut(BaseModel):
    entries: list[DiaryEntryOut]


class LifeSeedsOut(BaseModel):
    """The effective event-seed pack rendered as YAML text, plus which
    layer it resolved from (operator override → bundled pack → generic)."""

    yaml: str
    source: Literal["override", "bundled", "generic"]


class LifeSeedsIn(BaseModel):
    """Operator-authored event-seed override (raw YAML body)."""

    yaml: str


class OkOut(BaseModel):
    ok: bool


class DecayOut(BaseModel):
    rows_changed: int


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


async def _avatar_url_for(
    asset_store: PersonaAssetStore | None, persona_id: str
) -> str | None:
    """Resolve a persona's avatar blob URL.

    Prefers the persona's first ``emoji`` asset, falling back to its first
    ``reference`` 立绘, and returns ``None`` when the persona has no assets
    (or no asset store is wired). The URL shape matches
    :meth:`AssetOut.from_record` so the UI can drop it straight into an
    ``<img src=…>``. ``list()`` already returns assets sorted by
    ``label ASC`` so "first" is stable across calls.
    """
    if asset_store is None:
        return None
    for kind in ("emoji", "reference"):
        assets = await asset_store.list(persona_id, kind=kind)
        if assets:
            a = assets[0]
            return f"/admin/personas/{a.persona_id}/assets/{a.id}"
    return None


def _life_state_db_path(state: AdminState) -> Any:
    """Resolve the runtime persona-STATE DB path (``agent_state.sqlite``).

    The life-STATE store is opened lazily per-request off ``data_dir`` —
    the same path ``c2_wiring`` / the ``persona.decay`` builtin use — so
    these routes don't need a second long-lived handle wired onto
    :class:`AdminState`. 503s ``data_dir_missing`` when the gateway booted
    without a writable data dir."""
    if state.data_dir is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "data_dir_missing",
                "message": "gateway booted without a writable data dir",
            },
        )
    return state.data_dir / "agent_state.sqlite"


def _parse_iso_ms(value: Any) -> int:
    """Best-effort ISO-8601 (or already-numeric) → epoch-ms.

    The diary records written by ``persona_life_diary_add`` store ``ts`` as
    an ISO timestamp; an operator-seeded row may store an int. Returns
    ``0`` for anything unparseable rather than raising — the diary read
    path must never 500 on a single malformed entry."""
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip():
        from datetime import datetime  # noqa: PLC0415 — lazy, read-path only

        try:
            return int(datetime.fromisoformat(value).timestamp() * 1000)
        except ValueError:
            return 0
    return 0


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


#: Channels that support the humanlike system-prompt injection. The two
#: "official" platforms (QQ Official ``api.sgroup.qq.com`` + WeChat
#: Official webhook) were wired into the humanlike resolver in Wave 2, so
#: their runtime persona binding is now toggleable here too — leaving them
#: out 404'd ``PUT /admin/channels/{qq_official,wechat_official}/humanlike``
#: and operators couldn't flip the binding the channel runtime reads.
SUPPORTED_HUMANLIKE_CHANNELS: frozenset[str] = frozenset(
    {
        "qq",
        "telegram",
        "discord",
        "slack",
        "feishu",
        "qq_official",
        "wechat_official",
    }
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
