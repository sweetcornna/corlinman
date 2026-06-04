"""Tests for the server-side NapCat WebUI credential seam.

The admin UI embeds NapCat's WebUI, which authenticates client-side and
intermittently fails on a stale browser credential ("获取QQ列表失败:
Unauthorized"). The gateway now mints the credential server-side so the
reverse proxy can inject it on every NapCat ``/api/*`` request via
``auth_request`` -> ``GET /internal/napcat-credential``. These tests pin the
caching + fail-soft contract of :func:`_cached_napcat_credential`.
"""

from __future__ import annotations

import pytest

# Patch the module where ``_cached_napcat_credential`` actually does its name
# lookups: the helper + client + cred-cache were extracted into ``_napcat_lib``
# (``napcat.py`` re-exports them for the route handlers). monkeypatching the
# route module would miss the lib-internal ``config_snapshot`` / ``_NapcatClient``
# references, so target the lib directly.
from corlinman_server.gateway.routes_admin_b import _napcat_lib as nc

_QQ_CFG = {
    "channels": {
        "qq": {"napcat_url": "http://nap:6099", "napcat_access_token": "tok"}
    }
}


@pytest.fixture(autouse=True)
def _reset_cred_cache():
    nc._NAPCAT_CRED_CACHE["value"] = ""
    nc._NAPCAT_CRED_CACHE["exp"] = 0.0
    yield
    nc._NAPCAT_CRED_CACHE["value"] = ""
    nc._NAPCAT_CRED_CACHE["exp"] = 0.0


@pytest.mark.asyncio
async def test_empty_when_no_access_token(monkeypatch) -> None:
    # No qq config -> _resolve_napcat_url yields a url but no token -> "".
    monkeypatch.setattr(nc, "config_snapshot", lambda: {})
    monkeypatch.delenv("NAPCAT_WEBUI_TOKEN", raising=False)
    monkeypatch.delenv("NAPCAT_WEBUI_SECRET_KEY", raising=False)
    assert await nc._cached_napcat_credential() == ""


def test_resolve_napcat_url_uses_webui_token_env(monkeypatch) -> None:
    monkeypatch.setenv("NAPCAT_WEBUI_TOKEN", "webui-token")
    url, token = nc._resolve_napcat_url({"channels": {"qq": {}}})
    assert url == nc.DEFAULT_NAPCAT_URL
    assert token == "webui-token"


@pytest.mark.asyncio
async def test_exchanged_and_cached(monkeypatch) -> None:
    monkeypatch.setattr(nc, "config_snapshot", lambda: _QQ_CFG)
    calls = {"n": 0}

    async def fake_get_credential(self) -> str:
        calls["n"] += 1
        return "CRED-XYZ"

    monkeypatch.setattr(nc._NapcatClient, "get_credential", fake_get_credential)

    first = await nc._cached_napcat_credential()
    second = await nc._cached_napcat_credential()
    assert first == "CRED-XYZ"
    assert second == "CRED-XYZ"
    assert calls["n"] == 1  # second call served from cache, no re-exchange


@pytest.mark.asyncio
async def test_refreshes_after_ttl(monkeypatch) -> None:
    monkeypatch.setattr(nc, "config_snapshot", lambda: _QQ_CFG)
    seq = iter(["A", "B"])

    async def fake_get_credential(self) -> str:
        return next(seq)

    monkeypatch.setattr(nc._NapcatClient, "get_credential", fake_get_credential)

    first = await nc._cached_napcat_credential()
    nc._NAPCAT_CRED_CACHE["exp"] = 0.0  # force expiry
    second = await nc._cached_napcat_credential()
    assert (first, second) == ("A", "B")


@pytest.mark.asyncio
async def test_exchange_error_is_swallowed(monkeypatch) -> None:
    # A NapCat outage must degrade to "" (proxy falls back to client auth),
    # never raise into the auth_request path.
    monkeypatch.setattr(nc, "config_snapshot", lambda: _QQ_CFG)

    async def boom(self) -> str:
        raise RuntimeError("napcat unreachable")

    monkeypatch.setattr(nc._NapcatClient, "get_credential", boom)
    assert await nc._cached_napcat_credential() == ""
