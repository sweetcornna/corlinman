"""Security tests for the ``/v1/voice`` WebSocket surface.

Covers two confirmed defects:

* **S1** — the WebSocket handshake had *no* authentication. The only gate
  was :class:`ApiKeyAuthMiddleware`, a Starlette ``BaseHTTPMiddleware``
  that structurally never runs for WebSocket scopes, so an
  unauthenticated client could open ``ws://host/v1/voice``, drive a real
  provider session billed to the operator key, and spoof ``X-Tenant-Id``
  to exhaust per-tenant budget.
* **S2** — :func:`audio_path_for` / :func:`tts_audio_path_for`
  interpolated the (unauthenticated) ``tenant_id`` verbatim, so a
  ``../../..`` tenant escaped ``data_dir/tenants`` on write when
  ``retain_audio = true``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from corlinman_server.gateway.middleware.auth import ApiKeyAuthState
from corlinman_server.gateway.routes_voice.framing import SUBPROTOCOL
from corlinman_server.gateway.routes_voice.mod import (
    CLOSE_CODE_AUTH_DENIED,
    CLOSE_CODE_NORMAL,
    VoiceRouterConfig,
    VoiceState,
    run_voice_session,
)
from corlinman_server.gateway.routes_voice.persistence import (
    VoicePathError,
    audio_path_for,
    tts_audio_path_for,
)
from corlinman_server.gateway.routes_voice.provider import MockVoiceProvider
from corlinman_server.tenancy import AdminDb, TenantId

# ``asyncio_mode = "auto"`` (root pyproject) auto-detects the ``async def``
# tests, so no module-level ``pytest.mark.asyncio`` is needed — and adding
# one would mis-mark the synchronous path-traversal tests.


# ---------------------------------------------------------------------------
# In-memory WebSocket double (mirrors test_route_integration.FakeWebSocket)
# ---------------------------------------------------------------------------


class _ConnState:
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


class _AppShim:
    """Stand-in for ``websocket.app`` exposing ``.state.api_key_auth``."""

    def __init__(self, api_key_auth: ApiKeyAuthState | None) -> None:
        self.state = type("_State", (), {"api_key_auth": api_key_auth})()


class FakeWebSocket:
    """Minimal Starlette ``WebSocket`` stand-in for :func:`run_voice_session`."""

    def __init__(
        self,
        *,
        subprotocol: str | None = SUBPROTOCOL,
        headers: dict[str, str] | None = None,
        query_params: dict[str, str] | None = None,
        api_key_auth: ApiKeyAuthState | None = None,
    ) -> None:
        self.headers: dict[str, str] = dict(headers or {})
        if subprotocol is not None:
            self.headers["sec-websocket-protocol"] = subprotocol
        self.query_params: dict[str, str] = dict(query_params or {})
        self.app = _AppShim(api_key_auth)
        self._incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.accepted_subprotocol: str | None = None
        self.accepted = False
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self.client_state = _ConnState.CONNECTED

    def queue_text(self, text: str) -> None:
        self._incoming.put_nowait({"type": "websocket.receive", "text": text})

    def queue_bytes(self, data: bytes) -> None:
        self._incoming.put_nowait({"type": "websocket.receive", "bytes": data})

    def queue_disconnect(self) -> None:
        self._incoming.put_nowait({"type": "websocket.disconnect"})

    async def accept(self, subprotocol: str | None = None) -> None:
        self.accepted = True
        self.accepted_subprotocol = subprotocol

    async def receive(self) -> dict[str, Any]:
        return await self._incoming.get()

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_code = code
        self.close_reason = reason
        self.client_state = _ConnState.DISCONNECTED

    @property
    def sent_control(self) -> list[dict[str, Any]]:
        return [json.loads(t) for t in self.sent_text]


def _voice_state(
    *, auth_state: ApiKeyAuthState | None, data_dir: Path
) -> VoiceState:
    cfg = VoiceRouterConfig(
        enabled=True,
        provider_alias="mock",
        budget_minutes_per_tenant_per_day=60,
        default_tenant="default",
    )
    return VoiceState(
        config_loader=lambda: cfg,
        provider=MockVoiceProvider(),
        data_dir=data_dir,
        auth_state=auth_state,
    )


# ---------------------------------------------------------------------------
# Fixtures: a real AdminDb with a single minted key under tenant "acme"
# ---------------------------------------------------------------------------


@pytest.fixture
async def admin_db_with_key(tmp_path: Path):
    db = await AdminDb.open(tmp_path / "tenants.sqlite")
    try:
        tenant = TenantId.new("acme")
        await db.create_tenant(tenant, "Acme Inc", created_at=0)
        minted = await db.mint_api_key(tenant, "operator", "voice", None)
        yield db, minted.token
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# S1 — handshake authentication
# ---------------------------------------------------------------------------


async def test_unauthenticated_voice_connection_is_rejected(
    admin_db_with_key, tmp_path: Path
) -> None:
    """No token + a spoofed X-Tenant-Id must be rejected BEFORE the
    provider session opens (currently the bug: it is accepted)."""
    db, _token = admin_db_with_key
    auth_state = ApiKeyAuthState(admin_db=db)

    ws = FakeWebSocket(
        headers={"x-tenant-id": "victim-tenant"},
        api_key_auth=auth_state,
    )
    # A start frame the buggy path would consume to open a real session.
    ws.queue_text(json.dumps({"type": "start", "session_key": "sk-attack"}))

    await asyncio.wait_for(
        run_voice_session(ws, _voice_state(auth_state=auth_state, data_dir=tmp_path)),
        timeout=5.0,
    )

    # A `started` event means a billable provider session was opened — the
    # defect. The fix must prevent it and close with the auth-denied code.
    assert not any(
        c.get("type") == "started" for c in ws.sent_control
    ), "unauthenticated client opened a billable voice session (S1)"
    assert ws.close_code == CLOSE_CODE_AUTH_DENIED


async def test_invalid_token_voice_connection_is_rejected(
    admin_db_with_key, tmp_path: Path
) -> None:
    db, _token = admin_db_with_key
    auth_state = ApiKeyAuthState(admin_db=db)

    ws = FakeWebSocket(
        headers={"authorization": "Bearer ck_not_a_real_token"},
        api_key_auth=auth_state,
    )
    ws.queue_text(json.dumps({"type": "start", "session_key": "sk-attack"}))

    await asyncio.wait_for(
        run_voice_session(ws, _voice_state(auth_state=auth_state, data_dir=tmp_path)),
        timeout=5.0,
    )

    assert not any(c.get("type") == "started" for c in ws.sent_control)
    assert ws.close_code == CLOSE_CODE_AUTH_DENIED


async def test_valid_token_via_authorization_header_connects(
    admin_db_with_key, tmp_path: Path
) -> None:
    db, token = admin_db_with_key
    auth_state = ApiKeyAuthState(admin_db=db)

    ws = FakeWebSocket(
        headers={"authorization": f"Bearer {token}"},
        api_key_auth=auth_state,
    )
    ws.queue_text(json.dumps({"type": "start", "session_key": "sk-ok"}))
    ws.queue_text(json.dumps({"type": "end"}))

    await asyncio.wait_for(
        run_voice_session(ws, _voice_state(auth_state=auth_state, data_dir=tmp_path)),
        timeout=5.0,
    )

    assert any(c.get("type") == "started" for c in ws.sent_control)
    assert ws.close_code == CLOSE_CODE_NORMAL


async def test_valid_token_via_query_param_connects(
    admin_db_with_key, tmp_path: Path
) -> None:
    db, token = admin_db_with_key
    auth_state = ApiKeyAuthState(admin_db=db)

    ws = FakeWebSocket(
        query_params={"api_key": token},
        api_key_auth=auth_state,
    )
    ws.queue_text(json.dumps({"type": "start", "session_key": "sk-ok"}))
    ws.queue_text(json.dumps({"type": "end"}))

    await asyncio.wait_for(
        run_voice_session(ws, _voice_state(auth_state=auth_state, data_dir=tmp_path)),
        timeout=5.0,
    )

    assert any(c.get("type") == "started" for c in ws.sent_control)
    assert ws.close_code == CLOSE_CODE_NORMAL


async def test_tenant_bound_to_authenticated_key_not_spoofed_header(
    admin_db_with_key, tmp_path: Path
) -> None:
    """The authenticated key's tenant must win over a spoofed
    X-Tenant-Id header so an attacker can't drain another tenant's
    budget."""
    db, token = admin_db_with_key
    auth_state = ApiKeyAuthState(admin_db=db)

    state = _voice_state(auth_state=auth_state, data_dir=tmp_path)

    ws = FakeWebSocket(
        headers={
            "authorization": f"Bearer {token}",
            "x-tenant-id": "some-other-tenant",
        },
        api_key_auth=auth_state,
    )
    ws.queue_text(json.dumps({"type": "start", "session_key": "sk-ok"}))
    ws.queue_text(json.dumps({"type": "end"}))

    await asyncio.wait_for(
        run_voice_session(ws, state), timeout=5.0
    )

    # The session must have been billed against the key's tenant ("acme"),
    # never the spoofed header value.
    day = _any_day(state)
    assert state.spend.snapshot("acme", day).sessions_count >= 1
    assert state.spend.snapshot("some-other-tenant", day).sessions_count == 0


def _any_day(state: VoiceState) -> int:
    from corlinman_server.gateway.routes_voice.cost import (
        now_unix_secs,
        utc_day_epoch,
    )

    return utc_day_epoch(now_unix_secs())


# ---------------------------------------------------------------------------
# S2 — path traversal in retained-audio path resolution
# ---------------------------------------------------------------------------


def test_audio_path_for_rejects_traversal_tenant() -> None:
    """``../../etc`` previously escaped ``/data/tenants`` →
    ``/etc/voice/sid.pcm``. It must now be rejected."""
    with pytest.raises(VoicePathError):
        audio_path_for(Path("/data"), "../../etc", "sid")


def test_tts_audio_path_for_rejects_traversal_tenant() -> None:
    with pytest.raises(VoicePathError):
        tts_audio_path_for(Path("/data"), "../../etc", "sid")


def test_audio_path_for_rejects_traversal_session_id() -> None:
    with pytest.raises(VoicePathError):
        audio_path_for(Path("/data"), "acme", "../../../escape")


def test_audio_path_for_rejects_separators() -> None:
    with pytest.raises(VoicePathError):
        audio_path_for(Path("/data"), "a/b", "sid")
    with pytest.raises(VoicePathError):
        audio_path_for(Path("/data"), "acme", "a\\b")


def test_audio_path_for_keeps_valid_shape() -> None:
    data_dir = Path("/data")
    path = audio_path_for(data_dir, "acme", "voice-1234")
    assert path == data_dir / "tenants" / "acme" / "voice" / "voice-1234.pcm"
    # The legitimate path stays one directory deep under tenants/.
    assert path.resolve().relative_to((data_dir / "tenants").resolve())
