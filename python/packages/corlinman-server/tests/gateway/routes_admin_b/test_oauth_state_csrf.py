"""OAuth login-CSRF: the four admin submit flows must validate the CSRF
``state`` returned in the callback against the per-session state minted at
the authorize step.

Regression coverage for audit finding R4-D1. Before the fix:

* xai / codex / gemini ``/submit`` only compared ``state`` when the client
  *chose* to send a non-empty value, so an attacker who omitted ``state``
  (or sent an empty one) bypassed the check entirely.
* anthropic ``/submit`` never compared the callback state at all — the
  PKCE driver parsed it and forwarded it but never rejected a mismatch.

The legitimate UI (``ui/components/admin/oauth-login-modal.tsx``) ALWAYS
sends a non-empty ``state`` (it refuses to submit otherwise — see the
``errorBothRequired`` guard and the ``OAuthSubmitRequest`` type whose
``state`` field is non-optional). So all four providers REQUIRE state:
absent-or-mismatched is rejected with ``state_mismatch``.

For anthropic the callback shape is ``<code>#<state>`` and the minted
state equals the PKCE verifier, so the legitimate flow always echoes the
correct state back. When the callback genuinely omits the ``#state``
suffix the driver may fall back to the expected state (an operator who
pastes a bare code is not an attacker substituting a forged state), but a
callback state that is *present and wrong* must be rejected.

Network exchanges are mocked so the ONLY thing that can reject a request
is the state guard — that keeps these tests honest about what they prove.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from corlinman_server.gateway.oauth import anthropic_pkce, sessions
from corlinman_server.gateway.routes_admin_b import oauth as oauth_routes
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._admin_auth import authenticated_test_client, configure_admin_auth

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def oauth_client(tmp_path: Path) -> Iterator[TestClient]:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("", encoding="utf-8")
    snapshot: dict[str, Any] = {}

    state = AdminState(config_loader=lambda: dict(snapshot), config_path=cfg_path)
    state.data_dir = tmp_path  # type: ignore[attr-defined]
    configure_admin_auth(state)
    set_admin_state(state)
    sessions._reset_for_tests()

    app = FastAPI()
    app.include_router(oauth_routes.router())
    try:
        yield authenticated_test_client(app)
    finally:
        set_admin_state(None)
        sessions._reset_for_tests()


_FAKE_TOKENS = {
    "access_token": "fake-access",
    "refresh_token": "fake-refresh",
    "expires_at_ms": 9_999_999_999_999,
    "scope": "scope",
}


# ---------------------------------------------------------------------------
# xAI
# ---------------------------------------------------------------------------


def _start_xai(client: TestClient) -> str:
    discovery = {
        "authorization_endpoint": "https://accounts.x.ai/authorize",
        "token_endpoint": "https://accounts.x.ai/token",
    }
    with patch(
        "corlinman_server.gateway.oauth.xai_pkce.discover_endpoints",
        new=AsyncMock(return_value=discovery),
    ):
        resp = client.post("/admin/oauth/xai/start")
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


class TestXaiStateGuard:
    def test_mismatched_state_rejected(self, oauth_client: TestClient) -> None:
        sid = _start_xai(oauth_client)
        with patch(
            "corlinman_server.gateway.oauth.xai_pkce.exchange_code",
            new=AsyncMock(return_value=_FAKE_TOKENS),
        ):
            resp = oauth_client.post(
                "/admin/oauth/xai/submit",
                json={"session_id": sid, "code": "thecode", "state": "WRONG"},
            )
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"] == "state_mismatch"

    def test_absent_state_rejected(self, oauth_client: TestClient) -> None:
        sid = _start_xai(oauth_client)
        with patch(
            "corlinman_server.gateway.oauth.xai_pkce.exchange_code",
            new=AsyncMock(return_value=_FAKE_TOKENS),
        ):
            resp = oauth_client.post(
                "/admin/oauth/xai/submit",
                json={"session_id": sid, "code": "thecode"},
            )
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"] == "state_mismatch"

    def test_matching_state_accepted(self, oauth_client: TestClient) -> None:
        sid = _start_xai(oauth_client)
        record = sessions.get_session(sid)
        assert record is not None
        good_state = record["state"]
        with patch(
            "corlinman_server.gateway.oauth.xai_pkce.exchange_code",
            new=AsyncMock(return_value=_FAKE_TOKENS),
        ):
            resp = oauth_client.post(
                "/admin/oauth/xai/submit",
                json={"session_id": sid, "code": "thecode", "state": good_state},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


def _start_codex(client: TestClient) -> str:
    resp = client.post("/admin/oauth/codex/start")
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


class TestCodexStateGuard:
    def test_mismatched_state_rejected(self, oauth_client: TestClient) -> None:
        sid = _start_codex(oauth_client)
        with patch(
            "corlinman_server.gateway.oauth.codex_pkce.exchange_code",
            new=AsyncMock(return_value=_FAKE_TOKENS),
        ), patch("corlinman_server.gateway.oauth.codex_pkce.write_auth_json"):
            resp = oauth_client.post(
                "/admin/oauth/codex/submit",
                json={"session_id": sid, "code": "thecode", "state": "WRONG"},
            )
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"] == "state_mismatch"

    def test_absent_state_rejected(self, oauth_client: TestClient) -> None:
        sid = _start_codex(oauth_client)
        with patch(
            "corlinman_server.gateway.oauth.codex_pkce.exchange_code",
            new=AsyncMock(return_value=_FAKE_TOKENS),
        ), patch("corlinman_server.gateway.oauth.codex_pkce.write_auth_json"):
            resp = oauth_client.post(
                "/admin/oauth/codex/submit",
                json={"session_id": sid, "code": "thecode"},
            )
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"] == "state_mismatch"

    def test_matching_state_accepted(self, oauth_client: TestClient) -> None:
        sid = _start_codex(oauth_client)
        record = sessions.get_session(sid)
        assert record is not None
        good_state = record["state"]
        with patch(
            "corlinman_server.gateway.oauth.codex_pkce.exchange_code",
            new=AsyncMock(return_value=_FAKE_TOKENS),
        ), patch("corlinman_server.gateway.oauth.codex_pkce.write_auth_json"):
            resp = oauth_client.post(
                "/admin/oauth/codex/submit",
                json={"session_id": sid, "code": "thecode", "state": good_state},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


def _start_gemini(client: TestClient) -> str:
    resp = client.post("/admin/oauth/gemini/start")
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


class TestGeminiStateGuard:
    def test_mismatched_state_rejected(self, oauth_client: TestClient) -> None:
        sid = _start_gemini(oauth_client)
        with patch(
            "corlinman_server.gateway.oauth.gemini_pkce.exchange_code",
            new=AsyncMock(return_value=_FAKE_TOKENS),
        ), patch("corlinman_server.gateway.oauth.gemini_pkce.write_creds_json"):
            resp = oauth_client.post(
                "/admin/oauth/gemini/submit",
                json={"session_id": sid, "code": "thecode", "state": "WRONG"},
            )
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"] == "state_mismatch"

    def test_absent_state_rejected(self, oauth_client: TestClient) -> None:
        sid = _start_gemini(oauth_client)
        with patch(
            "corlinman_server.gateway.oauth.gemini_pkce.exchange_code",
            new=AsyncMock(return_value=_FAKE_TOKENS),
        ), patch("corlinman_server.gateway.oauth.gemini_pkce.write_creds_json"):
            resp = oauth_client.post(
                "/admin/oauth/gemini/submit",
                json={"session_id": sid, "code": "thecode"},
            )
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"] == "state_mismatch"

    def test_matching_state_accepted(self, oauth_client: TestClient) -> None:
        sid = _start_gemini(oauth_client)
        record = sessions.get_session(sid)
        assert record is not None
        good_state = record["state"]
        with patch(
            "corlinman_server.gateway.oauth.gemini_pkce.exchange_code",
            new=AsyncMock(return_value=_FAKE_TOKENS),
        ), patch("corlinman_server.gateway.oauth.gemini_pkce.write_creds_json"):
            resp = oauth_client.post(
                "/admin/oauth/gemini/submit",
                json={"session_id": sid, "code": "thecode", "state": good_state},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# Anthropic (driven at the exchange_code level — the route delegates the
# state check into the PKCE driver)
# ---------------------------------------------------------------------------


class TestAnthropicExchangeStateGuard:
    @pytest.mark.asyncio
    async def test_mismatched_callback_state_rejected(self) -> None:
        """``CODE#WRONGSTATE`` with a different ``expected_state`` must be
        rejected before any network call is made."""

        def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("token endpoint must not be hit on state mismatch")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(anthropic_pkce.OAuthExchangeError, match="state"):
                await anthropic_pkce.exchange_code(
                    code_input="CODE#WRONGSTATE",
                    code_verifier="verifier-123",
                    expected_state="REALSTATE",
                    client=client,
                )

    @pytest.mark.asyncio
    async def test_matching_callback_state_accepted(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"access_token": "a", "refresh_token": "r", "expires_in": 60},
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            out = await anthropic_pkce.exchange_code(
                code_input="CODE#REALSTATE",
                code_verifier="v",
                expected_state="REALSTATE",
                client=client,
            )
        assert out["access_token"] == "a"
