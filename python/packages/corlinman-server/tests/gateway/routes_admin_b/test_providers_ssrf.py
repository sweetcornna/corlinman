"""SEC-008 — SSRF guard for the ``/admin/providers/{name}/models`` probe.

``_query_provider_models`` dials the operator-supplied ``base_url`` with
the API key in an ``Authorization: Bearer`` header. A malicious /
mis-pasted base_url pointing at the cloud-metadata endpoint
(``169.254.169.254`` / ``metadata.google.internal``) would exfiltrate the
key (and could pull instance credentials) — classic SSRF.

The surgical guard rejects ONLY link-local + metadata targets:

* ``169.254.0.0/16`` (link-local, incl. the 169.254.169.254 metadata IP)
* IPv6 ``fe80::/10`` link-local
* the GCP/Azure metadata hostname ``metadata.google.internal``
* any non-http(s) scheme

Loopback (``127.0.0.0/8`` / ``::1``) and RFC1918 private ranges
(``10/8``, ``172.16/12``, ``192.168/16``) are INTENTIONALLY allowed so
self-hosted local LLM relays (Ollama / vLLM) keep working — admins
legitimately configure these and the host is operator-trusted.

All network is mocked: a rejected host MUST NOT produce any outbound
httpx call, an allowed host MUST attempt the dial.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from corlinman_server.gateway.routes_admin_b.providers import (
    _query_provider_models,
)


def _make_async_client_mock() -> tuple[MagicMock, AsyncMock]:
    """Return ``(AsyncClient_factory, get_mock)``.

    ``get_mock`` records whether an outbound GET was attempted.
    """
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"data": [{"id": "m"}]}

    get_mock = AsyncMock(return_value=resp)
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = get_mock

    factory = MagicMock(return_value=client)
    return factory, get_mock


def _cfg(base_url: str, *, kind: str = "openai_compatible") -> dict[str, Any]:
    return {
        "providers": {
            "p": {
                "kind": kind,
                "api_key": "sk-secret",
                "base_url": base_url,
                "enabled": True,
            }
        }
    }


# ---------------------------------------------------------------------------
# Rejected: metadata / link-local — no dial, unsafe_host error shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url",
    [
        "http://169.254.169.254/",
        "http://169.254.169.254",
        "http://169.254.0.1:80",
        "http://metadata.google.internal/",
        "https://metadata.google.internal",
        "http://[fe80::1]/",
    ],
)
async def test_metadata_and_link_local_rejected_without_dial(base_url: str) -> None:
    factory, get_mock = _make_async_client_mock()
    with patch("httpx.AsyncClient", factory):
        result = await _query_provider_models("p", _cfg(base_url))

    # Rejected before any outbound request.
    factory.assert_not_called()
    get_mock.assert_not_called()
    assert result["ok"] is False
    assert str(result.get("error") or "").startswith("unsafe_host:")
    # Key must not leak into the error.
    assert "sk-secret" not in str(result.get("error") or "")


@pytest.mark.asyncio
async def test_non_http_scheme_rejected_without_dial() -> None:
    factory, get_mock = _make_async_client_mock()
    with patch("httpx.AsyncClient", factory):
        result = await _query_provider_models("p", _cfg("file:///etc/passwd"))

    factory.assert_not_called()
    get_mock.assert_not_called()
    assert result["ok"] is False
    assert str(result.get("error") or "").startswith("unsafe_host:")


# ---------------------------------------------------------------------------
# Allowed: loopback / RFC1918 / public — dial IS attempted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:1234",
        "http://10.0.0.5",
        "http://192.168.1.10:8000",
        "http://172.16.0.9",
        "https://api.openai.com",
    ],
)
async def test_loopback_private_and_public_allowed(base_url: str) -> None:
    factory, get_mock = _make_async_client_mock()
    with patch("httpx.AsyncClient", factory):
        result = await _query_provider_models("p", _cfg(base_url))

    # Dial was attempted (guard let it through). The mocked response is a
    # success, so the probe should report ok.
    get_mock.assert_awaited_once()
    assert result["ok"] is True
