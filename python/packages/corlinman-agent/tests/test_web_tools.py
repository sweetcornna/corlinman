"""Tests for the builtin web tools (``web_fetch`` / ``web_search``) and
the self-contained ``calculator``.

Network is mocked with :class:`httpx.MockTransport` — no real I/O, no
new test dependency (``respx`` is not in the dependency set; the rerank
client tests set the same precedent).
"""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Callable

import httpx
import pytest
from corlinman_agent.web import (
    CALCULATOR_TOOL,
    WEB_FETCH_TOOL,
    WEB_SEARCH_TOOL,
    calculator_tool_schema,
    dispatch_calculator,
    dispatch_web_fetch,
    dispatch_web_search,
    web_fetch_tool_schema,
    web_search_tool_schema,
)
from corlinman_agent.web.fetch import DEFAULT_MAX_CHARS, MAX_BODY_BYTES

#: IANA-reserved example IPv4 (RFC 5737). Treated as public by
#: ``ipaddress``; safe to use as the resolved address for synthetic
#: test hostnames so the SSRF guard lets the (mocked) request through.
_PUBLIC_TEST_IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def _fake_dns_for_test_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make synthetic test hostnames (``*.example.com`` / ``*.example``
    / ``slow.example.com`` / ``safe.example.com``) resolve to a known
    public IP so the SSRF guard does not refuse them on DNS failure.

    Tests that deliberately exercise unsafe resolution
    (e.g. ``evil.test`` mapped to 10.0.0.5) install their own
    ``getaddrinfo`` stub that delegates to this baseline for everything
    else.
    """
    real = socket.getaddrinfo

    def _fake(host: str, *args, **kw):  # type: ignore[no-untyped-def]
        if host and (
            host.endswith(".example.com")
            or host.endswith(".example")
            or host == "example.com"
        ):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_TEST_IP, 0))]
        return real(host, *args, **kw)

    from corlinman_agent.web import _common as wc

    monkeypatch.setattr(wc.socket, "getaddrinfo", _fake)


def _transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Schemas / wire-stable names
# ---------------------------------------------------------------------------


def test_tool_names_are_wire_stable() -> None:
    assert WEB_FETCH_TOOL == "web_fetch"
    assert WEB_SEARCH_TOOL == "web_search"
    assert CALCULATOR_TOOL == "calculator"


@pytest.mark.parametrize(
    ("schema_fn", "name"),
    [
        (web_fetch_tool_schema, "web_fetch"),
        (web_search_tool_schema, "web_search"),
        (calculator_tool_schema, "calculator"),
    ],
)
def test_schemas_are_openai_shaped(schema_fn, name) -> None:  # type: ignore[no-untyped-def]
    schema = schema_fn()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == name
    assert "parameters" in schema["function"]
    assert schema["function"]["parameters"]["type"] == "object"


# ---------------------------------------------------------------------------
# web_fetch
# ---------------------------------------------------------------------------


def test_web_fetch_success_strips_html() -> None:
    html_body = (
        "<html><head><title>Hello Page</title>"
        "<style>.x{color:red}</style></head>"
        "<body><script>var a=1;</script>"
        "<h1>Heading</h1><p>First &amp; paragraph.</p>"
        "<p>Second paragraph.</p></body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, html=html_body, headers={"content-type": "text/html"}
        )

    out = json.loads(
        asyncio.run(
            dispatch_web_fetch(
                args_json=json.dumps({"url": "https://example.com/doc"}),
                transport=_transport(handler),
            )
        )
    )
    assert out["status"] == 200
    assert out["title"] == "Hello Page"
    assert "First & paragraph." in out["text"]
    assert "Second paragraph." in out["text"]
    # script / style content must be gone.
    assert "var a=1" not in out["text"]
    assert "color:red" not in out["text"]
    assert out["truncated"] is False


def test_web_fetch_plain_text_passthrough() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="just plain text",
            headers={"content-type": "text/plain"},
        )

    out = json.loads(
        asyncio.run(
            dispatch_web_fetch(
                args_json=json.dumps({"url": "https://example.com/raw.txt"}),
                transport=_transport(handler),
            )
        )
    )
    assert out["text"] == "just plain text"
    assert out["title"] is None


def test_web_fetch_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out", request=request)

    out = json.loads(
        asyncio.run(
            dispatch_web_fetch(
                args_json=json.dumps({"url": "https://slow.example.com"}),
                transport=_transport(handler),
            )
        )
    )
    assert "error" in out
    assert out["error"].startswith("timeout:")


def test_web_fetch_non_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="<html>not found</html>")

    out = json.loads(
        asyncio.run(
            dispatch_web_fetch(
                args_json=json.dumps({"url": "https://example.com/missing"}),
                transport=_transport(handler),
            )
        )
    )
    assert out["status"] == 404
    assert out["error"].startswith("http_status:")


def test_web_fetch_oversized_body_is_truncated() -> None:
    # Body larger than MAX_BODY_BYTES — must be flagged truncated and
    # never blow memory (bounded mid-stream).
    big = "x" * (MAX_BODY_BYTES + 5_000)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=big, headers={"content-type": "text/plain"}
        )

    out = json.loads(
        asyncio.run(
            dispatch_web_fetch(
                args_json=json.dumps({"url": "https://example.com/big"}),
                transport=_transport(handler),
            )
        )
    )
    assert out["truncated"] is True
    assert len(out["text"]) <= DEFAULT_MAX_CHARS


def test_web_fetch_respects_max_chars() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="abcdefghij" * 50, headers={"content-type": "text/plain"}
        )

    out = json.loads(
        asyncio.run(
            dispatch_web_fetch(
                args_json=json.dumps(
                    {"url": "https://example.com/x", "max_chars": 25}
                ),
                transport=_transport(handler),
            )
        )
    )
    assert len(out["text"]) == 25
    assert out["truncated"] is True


def test_web_fetch_rejects_bad_url() -> None:
    out = json.loads(
        asyncio.run(dispatch_web_fetch(args_json=json.dumps({"url": "ftp://x"})))
    )
    assert out["error"].startswith("args_invalid:")


def test_web_fetch_rejects_missing_url() -> None:
    out = json.loads(asyncio.run(dispatch_web_fetch(args_json=b"{}")))
    assert out["error"].startswith("args_invalid:")


def test_web_fetch_rejects_bad_json() -> None:
    out = json.loads(asyncio.run(dispatch_web_fetch(args_json=b"not json")))
    assert out["error"].startswith("args_invalid:")


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------

_DDG_HTML = """
<html><body>
<div class="result">
  <a class="result__a" href="https://a.example.com/page">First &amp; Result</a>
  <a class="result__snippet">Snippet about the first result.</a>
</div>
<div class="result">
  <a class="result__a"
     href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fb.example.com%2Fx&amp;rut=z">
     Second Result</a>
  <a class="result__snippet">Snippet <b>two</b> here.</a>
</div>
</body></html>
"""


def test_web_search_parses_ddg_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORLINMAN_WEB_SEARCH_BACKEND", raising=False)
    monkeypatch.delenv("CORLINMAN_WEB_SEARCH_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "duckduckgo.com" in str(request.url)
        return httpx.Response(200, text=_DDG_HTML)

    out = json.loads(
        asyncio.run(
            dispatch_web_search(
                args_json=json.dumps({"query": "corlinman agent"}),
                transport=_transport(handler),
            )
        )
    )
    assert out["backend"] == "ddg"
    assert len(out["results"]) == 2
    first = out["results"][0]
    assert first["title"] == "First & Result"
    assert first["url"] == "https://a.example.com/page"
    assert "first result" in first["snippet"].lower()
    # redirect-wrapped URL must be unwrapped to the real target.
    assert out["results"][1]["url"] == "https://b.example.com/x"


def test_web_search_respects_max_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CORLINMAN_WEB_SEARCH_BACKEND", raising=False)
    monkeypatch.delenv("CORLINMAN_WEB_SEARCH_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_DDG_HTML)

    out = json.loads(
        asyncio.run(
            dispatch_web_search(
                args_json=json.dumps({"query": "x", "max_results": 1}),
                transport=_transport(handler),
            )
        )
    )
    assert len(out["results"]) == 1


def test_web_search_degrades_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CORLINMAN_WEB_SEARCH_BACKEND", raising=False)
    monkeypatch.delenv("CORLINMAN_WEB_SEARCH_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="service unavailable")

    out = json.loads(
        asyncio.run(
            dispatch_web_search(
                args_json=json.dumps({"query": "x"}),
                transport=_transport(handler),
            )
        )
    )
    assert out["results"] == []
    assert out["error"].startswith("search_unavailable:")


def test_web_search_degrades_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CORLINMAN_WEB_SEARCH_BACKEND", raising=False)
    monkeypatch.delenv("CORLINMAN_WEB_SEARCH_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    out = json.loads(
        asyncio.run(
            dispatch_web_search(
                args_json=json.dumps({"query": "x"}),
                transport=_transport(handler),
            )
        )
    )
    assert out["results"] == []
    assert out["error"].startswith("timeout:")


def test_web_search_rejects_missing_query() -> None:
    out = json.loads(asyncio.run(dispatch_web_search(args_json=b"{}")))
    assert out["results"] == []
    assert out["error"].startswith("args_invalid:")


def test_web_search_serpapi_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORLINMAN_WEB_SEARCH_API_KEY", "secret-key")
    monkeypatch.delenv("CORLINMAN_WEB_SEARCH_BACKEND", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "serpapi.com" in str(request.url)
        assert "secret-key" in str(request.url)
        return httpx.Response(
            200,
            json={
                "organic_results": [
                    {
                        "title": "SerpApi Hit",
                        "link": "https://c.example.com",
                        "snippet": "from serpapi",
                    }
                ]
            },
        )

    out = json.loads(
        asyncio.run(
            dispatch_web_search(
                args_json=json.dumps({"query": "x"}),
                transport=_transport(handler),
            )
        )
    )
    assert out["backend"] == "serpapi"
    assert out["results"][0]["url"] == "https://c.example.com"


def test_web_search_unknown_backend_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORLINMAN_WEB_SEARCH_BACKEND", "bing-nope")

    out = json.loads(
        asyncio.run(dispatch_web_search(args_json=json.dumps({"query": "x"})))
    )
    assert out["results"] == []
    assert out["error"].startswith("unknown_backend:")


# ---------------------------------------------------------------------------
# calculator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("2 + 2", 4),
        ("2 + 3 * 4", 14),
        ("(1234 * 5678) / 2", 3503326.0),
        ("2 ** 10", 1024),
        ("17 % 5", 2),
        ("17 // 5", 3),
        ("-(3 + 4)", -7),
    ],
)
def test_calculator_evaluates(expr: str, expected: float) -> None:
    out = json.loads(dispatch_calculator(args_json=json.dumps({"expression": expr})))
    assert out["result"] == expected


def test_calculator_division_by_zero() -> None:
    out = json.loads(
        dispatch_calculator(args_json=json.dumps({"expression": "1 / 0"}))
    )
    assert out["error"] == "division by zero"


def test_calculator_rejects_code_injection() -> None:
    for evil in ["__import__('os')", "open('x')", "x + 1", "[i for i in range(3)]"]:
        out = json.loads(
            dispatch_calculator(args_json=json.dumps({"expression": evil}))
        )
        assert "error" in out
        assert "result" not in out


def test_calculator_rejects_huge_exponent() -> None:
    out = json.loads(
        dispatch_calculator(args_json=json.dumps({"expression": "9 ** 999999"}))
    )
    assert out["error"].startswith("invalid_expression:")


def test_calculator_rejects_missing_expression() -> None:
    out = json.loads(dispatch_calculator(args_json=b"{}"))
    assert out["error"].startswith("args_invalid:")


def test_calculator_rejects_bad_json() -> None:
    out = json.loads(dispatch_calculator(args_json=b"<<<"))
    assert out["error"].startswith("args_invalid:")


# ---------------------------------------------------------------------------
# S1 — SSRF guard on web_fetch
# ---------------------------------------------------------------------------


def _expect_unsafe(out: dict, *, key: str = "error") -> None:
    """Helper: every SSRF refusal returns ``{"error": "unsafe_host: ..."}``
    or ``{"error": "unsafe_redirect: ..."}`` — no body is fetched."""
    assert key in out, f"expected refusal envelope, got: {out!r}"
    assert (
        out[key].startswith("unsafe_host:") or out[key].startswith("unsafe_redirect:")
    ), f"unexpected refusal shape: {out!r}"


def test_web_fetch_rejects_loopback_v4() -> None:
    out = json.loads(
        asyncio.run(
            dispatch_web_fetch(args_json=json.dumps({"url": "http://127.0.0.1/admin"}))
        )
    )
    _expect_unsafe(out)


def test_web_fetch_rejects_localhost_via_dns() -> None:
    """Hostname ``localhost`` resolves to 127.0.0.1 via DNS — the guard
    must classify the resolved address, not the literal hostname."""
    out = json.loads(
        asyncio.run(
            dispatch_web_fetch(args_json=json.dumps({"url": "http://localhost:8080"}))
        )
    )
    _expect_unsafe(out)


def test_web_fetch_rejects_private_rfc1918_literal() -> None:
    out = json.loads(
        asyncio.run(
            dispatch_web_fetch(args_json=json.dumps({"url": "http://10.0.0.1/x"}))
        )
    )
    _expect_unsafe(out)


def test_web_fetch_rejects_cloud_metadata_endpoint() -> None:
    out = json.loads(
        asyncio.run(
            dispatch_web_fetch(
                args_json=json.dumps({"url": "http://169.254.169.254/latest/meta-data"})
            )
        )
    )
    _expect_unsafe(out)
    # Metadata error message names the endpoint explicitly.
    assert "metadata" in out["error"].lower() or "169.254" in out["error"]


def test_web_fetch_metadata_blocked_even_with_allow_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dev override turns off RFC1918/loopback checks but MUST NOT
    open up the cloud metadata endpoint."""
    monkeypatch.setenv("CORLINMAN_WEB_FETCH_ALLOW_PRIVATE", "1")
    out = json.loads(
        asyncio.run(
            dispatch_web_fetch(
                args_json=json.dumps({"url": "http://169.254.169.254/latest/meta-data"})
            )
        )
    )
    _expect_unsafe(out)


def test_web_fetch_rejects_hostname_that_resolves_to_private_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DNS-pinning: ``evil.test`` resolves to 10.0.0.5, the guard must
    classify EACH resolved address and refuse even one internal hit."""
    from corlinman_agent.web import _common as wc

    def fake_getaddrinfo(host: str, *args, **kw):  # type: ignore[no-untyped-def]
        if host == "evil.test":
            return [(socket.AF_INET, 1, 0, "", ("10.0.0.5", 0))]
        return socket.getaddrinfo(host, *args, **kw)

    monkeypatch.setattr(wc.socket, "getaddrinfo", fake_getaddrinfo)
    out = json.loads(
        asyncio.run(
            dispatch_web_fetch(args_json=json.dumps({"url": "http://evil.test/"}))
        )
    )
    _expect_unsafe(out)


def test_web_fetch_rejects_redirect_to_internal_host() -> None:
    """A public site that 302s to an internal address must be refused at
    the redirect stage; the guard re-validates every hop."""
    visited: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        visited.append(str(request.url))
        if "public" in str(request.url):
            return httpx.Response(
                302, headers={"location": "http://127.0.0.1/secret"}
            )
        return httpx.Response(200, text="LEAKED")

    out = json.loads(
        asyncio.run(
            dispatch_web_fetch(
                args_json=json.dumps({"url": "https://public.example.com/start"}),
                transport=_transport(handler),
            )
        )
    )
    assert out["error"].startswith("unsafe_redirect:"), out
    # We only ever dialed the first (public) URL. The 127.0.0.1 leg was
    # blocked before any socket was opened.
    assert len(visited) == 1
    assert "public" in visited[0]


def test_web_fetch_follows_safe_redirect_chain() -> None:
    """Capped redirect loop still works for normal public redirects."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = httpx.URL(str(request.url)).path
        if path == "/one":
            return httpx.Response(
                302, headers={"location": "https://example.com/final"}
            )
        return httpx.Response(
            200, text="ok-body", headers={"content-type": "text/plain"}
        )

    out = json.loads(
        asyncio.run(
            dispatch_web_fetch(
                args_json=json.dumps({"url": "https://example.com/one"}),
                transport=_transport(handler),
            )
        )
    )
    assert out["status"] == 200
    assert out["text"] == "ok-body"


def test_web_fetch_refuses_too_many_redirects() -> None:
    """An infinite redirect loop is bounded by ``MAX_REDIRECTS``."""
    def handler(request: httpx.Request) -> httpx.Response:
        # Always 302 back to the same public host; we should give up
        # after MAX_REDIRECTS hops, not loop forever.
        return httpx.Response(302, headers={"location": "https://example.com/x"})

    out = json.loads(
        asyncio.run(
            dispatch_web_fetch(
                args_json=json.dumps({"url": "https://example.com/x"}),
                transport=_transport(handler),
            )
        )
    )
    assert out["error"].startswith("too_many_redirects:")


def test_web_fetch_allow_private_override_admits_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dev override lets developers hit a local fixture explicitly."""
    monkeypatch.setenv("CORLINMAN_WEB_FETCH_ALLOW_PRIVATE", "1")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="local-only", headers={"content-type": "text/plain"}
        )

    out = json.loads(
        asyncio.run(
            dispatch_web_fetch(
                args_json=json.dumps({"url": "http://127.0.0.1:8080/dev"}),
                transport=_transport(handler),
            )
        )
    )
    assert out["status"] == 200
    assert out["text"] == "local-only"


def test_web_search_drops_unsafe_result_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search results pointing at internal hosts are filtered out before
    surfacing to the model — the model can never even see them."""
    # Inject the keyless DDG backend so the filter codepath runs.
    monkeypatch.setenv("CORLINMAN_WEB_SEARCH_BACKEND", "ddg")

    html_with_mixed_urls = """
<html><body>
<div class="result">
  <a class="result__a" href="https://safe.example.com/page">Public Hit</a>
  <a class="result__snippet">A public, fetchable page.</a>
</div>
<div class="result">
  <a class="result__a" href="http://10.0.0.7/internal">Internal Hit</a>
  <a class="result__snippet">Should be filtered out.</a>
</div>
<div class="result">
  <a class="result__a" href="http://169.254.169.254/latest">Metadata Hit</a>
  <a class="result__snippet">Also filtered.</a>
</div>
</body></html>
"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html_with_mixed_urls)

    out = json.loads(
        asyncio.run(
            dispatch_web_search(
                args_json=json.dumps({"query": "anything"}),
                transport=_transport(handler),
            )
        )
    )
    urls = [r["url"] for r in out["results"]]
    assert "https://safe.example.com/page" in urls
    assert all("10.0.0.7" not in u and "169.254" not in u for u in urls), urls
