"""Extracted module-level mass for the onboard route module.

This module was split out of ``onboard.py`` (the ``/admin/onboard*``
route file) to keep the route core small. It holds the wire-model
classes, module constants, and helper functions that ``router()`` and
its nested handlers depend on.

MUST NOT import the route module (``onboard``) — doing so would create
an import cycle. Use absolute imports only.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_b.state import (
    get_admin_state,
)

# ---------------------------------------------------------------------------
# First-run wizard helpers — pull in the username/password service logic from
# the routes_admin_a.auth module so we don't duplicate hashing, validation,
# and atomic-write semantics. These imports are deliberately lazy-friendly:
# the module is part of the same gateway package that boots admin_a and
# admin_b together, so the import always succeeds at runtime.
# ---------------------------------------------------------------------------


# Mirror the username constraints from ``routes_admin_a.auth`` so we can
# perform the same shape-level rejection without taking an indirect cookie
# dependency on the auth module's private name bindings. Kept as local
# module constants because the auth module re-exports them via the request
# dataclasses but not as standalone symbols.
_USERNAME_MAX_LEN = 64
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class FinalizeBody(BaseModel):
    """Generic provider-finalize payload.

    ``provider_name`` is the slot key used in ``[providers.<name>]``.
    ``kind`` must be one of :func:`list_supported_kinds` (e.g.
    ``"openai_compatible"``, ``"openai"``, ``"anthropic"``).
    ``model`` is set as the default model alias; ``embedding_model``
    (when present) seeds the ``[embedding]`` block pointed at the same
    provider.
    """

    provider_name: str
    kind: str
    base_url: str | None = None
    api_key: str | None = None
    model: str
    embedding_model: str | None = None


class FinalizeResponse(BaseModel):
    ok: bool = True
    redirect: str = "/login"


class FinalizeSkipResponse(BaseModel):
    """Response payload for ``POST /admin/onboard/finalize-skip``."""

    status: str = "ok"
    mode: str = "mock"


# ---------------------------------------------------------------------------
# First-run wizard wire shapes (B1–B4)
# ---------------------------------------------------------------------------


class FinalizeAccountBody(BaseModel):
    """B1: ``POST /admin/onboard/finalize-account`` request body.

    First-run flow trusts the authed session for the *old* password —
    operator authenticated with the default ``admin``/``root`` creds and
    we don't want to make them re-type their default password just to
    pick a username. The session-cookie check is the gatekeeper.
    """

    new_username: str = Field(min_length=1, max_length=_USERNAME_MAX_LEN)


class FinalizeAccountResponse(BaseModel):
    status: str = "ok"
    username: str


class FinalizePasswordBody(BaseModel):
    """B2: ``POST /admin/onboard/finalize-password`` request body."""

    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=1)


class FinalizePasswordResponse(BaseModel):
    status: str = "ok"
    must_change_password: bool = False


class FinalizePersonaBody(BaseModel):
    """B3: ``POST /admin/onboard/finalize-persona`` request body."""

    choice: Literal["skip", "default", "custom"]


class FinalizePersonaResponse(BaseModel):
    status: str = "ok"
    choice: str
    persona_id: str | None = None
    redirect: str | None = None


class ImageProviderSpec(BaseModel):
    """Slim wire shape for the ``separate`` branch of B4.

    Mirrors the canonical ``ProviderUpsert`` payload used by
    ``/admin/providers`` but keeps the field set narrow to the bits the
    image-provider configuration form actually surfaces. The handler
    upserts a ``[providers.<name>]`` block with ``image_capable=true``.
    """

    name: str = Field(min_length=1, max_length=64)
    kind: str
    base_url: str | None = None
    api_key: str | None = None
    image_model: str | None = None


class FinalizeImageProviderBody(BaseModel):
    """B4: ``POST /admin/onboard/finalize-image-provider`` request body.

    Schema is a discriminated union flavoured by ``choice``. The contract
    deliberately keeps every leaf optional so a single Pydantic class can
    parse all three branches; handler-side validation enforces the
    per-branch required fields.
    """

    choice: Literal["skip", "reuse", "separate"]
    provider_name: str | None = None
    spec: ImageProviderSpec | None = None


class FinalizeImageProviderResponse(BaseModel):
    status: str = "ok"
    choice: str
    image_provider: str | None = None
    evidence: str | None = None


def _bad(code: str, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code})


def _resolve_auth_state() -> Any:
    """Return the admin_a :class:`AdminState` (where the credentials +
    session store live).

    The first-run wizard endpoints run under the ``routes_admin_b``
    router but the canonical username / password / session state is
    populated on the admin_a side by the gateway lifecycle. Falling back
    to the admin_b state lets test harnesses that only build one of the
    two states still drive these endpoints.
    """
    try:
        from corlinman_server.gateway.routes_admin_a.state import (
            get_admin_state as _get_admin_a_state,
        )
    except Exception:  # pragma: no cover — admin_a missing
        return get_admin_state()
    try:
        state_a = _get_admin_a_state()
    except RuntimeError:
        # admin_a state not installed; admin_b's get_admin_state defaults
        # to an empty AdminState which the handler will recognise as
        # "no credentials" and 503 with a clean envelope.
        return get_admin_state()
    if state_a.admin_username is not None or state_a.admin_password_hash is not None:
        return state_a
    # admin_a is empty (e.g. degraded boot) — try admin_b which carries
    # the same field names.
    return get_admin_state()


def _read_session_cookie_from_request(request: Request) -> str | None:
    """Local copy of ``routes_admin_a.auth._read_session_cookie``.

    Re-implemented here so we don't reach into a sibling module's
    private name; the cookie name comes from the shared ``_session_store``
    constant which is the only stable hook.
    """
    from corlinman_server.gateway.routes_admin_a._session_store import (
        SESSION_COOKIE_NAME,
        extract_cookie,
    )

    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        return token
    raw = request.headers.get("cookie")
    if raw is None:
        return None
    return extract_cookie(raw, SESSION_COOKIE_NAME)
