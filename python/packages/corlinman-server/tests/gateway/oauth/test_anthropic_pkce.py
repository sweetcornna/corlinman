"""Tests for ``corlinman_server.gateway.oauth.anthropic_pkce``.

Coverage:

* PKCE pair generation produces a verifier in the 43-128 char window
  and a challenge that is SHA256(verifier) base64url no-pad — verified
  by recomputing the digest in the test.
* The authorize URL contains every required PKCE param + the literal
  Anthropic client id + the S256 challenge method.
* ``exchange_code`` POSTs to the token endpoint with the right body,
  returns the parsed shape, and surfaces a clean error on non-2xx /
  network failure.
* ``refresh_token`` rotates the refresh token when the upstream emits
  a new one and falls back to the input when it doesn't.
* Empty / malformed responses raise ``OAuthExchangeError`` rather than
  silently returning garbage.
"""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.parse

import httpx
import pytest
from corlinman_server.gateway.oauth import anthropic_pkce


def test_generate_pkce_pair_shape() -> None:
    verifier, challenge = anthropic_pkce.generate_pkce_pair()
    # RFC 7636: 43-128 chars
    assert 43 <= len(verifier) <= 128
    # URL-safe base64 alphabet, no padding
    assert all(c.isalnum() or c in "-_" for c in verifier)
    assert "=" not in verifier
    # Challenge is SHA256(verifier) base64url no-pad
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert challenge == expected


def test_generate_pkce_pair_changes_each_call() -> None:
    pairs = {anthropic_pkce.generate_pkce_pair()[0] for _ in range(8)}
    assert len(pairs) == 8


def test_build_authorize_url_contains_all_params() -> None:
    url = anthropic_pkce.build_authorize_url(
        code_challenge="challenge-xyz",
        state="state-abc",
    )
    assert url.startswith(anthropic_pkce.ANTHROPIC_OAUTH_AUTHORIZE_URL + "?")
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert qs["client_id"] == [anthropic_pkce.ANTHROPIC_OAUTH_CLIENT_ID]
    assert qs["code_challenge"] == ["challenge-xyz"]
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["state"] == ["state-abc"]
    assert qs["redirect_uri"] == [anthropic_pkce.ANTHROPIC_OAUTH_REDIRECT_URI]
    assert qs["response_type"] == ["code"]
    assert qs["scope"] == [anthropic_pkce.ANTHROPIC_OAUTH_SCOPES]
    assert qs["code"] == ["true"]


@pytest.mark.asyncio
async def test_exchange_code_success() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
                "scope": "user:inference",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        out = await anthropic_pkce.exchange_code(
            code_input="abcdef#state-xyz",
            code_verifier="verifier-123",
            # R4-D1: the callback state must MATCH the minted state, so a
            # successful exchange carries equal values. (Anthropic mints
            # state == verifier and the callback echoes it back.)
            expected_state="state-xyz",
            client=client,
        )

    assert captured["url"] == anthropic_pkce.ANTHROPIC_OAUTH_TOKEN_URL
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "abcdef"
    # Validated callback state is what gets forwarded to the token endpoint.
    assert body["state"] == "state-xyz"
    assert body["code_verifier"] == "verifier-123"
    assert body["client_id"] == anthropic_pkce.ANTHROPIC_OAUTH_CLIENT_ID
    assert body["redirect_uri"] == anthropic_pkce.ANTHROPIC_OAUTH_REDIRECT_URI

    assert out["access_token"] == "new-access"
    assert out["refresh_token"] == "new-refresh"
    assert out["scope"] == "user:inference"
    assert isinstance(out["expires_at_ms"], int)
    assert out["expires_at_ms"] > 0


@pytest.mark.asyncio
async def test_exchange_code_falls_back_to_expected_state_when_callback_omits_it() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"access_token": "a", "refresh_token": "r", "expires_in": 60})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await anthropic_pkce.exchange_code(
            code_input="just-the-code",
            code_verifier="v",
            expected_state="fallback-state",
            client=client,
        )
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["state"] == "fallback-state"


@pytest.mark.asyncio
async def test_exchange_code_rejects_empty_code() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200))) as client:
        with pytest.raises(anthropic_pkce.OAuthExchangeError, match="empty"):
            await anthropic_pkce.exchange_code(
                code_input="   ",
                code_verifier="v",
                expected_state="s",
                client=client,
            )


@pytest.mark.asyncio
async def test_exchange_code_non_2xx_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_grant"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(anthropic_pkce.OAuthExchangeError, match="HTTP 401"):
            await anthropic_pkce.exchange_code(
                code_input="abc",
                code_verifier="v",
                expected_state="s",
                client=client,
            )


@pytest.mark.asyncio
async def test_exchange_code_missing_access_token_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"refresh_token": "r"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(anthropic_pkce.OAuthExchangeError, match="access_token"):
            await anthropic_pkce.exchange_code(
                code_input="abc",
                code_verifier="v",
                expected_state="s",
                client=client,
            )


@pytest.mark.asyncio
async def test_refresh_token_rotates_when_upstream_provides_new() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
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
        out = await anthropic_pkce.refresh_token(
            refresh_token="old-refresh", client=client
        )
    assert out["access_token"] == "rotated-access"
    assert out["refresh_token"] == "rotated-refresh"


@pytest.mark.asyncio
async def test_refresh_token_preserves_input_when_upstream_omits_new() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "a", "expires_in": 60})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        out = await anthropic_pkce.refresh_token(
            refresh_token="kept-refresh", client=client
        )
    assert out["refresh_token"] == "kept-refresh"


@pytest.mark.asyncio
async def test_refresh_token_falls_back_through_endpoints() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        # First endpoint 500s; second 200s.
        if len(seen) == 1:
            return httpx.Response(500, json={"error": "down"})
        return httpx.Response(200, json={"access_token": "ok", "expires_in": 60})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        out = await anthropic_pkce.refresh_token(
            refresh_token="r", client=client
        )
    assert out["access_token"] == "ok"
    assert len(seen) == 2
    assert seen[0] != seen[1]


@pytest.mark.asyncio
async def test_refresh_token_requires_non_empty_input() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(anthropic_pkce.OAuthExchangeError, match="required"):
            await anthropic_pkce.refresh_token(refresh_token="", client=client)


@pytest.mark.asyncio
async def test_refresh_when_expired_triggers_refresh_path() -> None:
    """Integration-style: a stored credential that is expired triggers
    a refresh and saves the rotated tokens."""
    import time
    from pathlib import Path

    from corlinman_server.gateway.oauth.storage import (
        OAuthCredential,
        load_credential,
        save_credential,
    )

    tmp = Path(__import__("tempfile").mkdtemp())
    try:
        # Save an expired credential.
        cred = OAuthCredential.new(
            provider="anthropic",
            access_token="stale-access",
            refresh_token="stale-refresh",
            expires_at_ms=int(time.time() * 1000) - 60_000,  # expired 60s ago
        )
        save_credential(tmp, cred)
        loaded = load_credential(tmp, "anthropic")
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
            refreshed = await anthropic_pkce.refresh_token(
                refresh_token=loaded.refresh_token or "", client=client
            )
        # Persist the rotated bundle the way the router would.
        new_cred = loaded.with_refreshed(
            access_token=refreshed["access_token"],
            refresh_token=refreshed.get("refresh_token"),
            expires_at_ms=refreshed.get("expires_at_ms"),
        )
        save_credential(tmp, new_cred)
        reloaded = load_credential(tmp, "anthropic")
        assert reloaded is not None
        assert reloaded.access_token == "fresh-access"
        assert reloaded.is_expired() is False
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
