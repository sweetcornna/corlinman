"""Module-level support code extracted from ``credentials.py``.

Holds the whitelist/provenance metadata, wire models, and pure helper
functions for the ``/admin/credentials*`` surface. Extracted verbatim
from the route file as part of a behaviour-preserving god-file split.

This module MUST NOT import the route module
(``corlinman_server.gateway.routes_admin_b.config_admin.credentials``) —
that would create an import cycle.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Whitelist + provenance metadata
# ---------------------------------------------------------------------------


# Map of (provider name → ordered list of editable string fields). The order
# is the order the UI renders them in (api_key first, base_url next, etc).
_ALLOWED_FIELDS: dict[str, tuple[str, ...]] = {
    "openai": ("api_key", "base_url", "org_id"),
    "anthropic": ("api_key", "base_url"),
    "openrouter": ("api_key", "base_url"),
    "ollama": ("base_url",),
    "mock": (),
    "custom": ("api_key", "base_url", "kind"),
}


# Default kinds for each well-known provider — used when the block is
# absent and we synthesise an empty stub. ``custom`` carries no default
# kind because the operator picks it via the ``kind`` field.
_DEFAULT_KIND: dict[str, str] = {
    "openai": "openai",
    "anthropic": "anthropic",
    "openrouter": "openai_compatible",
    "ollama": "openai_compatible",
    "mock": "mock",
    "custom": "openai_compatible",
}


# Map of (provider, field) → conventional env-var name. Surfaces as
# ``env_ref`` in the GET response so operators recognise "I should set
# this in shell" without us probing ``os.environ`` ourselves.
_DEFAULT_ENV_REF: dict[tuple[str, str], str] = {
    ("openai", "api_key"): "OPENAI_API_KEY",
    ("openai", "base_url"): "OPENAI_BASE_URL",
    ("openai", "org_id"): "OPENAI_ORG_ID",
    ("anthropic", "api_key"): "ANTHROPIC_API_KEY",
    ("anthropic", "base_url"): "ANTHROPIC_BASE_URL",
    ("openrouter", "api_key"): "OPENROUTER_API_KEY",
    ("openrouter", "base_url"): "OPENROUTER_BASE_URL",
    ("ollama", "base_url"): "OLLAMA_BASE_URL",
    ("custom", "api_key"): "CUSTOM_API_KEY",
}


# Fields whose value must drive ``enabled = true`` when first written.
# For most providers the API key suffices; ollama is keyless so its
# base_url plays that role.
_PRIMARY_FIELD: dict[str, str] = {
    "openai": "api_key",
    "anthropic": "api_key",
    "openrouter": "api_key",
    "ollama": "base_url",
    "custom": "api_key",
}


# Well-known provider display order — UI walks this list when rendering
# placeholders for not-yet-configured providers.
_WELL_KNOWN_ORDER: tuple[str, ...] = (
    "openai",
    "anthropic",
    "openrouter",
    "ollama",
    "mock",
    "custom",
)


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class CredentialField(BaseModel):
    """One editable slot inside a ``[providers.<name>]`` block."""

    key: str
    set: bool = False
    preview: str | None = None
    env_ref: str | None = None


class CredentialProvider(BaseModel):
    name: str
    kind: str
    enabled: bool = False
    fields: list[CredentialField] = Field(default_factory=list)


class CredentialsListResponse(BaseModel):
    providers: list[CredentialProvider]


class SetCredentialBody(BaseModel):
    value: str


class EnableProviderBody(BaseModel):
    enabled: bool


class StatusOk(BaseModel):
    status: str = "ok"


class RevealResponse(BaseModel):
    """Cleartext value of a stored credential (auth-gated; never logged)."""

    value: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bad(code: str, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code})


def _mask_preview(value: str) -> str:
    """Return a sanitised display preview of a stored credential.

    The hermes EnvPage convention is "first 4 + '…' + last 4"; we shrink
    that to just "…last4" because most providers stash sk-… style
    prefixes that would leak provider identity if we exposed both ends.
    Literals shorter than 5 characters are rendered as ``***`` so the
    UI never echoes "ab…ab" for a 4-char string.
    """
    if not value:
        return "***"
    if len(value) < 5:
        return "***"
    return "…" + value[-4:]


def _resolve_field_view(
    provider: str,
    key: str,
    raw: Any,
) -> CredentialField:
    """Return the wire-shaped row for one whitelisted field.

    Handles all three storage shapes the config supports today:

    * absent / ``None`` → ``set=false`` (with a default ``env_ref``
      hint so the UI can show "set OPENAI_API_KEY" placeholder text).
    * ``"plain string"`` → ``set=true`` + ``preview="…last4"``.
    * ``{ "env": "FOO" }``  → ``set=true``, ``env_ref="FOO"``, no preview
      (we intentionally don't peek at the env var — that's the operator's
      truth source and reading it through the admin surface would leak it
      to the gateway logs on error paths).
    * ``{ "value": "sk-..." }`` → ``set=true`` + ``preview="…last4"``.
    """
    default_env_ref = _DEFAULT_ENV_REF.get((provider, key))
    if raw is None:
        return CredentialField(
            key=key, set=False, preview=None, env_ref=default_env_ref
        )
    if isinstance(raw, dict):
        if "env" in raw:
            env_name = str(raw["env"])
            return CredentialField(
                key=key,
                set=True,
                preview=None,
                env_ref=env_name or default_env_ref,
            )
        if "value" in raw:
            literal = str(raw.get("value") or "")
            return CredentialField(
                key=key,
                set=bool(literal),
                preview=_mask_preview(literal) if literal else None,
                env_ref=default_env_ref,
            )
        # Unknown dict shape — surface as set without preview so the
        # operator at least sees "something is here" and can replace it.
        return CredentialField(
            key=key, set=True, preview=None, env_ref=default_env_ref
        )
    # Plain literal (or any non-dict like int) — coerce to str for preview.
    literal = str(raw)
    return CredentialField(
        key=key,
        set=bool(literal),
        preview=_mask_preview(literal) if literal else None,
        env_ref=default_env_ref,
    )


def _resolve_raw_literal(raw: Any) -> str | None:
    """Extract the cleartext literal stored for a field, if any.

    Returns ``None`` for absent slots and for ``{env="FOO"}`` references —
    env-var-shaped credentials are intentionally opaque to the admin
    surface (the gateway never reads ``os.environ`` here, so there's no
    plaintext to return). Plain strings, ``{"value": "..."}`` dicts and
    coerce-to-str primitives all flow through.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        if "env" in raw:
            # Never resolve env vars through the admin surface — that
            # would leak operator secrets into our error/log paths.
            return None
        if "value" in raw:
            literal = raw.get("value")
            if literal is None or literal == "":
                return None
            return str(literal)
        return None
    literal = str(raw)
    return literal if literal else None


def _has_primary_set(provider: str, block: dict[str, Any]) -> bool:
    """Is the provider's primary field present + non-empty in the block?

    Drives the auto-flip of ``enabled``: writing the primary field for
    the first time turns the provider on; deleting it turns the provider
    off (but the rest of the block stays, so the UI keeps showing the
    placeholder row).
    """
    primary = _PRIMARY_FIELD.get(provider)
    if primary is None:
        # Providers without a primary field (e.g. mock) are always
        # "primed" once the block exists at all.
        return True
    raw = block.get(primary)
    if raw is None:
        return False
    if isinstance(raw, dict):
        if "env" in raw:
            return bool(raw.get("env"))
        if "value" in raw:
            return bool(raw.get("value"))
        return False
    return bool(str(raw))
