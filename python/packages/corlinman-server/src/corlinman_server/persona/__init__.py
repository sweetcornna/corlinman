"""Human-like persona registry — system_prompt blocks for chat channels.

This is the gateway-internal persona store that channels read at chat
time to prepend a "speak like X" system message to the agent request.
It is **not** the runtime persona-state store with affection / decay
counters (that lives in :mod:`corlinman_persona`, the separately-
packaged ``corlinman-persona`` distribution). The two names overlap on
purpose: the system-prompt body shapes how the agent speaks, while the
state store shapes who it remembers feeling things about.

Public surface
--------------

* :class:`Persona` — frozen dataclass row.
* :class:`PersonaStore` — async aiosqlite CRUD.
* :func:`seed_builtin_personas` — idempotent first-boot seeder.
* :data:`DEFAULT_GRANTLEY_ID` — stable id of the seeded ``grantley``
  persona (other modules should reference this constant rather than
  re-spelling the literal string).
"""

from __future__ import annotations

from corlinman_server.persona.asset_store import (
    ALLOWED_MIMES,
    DEFAULT_MAX_BYTES_PER_ASSET,
    DEFAULT_MAX_BYTES_PER_PERSONA,
    AssetError,
    AssetKind,
    AssetMimeRejected,
    AssetNotFound,
    AssetQuotaExceeded,
    AssetRecord,
    AssetTooLarge,
    PersonaAssetStore,
)
from corlinman_server.persona.default_grantley import (
    DEFAULT_GRANTLEY_DISPLAY_NAME,
    DEFAULT_GRANTLEY_ID,
    DEFAULT_GRANTLEY_SUMMARY,
    load_default_grantley_body,
)
from corlinman_server.persona.store import (
    Persona,
    PersonaError,
    PersonaExists,
    PersonaProtected,
    PersonaStore,
    seed_builtin_personas,
)

__all__ = [
    "ALLOWED_MIMES",
    "DEFAULT_GRANTLEY_DISPLAY_NAME",
    "DEFAULT_GRANTLEY_ID",
    "DEFAULT_GRANTLEY_SUMMARY",
    "DEFAULT_MAX_BYTES_PER_ASSET",
    "DEFAULT_MAX_BYTES_PER_PERSONA",
    "AssetError",
    "AssetKind",
    "AssetMimeRejected",
    "AssetNotFound",
    "AssetQuotaExceeded",
    "AssetRecord",
    "AssetTooLarge",
    "Persona",
    "PersonaAssetStore",
    "PersonaError",
    "PersonaExists",
    "PersonaProtected",
    "PersonaStore",
    "load_default_grantley_body",
    "seed_builtin_personas",
]
