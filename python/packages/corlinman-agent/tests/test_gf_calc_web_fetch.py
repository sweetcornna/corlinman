"""gap-fill lane-calc-web: web_fetch prompt extraction + paging + wrap.

Covers gap ``web-fetch-prompt-extraction``:

* the no-prompt path keeps the historical envelope keys (shape stable);
* the body is fenced via the untrusted-content wrapper before it enters
  the envelope;
* an optional ``prompt`` converts the page to Markdown (stdlib fallback
  ok — no optional lib installed in CI);
* ``offset`` pages through a long document, surfacing ``next_offset``.

Network is mocked with :class:`httpx.MockTransport` (no real I/O, the
same precedent as ``test_web_tools.py``).
"""

from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest
from corlinman_agent.web.external_content import _LABEL
from corlinman_agent.web.fetch import dispatch_web_fetch, web_fetch_tool_schema

_PUBLIC_TEST_IP = "93.184.216.34"
_BEGIN = f"{_LABEL}_BEGIN"
_END = f"{_LABEL}_END"


@pytest.fixture(autouse=True)
def _fake_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    real = socket.getaddrinfo

    def _fake(host: str, *args, **kw):  # type: ignore[no-untyped-def]
        if host and host.endswith(".example"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_TEST_IP, 0))]
        return real(host, *args, **kw)

    from corlinman_agent.web import _common as wc

    monkeypatch.setattr(wc.socket, "getaddrinfo", _fake)


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def _html(body: str) -> str:
    return f"<html><head><title>Doc</title></head><body>{body}</body></html>"


def test_schema_advertises_prompt_and_offset() -> None:
    params = web_fetch_tool_schema()["function"]["parameters"]["properties"]
    assert "prompt" in params
    assert "offset" in params


def test_no_prompt_envelope_shape_stable_and_wrapped() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=_html("<p>First & paragraph.</p><p>Second paragraph.</p>"),
        )

    out = json.loads(
        _run(
            dispatch_web_fetch(
                args_json=json.dumps({"url": "http://p.example"}),
                transport=httpx.MockTransport(handler),
            )
        )
    )
    # All historical keys present (shape stable).
    assert {
        "url",
        "final_url",
        "status",
        "title",
        "content_type",
        "text",
        "truncated",
        "bytes",
    } <= set(out.keys())
    assert out["status"] == 200
    assert out["title"] == "Doc"
    # ``text`` is the RAW fetched body (not delimiter-wrapped) — that is the
    # tool's stable public contract; injection framing is applied at the
    # model-render layer, not baked into this field.
    assert _BEGIN not in out["text"] and _END not in out["text"]
    assert "First & paragraph." in out["text"]
    assert "Second paragraph." in out["text"]
    # Short page: no paging needed.
    assert out.get("next_offset") is None
    assert out["truncated"] is False


def test_prompt_returns_markdown_and_pages() -> None:
    long_body = "<h1>Heading</h1>" + "".join(
        f"<p>Paragraph number {i} lorem ipsum dolor sit amet.</p>" for i in range(400)
    )

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text=_html(long_body)
        )

    transport = httpx.MockTransport(handler)
    out1 = json.loads(
        _run(
            dispatch_web_fetch(
                args_json=json.dumps(
                    {"url": "http://p.example", "prompt": "find heading", "max_chars": 600}
                ),
                transport=transport,
            )
        )
    )
    # Long doc -> paging kicked in.
    assert "next_offset" in out1 and out1["next_offset"] > 0
    assert out1["truncated"] is True
    # Result respects the byte budget (raw body, capped to max_chars).
    assert len(out1["text"]) <= 600

    # Page 2 via the returned offset reads further content.
    out2 = json.loads(
        _run(
            dispatch_web_fetch(
                args_json=json.dumps(
                    {
                        "url": "http://p.example",
                        "prompt": "x",
                        "max_chars": 600,
                        "offset": out1["next_offset"],
                    }
                ),
                transport=transport,
            )
        )
    )
    assert out2["status"] == 200
    assert out2["truncated"] is True
    assert out2["text"] != out1["text"]


def test_offset_past_end_returns_no_next_offset() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text=_html("<p>tiny</p>")
        )

    out = json.loads(
        _run(
            dispatch_web_fetch(
                args_json=json.dumps({"url": "http://p.example", "offset": 99999}),
                transport=httpx.MockTransport(handler),
            )
        )
    )
    assert out.get("next_offset") is None


def test_bad_offset_and_prompt_types_rejected() -> None:
    out = json.loads(
        _run(
            dispatch_web_fetch(
                args_json=json.dumps({"url": "http://p.example", "offset": -1}),
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(200, text="x")
                ),
            )
        )
    )
    assert out["error"].startswith("args_invalid:")

    out2 = json.loads(
        _run(
            dispatch_web_fetch(
                args_json=json.dumps({"url": "http://p.example", "prompt": 5}),
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(200, text="x")
                ),
            )
        )
    )
    assert out2["error"].startswith("args_invalid:")
