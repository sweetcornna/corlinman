"""WebSocket handshake authentication helpers for the voice surface.

Extracted verbatim from
:mod:`corlinman_server.gateway.routes_voice.mod` as part of a
behaviour-preserving god-file split. This module MUST NOT import the
source ``mod`` module (no cycle). The ``VoiceState`` type used in
annotations is imported under :data:`typing.TYPE_CHECKING` only (the
runtime annotations are strings via ``from __future__ import
annotations``), and the ``WS_TOKEN_SUBPROTOCOL_PREFIX`` constant comes
from the sibling :mod:`._constants_config`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import WebSocket

from corlinman_server.gateway.middleware.auth import (
    ApiKeyAuthState,
    extract_bearer_token,
)
from corlinman_server.gateway.routes_voice._constants_config import (
    WS_TOKEN_SUBPROTOCOL_PREFIX,
)

if TYPE_CHECKING:
    from corlinman_server.gateway.routes_voice.mod import VoiceState

logger = logging.getLogger("corlinman_server.gateway.routes_voice")


def _resolve_auth_state(
    state: VoiceState, websocket: WebSocket
) -> ApiKeyAuthState | None:
    """Resolve the :class:`ApiKeyAuthState` to authenticate against.

    Prefers the explicitly-wired :attr:`VoiceState.auth_state`; otherwise
    falls back to ``websocket.app.state.api_key_auth`` (the same instance
    the HTTP :class:`ApiKeyAuthMiddleware` publishes at boot). Returns
    ``None`` only when neither is present — the direct unit-test driver,
    where the in-memory socket has no ``app`` and the test builds a
    :class:`VoiceState` without an ``auth_state``.
    """
    if state.auth_state is not None:
        return state.auth_state
    app = getattr(websocket, "app", None)
    app_state = getattr(app, "state", None)
    candidate = getattr(app_state, "api_key_auth", None)
    if isinstance(candidate, ApiKeyAuthState):
        return candidate
    return None


def _extract_ws_token(websocket: WebSocket) -> str | None:
    """Pull the bearer token a WebSocket client supplied.

    Header path first — ``Authorization: Bearer <token>`` then
    ``X-API-Key`` — reusing :func:`extract_bearer_token` so the WS gate
    accepts exactly the same headers the HTTP gate does. As a
    browser-compatible fallback (the WebSocket API can't set request
    headers), a token offered via the ``Sec-WebSocket-Protocol``
    subprotocol list as ``corlinman.voice.token.<token>`` is honoured —
    the standard, browser-settable WS auth channel that, unlike a
    query-string parameter, does NOT land in uvicorn's access log.

    The query string is deliberately NOT consulted: uvicorn logs the
    full path-with-query on every WS accept (and on the 4401 deny path,
    which accepts before close), so a ``?api_key=`` fallback would write
    the tenant key verbatim to the access log on every connect.
    """
    token = extract_bearer_token(websocket)
    if token:
        return token
    return _extract_subprotocol_token(
        websocket.headers.get("sec-websocket-protocol")
    )


def _extract_subprotocol_token(header: str | None) -> str | None:
    """Extract a ``corlinman.voice.token.<token>`` value from the
    comma-separated ``Sec-WebSocket-Protocol`` offer list.

    The first token-carrying entry wins; the canonical
    :data:`SUBPROTOCOL` entry and any blanks are skipped. Returns
    ``None`` when no token entry is present.
    """
    if not header:
        return None
    for raw in header.split(","):
        entry = raw.strip()
        if entry.startswith(WS_TOKEN_SUBPROTOCOL_PREFIX):
            token = entry[len(WS_TOKEN_SUBPROTOCOL_PREFIX):].strip()
            if token:
                return token
    return None


async def _authenticate_ws(
    state: VoiceState, websocket: WebSocket
) -> tuple[bool, str | None]:
    """Authenticate the WebSocket handshake against the tenant API-key
    store, mirroring :class:`ApiKeyAuthMiddleware`.

    Returns ``(allowed, authenticated_tenant)``:

    * ``(True, None)`` — no auth state is resolvable at all (the direct
      unit-test driver). The caller proceeds without a key check.
    * ``(True, "<tenant-slug>")`` — a valid key was verified; the caller
      must bind the session to this tenant.
    * ``(False, None)`` — auth is required (an :class:`ApiKeyAuthState`
      is wired) but the key was missing / invalid / unverifiable. The
      caller must close :data:`CLOSE_CODE_AUTH_DENIED` without opening a
      provider session.
    """
    auth_state = _resolve_auth_state(state, websocket)
    if auth_state is None:
        # No gate wired (direct unit-test driver). Production always
        # publishes ``app.state.api_key_auth`` at boot.
        return True, None

    if auth_state.admin_db is None:
        # Fail closed — same posture as the HTTP middleware when the
        # protected route has nothing to verify against.
        logger.warning("voice: auth required but no admin_db configured; denying")
        return False, None

    token = _extract_ws_token(websocket)
    if token is None:
        logger.debug("voice: handshake missing api key; denying")
        return False, None

    try:
        row = await auth_state.admin_db.verify_api_key(token)
    except Exception:  # noqa: BLE001 — surface as auth-denied, log details
        logger.warning("voice: api key verification raised; denying", exc_info=True)
        return False, None

    if row is None:
        logger.debug("voice: handshake api key invalid/revoked; denying")
        return False, None

    return True, str(row.tenant_id)
