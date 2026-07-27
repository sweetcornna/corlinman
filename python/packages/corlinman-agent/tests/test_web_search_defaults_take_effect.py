"""Operator config must reach the ``web_search`` tool, not just the route.

``web_search`` runs in the agent process, whose systemd unit carries no
``EnvironmentFile`` — it sets exactly ``HOME`` /
``CORLINMAN_EXECUTION_STATE_DIR`` / ``CORLINMAN_PY_CONFIG`` /
``CORLINMAN_PY_SOCKET``. So the ``CORLINMAN_WEB_SEARCH_*`` env layer is
unreachable in a native deployment and every install silently fell back to
the keyless DuckDuckGo scrape.

These tests drive the **tool dispatcher** (not the admin route) and pin
that what the operator sets in the UI is what the outbound search request
actually uses, and that it outranks a stale export on the host.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator

import httpx
import pytest
from corlinman_agent.web.defaults import (
    apply_web_search_config,
    get_web_search_defaults,
    reset_web_search_defaults,
    web_search_defaults_from_config,
)
from corlinman_agent.web.search import dispatch_web_search

_SERPAPI_PAYLOAD = {
    "organic_results": [
        {
            "title": "Result one",
            "link": "https://example.com/one",
            "snippet": "first",
        }
    ]
}

_DDG_HTML = (
    '<a class="result__a" href="https://example.org/hit">DDG hit</a>'
    '<a class="result__snippet">from duckduckgo</a>'
)


#: Mirrors ``test_web_tools._PUBLIC_TEST_IP`` — any address the SSRF guard
#: accepts as public.
_PUBLIC_TEST_IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def _hermetic_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the search backends to a public test IP.

    ``dispatch_web_search`` validates + pins the backend host before
    connecting (SEC-012), which is a real ``getaddrinfo`` call. Without
    this the suite depends on the developer's resolver — a VPN that maps
    ``serpapi.com`` into 198.18/15 makes every test fail on
    ``unsafe_backend`` instead of exercising the config path.
    """
    real = socket.getaddrinfo
    # The backends themselves, plus the hosts the canned results point at —
    # result URLs are SSRF-filtered too, so they must look public or the
    # envelope comes back empty for the wrong reason.
    public = {"html.duckduckgo.com", "serpapi.com", "example.com", "example.org"}

    def _fake(host: str, *args, **kw):  # type: ignore[no-untyped-def]
        if host in public:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_TEST_IP, 0))]
        return real(host, *args, **kw)

    from corlinman_agent.web import _common as wc

    monkeypatch.setattr(wc.socket, "getaddrinfo", _fake)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The env layer is what these tests measure *against*; start from a
    # known-empty state so a developer's own export cannot mask a failure.
    monkeypatch.delenv("CORLINMAN_WEB_SEARCH_BACKEND", raising=False)
    monkeypatch.delenv("CORLINMAN_WEB_SEARCH_API_KEY", raising=False)
    reset_web_search_defaults()
    yield
    reset_web_search_defaults()


def _capture(payload: object, *, json_body: bool = True):
    """MockTransport that records the outbound request it served."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        # The SSRF pin rewrites the URL host to the resolved IP and moves
        # the real hostname into the Host header, so that is what we assert.
        seen["host"] = request.headers.get("host", "")
        seen["params"] = dict(request.url.params)
        if json_body:
            return httpx.Response(200, json=payload)
        return httpx.Response(200, text=str(payload))

    return seen, httpx.MockTransport(handler)


async def _search(transport) -> dict:
    return json.loads(
        await dispatch_web_search(
            args_json=json.dumps({"query": "corlinman"}).encode(),
            transport=transport,
        )
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_config_block_parses_into_defaults() -> None:
    d = web_search_defaults_from_config({"backend": "SerpApi", "api_key": " k "})
    assert d.backend == "serpapi"  # normalised
    assert d.api_key == "k"


def test_malformed_block_is_unconfigured() -> None:
    assert web_search_defaults_from_config(None) == web_search_defaults_from_config("nope")
    assert web_search_defaults_from_config({"backend": 7}).backend == ""


def test_as_dict_never_leaks_the_key() -> None:
    d = web_search_defaults_from_config({"api_key": "super-secret"})
    assert "super-secret" not in json.dumps(d.as_dict())
    assert d.as_dict()["api_key_set"] is True


def test_apply_is_idempotent() -> None:
    apply_web_search_config({"backend": "serpapi", "api_key": "k"})
    apply_web_search_config({"backend": "serpapi", "api_key": "k"})
    assert get_web_search_defaults().backend == "serpapi"
    apply_web_search_config(None)
    assert get_web_search_defaults().backend == ""


# ---------------------------------------------------------------------------
# The dispatcher actually honours it
# ---------------------------------------------------------------------------


async def test_unconfigured_falls_back_to_keyless_ddg() -> None:
    seen, transport = _capture(_DDG_HTML, json_body=False)
    out = await _search(transport)
    assert out["backend"] == "ddg"
    assert seen["host"] == "html.duckduckgo.com"


async def test_configured_backend_and_key_reach_the_request() -> None:
    apply_web_search_config({"backend": "serpapi", "api_key": "cfg-key"})
    seen, transport = _capture(_SERPAPI_PAYLOAD)
    out = await _search(transport)
    assert out["backend"] == "serpapi"
    assert seen["host"] == "serpapi.com"
    # The operator's key is what authenticates the outbound call.
    assert seen["params"]["api_key"] == "cfg-key"


async def test_key_alone_selects_serpapi() -> None:
    """No explicit backend, but a key on file → the key-based backend."""
    apply_web_search_config({"api_key": "cfg-key"})
    seen, transport = _capture(_SERPAPI_PAYLOAD)
    out = await _search(transport)
    assert out["backend"] == "serpapi"
    assert seen["params"]["api_key"] == "cfg-key"


async def test_config_outranks_a_stale_env_export(monkeypatch: pytest.MonkeyPatch) -> None:
    """A leftover host export must not override a UI choice."""
    monkeypatch.setenv("CORLINMAN_WEB_SEARCH_BACKEND", "ddg")
    monkeypatch.setenv("CORLINMAN_WEB_SEARCH_API_KEY", "stale-env-key")
    apply_web_search_config({"backend": "serpapi", "api_key": "cfg-key"})

    seen, transport = _capture(_SERPAPI_PAYLOAD)
    out = await _search(transport)
    assert out["backend"] == "serpapi"
    assert seen["params"]["api_key"] == "cfg-key"


async def test_env_still_works_when_config_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The env layer stays functional for in-process / dev deployments."""
    monkeypatch.setenv("CORLINMAN_WEB_SEARCH_API_KEY", "env-key")
    seen, transport = _capture(_SERPAPI_PAYLOAD)
    out = await _search(transport)
    assert out["backend"] == "serpapi"
    assert seen["params"]["api_key"] == "env-key"


async def test_serpapi_without_any_key_degrades_cleanly() -> None:
    """Selecting serpapi with no key must not break the reasoning loop."""
    apply_web_search_config({"backend": "serpapi"})
    _seen, transport = _capture(_SERPAPI_PAYLOAD)
    out = await _search(transport)
    assert out["results"] == []
    assert "api key" in out["error"].lower()
