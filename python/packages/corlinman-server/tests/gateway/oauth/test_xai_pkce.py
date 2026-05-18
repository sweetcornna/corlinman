"""Tests for ``corlinman_server.gateway.oauth.xai_pkce``.

Coverage:

* PKCE pair generation: 43-char URL-safe base64 verifier; challenge is
  ``SHA256(verifier)`` URL-safe base64 no-pad.
* Authorize URL contains the verbatim hermes constants (``client_id``,
  ``scope``, ``plan=generic``, ``referrer=hermes-agent``,
  ``code_challenge_method=S256``).
* ``discover_endpoints`` returns the validated pair and rejects
  non-``x.ai`` endpoints (defence against MITM during first contact).
* ``exchange_code`` POSTs the form body to the discovered token endpoint
  with the four canonical fields and returns the coerced shape.
* ``refresh_token`` rotates when upstream emits a new refresh token and
  preserves the input when it doesn't (matches hermes line 3154).
* An expired stored credential triggers the refresh path end-to-end
  through ``storage`` helpers — the integration-style test mirrors the
  Anthropic suite's ``test_refresh_when_expired_triggers_refresh_path``.
"""

from __future__ import annotations

import base64
import hashlib
import time
import urllib.parse
from pathlib import Path

import httpx
import pytest

from corlinman_server.gateway.oauth import xai_pkce


# ---------------------------------------------------------------------------
# PKCE primitives
# ---------------------------------------------------------------------------


def test_generate_pkce_pair_shape() -> None:
    verifier, challenge = xai_pkce.generate_pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert all(c.isalnum() or c in "-_" for c in verifier)
    assert "=" not in verifier
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert challenge == expected


def test_generate_pkce_pair_changes_each_call() -> None:
    pairs = {xai_pkce.generate_pkce_pair()[0] for _ in range(8)}
    assert len(pairs) == 8


def test_state_and_nonce_are_uuid_hex() -> None:
    s = xai_pkce.generate_state()
    n = xai_pkce.generate_nonce()
    assert len(s) == 32
    assert len(n) == 32
    assert all(c in "0123456789abcdef" for c in s)
    assert all(c in "0123456789abcdef" for c in n)
    assert s != n


# ---------------------------------------------------------------------------
# Authorize URL — verbatim hermes constants
# ---------------------------------------------------------------------------


def test_build_authorize_url_contains_hermes_constants() -> None:
    url = xai_pkce.build_authorize_url(
        authorization_endpoint="https://accounts.x.ai/oauth/authorize",
        code_challenge="cc",
        state="st",
        nonce="nn",
    )
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    assert qs["client_id"] == [xai_pkce.XAI_OAUTH_CLIENT_ID]
    assert qs["scope"] == [xai_pkce.XAI_OAUTH_SCOPE]
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["code_challenge"] == ["cc"]
    assert qs["state"] == ["st"]
    assert qs["nonce"] == ["nn"]
    assert qs["plan"] == ["generic"]
    assert qs["referrer"] == ["hermes-agent"]
    assert qs["response_type"] == ["code"]


def test_authorize_url_uses_supplied_redirect_uri() -> None:
    url = xai_pkce.build_authorize_url(
        authorization_endpoint="https://accounts.x.ai/oauth/authorize",
        code_challenge="cc",
        state="st",
        nonce="nn",
        redirect_uri="http://127.0.0.1:9999/cb",
    )
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert qs["redirect_uri"] == ["http://127.0.0.1:9999/cb"]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_endpoints_success() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "authorization_endpoint": "https://accounts.x.ai/oauth/authorize",
                "token_endpoint": "https://auth.x.ai/oauth/token",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        out = await xai_pkce.discover_endpoints(client=client)

    assert captured["url"] == xai_pkce.XAI_OAUTH_DISCOVERY_URL
    assert out["authorization_endpoint"] == "https://accounts.x.ai/oauth/authorize"
    assert out["token_endpoint"] == "https://auth.x.ai/oauth/token"


@pytest.mark.asyncio
async def test_discover_rejects_non_xai_endpoints() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "authorization_endpoint": "https://attacker.example.com/auth",
                "token_endpoint": "https://attacker.example.com/token",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(xai_pkce.OAuthExchangeError, match="not under x.ai"):
            await xai_pkce.discover_endpoints(client=client)


@pytest.mark.asyncio
async def test_discover_rejects_http_scheme() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "authorization_endpoint": "http://accounts.x.ai/auth",
                "token_endpoint": "https://auth.x.ai/oauth/token",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(xai_pkce.OAuthExchangeError, match="https"):
            await xai_pkce.discover_endpoints(client=client)


@pytest.mark.asyncio
async def test_discover_handles_500() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream down")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(xai_pkce.OAuthExchangeError, match="HTTP 500"):
            await xai_pkce.discover_endpoints(client=client)


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exchange_code_posts_form_body() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["content_type"] = request.headers.get("content-type")
        captured["body"] = dict(urllib.parse.parse_qsl(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "access_token": "at-1",
                "refresh_token": "rt-1",
                "expires_in": 3600,
                "scope": "openid",
                "id_token": "id-token-xyz",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        out = await xai_pkce.exchange_code(
            token_endpoint="https://auth.x.ai/oauth/token",
            code="auth-code",
            code_verifier="ver-1",
            client=client,
        )

    assert captured["url"] == "https://auth.x.ai/oauth/token"
    assert "application/x-www-form-urlencoded" in str(captured["content_type"])
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "auth-code"
    assert body["code_verifier"] == "ver-1"
    assert body["client_id"] == xai_pkce.XAI_OAUTH_CLIENT_ID

    assert out["access_token"] == "at-1"
    assert out["refresh_token"] == "rt-1"
    assert out["scope"] == "openid"
    assert out["id_token"] == "id-token-xyz"
    assert isinstance(out["expires_at_ms"], int) and out["expires_at_ms"] > 0


@pytest.mark.asyncio
async def test_exchange_code_rejects_empty_code() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200))
    ) as client:
        with pytest.raises(xai_pkce.OAuthExchangeError, match="empty"):
            await xai_pkce.exchange_code(
                token_endpoint="https://auth.x.ai/oauth/token",
                code="  ",
                code_verifier="v",
                client=client,
            )


@pytest.mark.asyncio
async def test_exchange_code_non_2xx_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_grant"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(xai_pkce.OAuthExchangeError, match="HTTP 401"):
            await xai_pkce.exchange_code(
                token_endpoint="https://auth.x.ai/oauth/token",
                code="c",
                code_verifier="v",
                client=client,
            )


@pytest.mark.asyncio
async def test_exchange_code_missing_access_token_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"refresh_token": "r"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(xai_pkce.OAuthExchangeError, match="access_token"):
            await xai_pkce.exchange_code(
                token_endpoint="https://auth.x.ai/oauth/token",
                code="c",
                code_verifier="v",
                client=client,
            )


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_rotates_when_upstream_provides_new() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = dict(urllib.parse.parse_qsl(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "access_token": "rotated-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 3600,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        out = await xai_pkce.refresh_token(
            refresh_token="old-refresh",
            token_endpoint="https://auth.x.ai/oauth/token",
            client=client,
        )

    body = captured["body"]
    assert isinstance(body, dict)
    assert body["grant_type"] == "refresh_token"
    assert body["client_id"] == xai_pkce.XAI_OAUTH_CLIENT_ID
    assert body["refresh_token"] == "old-refresh"
    assert out["access_token"] == "rotated-access"
    assert out["refresh_token"] == "rotated-refresh"


@pytest.mark.asyncio
async def test_refresh_preserves_input_when_upstream_omits_new() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "fresh", "expires_in": 60})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        out = await xai_pkce.refresh_token(
            refresh_token="kept-refresh",
            token_endpoint="https://auth.x.ai/oauth/token",
            client=client,
        )
    assert out["refresh_token"] == "kept-refresh"


@pytest.mark.asyncio
async def test_refresh_requires_non_empty_input() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(xai_pkce.OAuthExchangeError, match="required"):
            await xai_pkce.refresh_token(
                refresh_token="",
                token_endpoint="https://auth.x.ai/oauth/token",
                client=client,
            )


@pytest.mark.asyncio
async def test_refresh_non_2xx_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(xai_pkce.OAuthExchangeError, match="HTTP 403"):
            await xai_pkce.refresh_token(
                refresh_token="r",
                token_endpoint="https://auth.x.ai/oauth/token",
                client=client,
            )


# ---------------------------------------------------------------------------
# Integration: expired stored credential triggers refresh + persist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_when_expired_triggers_refresh_path(tmp_path: Path) -> None:
    """End-to-end: store an expired xAI cred, refresh through the driver,
    persist the rotated bundle, reload and confirm it's no longer expired."""
    from corlinman_server.gateway.oauth.storage import (
        OAuthCredential,
        load_credential,
        save_credential,
    )

    cred = OAuthCredential.new(
        provider="xai",
        access_token="stale-access",
        refresh_token="stale-refresh",
        expires_at_ms=int(time.time() * 1000) - 60_000,  # 60s ago
    )
    save_credential(tmp_path, cred)
    loaded = load_credential(tmp_path, "xai")
    assert loaded is not None
    assert loaded.is_expired()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "fresh-access",
                "refresh_token": "fresh-refresh",
                "expires_in": 3600,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        refreshed = await xai_pkce.refresh_token(
            refresh_token=loaded.refresh_token or "",
            token_endpoint="https://auth.x.ai/oauth/token",
            client=client,
        )

    new_cred = loaded.with_refreshed(
        access_token=refreshed["access_token"],
        refresh_token=refreshed.get("refresh_token"),
        expires_at_ms=refreshed.get("expires_at_ms"),
    )
    save_credential(tmp_path, new_cred)
    reloaded = load_credential(tmp_path, "xai")
    assert reloaded is not None
    assert reloaded.access_token == "fresh-access"
    assert reloaded.is_expired() is False
