"""Streamable HTTP client transport.

``McpServerSpec`` accepted ``transport = "http"`` long before anything
implemented it: ``_connect_peer`` rewrote the URL to ``ws://`` and dialled
the websocket client, so every real Streamable HTTP server — which is to say
essentially every hosted third-party MCP server — failed at the handshake.

Driven through :class:`httpx.MockTransport`, so these exercise the real
request/response plumbing (headers, content types, SSE framing) with no
socket.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from corlinman_mcp_server.client import (
    McpClientDisconnected,
    McpClientServerError,
    McpClientSpawnError,
)
from corlinman_mcp_server.client_http import McpStreamableHttpClient

_URL = "https://example.test/mcp"


def _json_response(payload: dict[str, Any], **kwargs: Any) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json", **kwargs.pop("headers", {})},
        content=json.dumps(payload).encode("utf-8"),
        **kwargs,
    )


def _sse_response(frames: list[dict[str, Any]], **kwargs: Any) -> httpx.Response:
    body = "".join(
        f"event: message\ndata: {json.dumps(f)}\n\n" for f in frames
    )
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream", **kwargs.pop("headers", {})},
        content=body.encode("utf-8"),
        **kwargs,
    )


async def _peer(
    handler,
    *,
    headers: dict[str, str] | None = None,
) -> McpStreamableHttpClient:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return await McpStreamableHttpClient.connect(
        _URL, headers=headers, client=client
    )


# ─── construction ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_rejects_a_non_http_url() -> None:
    with pytest.raises(McpClientSpawnError, match="http"):
        await McpStreamableHttpClient.connect("ws://example.test/mcp")


# ─── application/json replies ────────────────────────────────────────


@pytest.mark.asyncio
async def test_call_over_json_returns_the_result() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        frame = json.loads(request.content)
        seen.append(frame)
        return _json_response(
            {"jsonrpc": "2.0", "id": frame["id"], "result": {"tools": []}}
        )

    peer = await _peer(handler)
    try:
        assert await peer.call("tools/list", {}) == {"tools": []}
        assert seen[0]["method"] == "tools/list"
        assert "id" in seen[0]
    finally:
        await peer.close()


@pytest.mark.asyncio
async def test_json_rpc_error_lifts_to_server_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        frame = json.loads(request.content)
        return _json_response(
            {
                "jsonrpc": "2.0",
                "id": frame["id"],
                "error": {"code": -32601, "message": "nope"},
            }
        )

    peer = await _peer(handler)
    try:
        with pytest.raises(McpClientServerError) as excinfo:
            await peer.call("tools/call", {})
        assert excinfo.value.code == -32601
    finally:
        await peer.close()


@pytest.mark.asyncio
async def test_notify_sends_an_id_less_frame() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(202)

    peer = await _peer(handler)
    try:
        await peer.notify("notifications/initialized", {})
        assert seen[0]["method"] == "notifications/initialized"
        assert "id" not in seen[0]
    finally:
        await peer.close()


# ─── text/event-stream replies ───────────────────────────────────────


@pytest.mark.asyncio
async def test_call_over_sse_reads_past_interleaved_traffic() -> None:
    """The reply may arrive behind notifications on the same stream —
    that interleaving is the entire reason the transport streams."""
    notes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        frame = json.loads(request.content)
        return _sse_response(
            [
                {"jsonrpc": "2.0", "method": "notifications/progress",
                 "params": {"pct": 10}},
                {"jsonrpc": "2.0", "method": "notifications/progress",
                 "params": {"pct": 90}},
                {"jsonrpc": "2.0", "id": frame["id"], "result": {"ok": True}},
            ]
        )

    peer = await _peer(handler)

    async def on_note(method: str, params: dict[str, Any]) -> None:
        notes.append(method)

    peer.on_notification = on_note
    try:
        assert await peer.call("tools/call", {}) == {"ok": True}
        assert notes == ["notifications/progress", "notifications/progress"]
    finally:
        await peer.close()


@pytest.mark.asyncio
async def test_server_request_on_the_stream_is_answered_by_post() -> None:
    """A server→client request (sampling, elicitation) arrives mid-stream;
    the client answers it as a *new* POST, not on the stream."""
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        frame = json.loads(request.content)
        posted.append(frame)
        if frame.get("method") == "tools/call":
            return _sse_response(
                [
                    {"jsonrpc": "2.0", "id": "srv-1",
                     "method": "sampling/createMessage", "params": {}},
                    {"jsonrpc": "2.0", "id": frame["id"], "result": {"done": 1}},
                ]
            )
        return httpx.Response(202)

    peer = await _peer(handler)

    async def on_request(
        method: str, params: dict[str, Any]
    ) -> tuple[Any, dict[str, Any] | None]:
        return {"role": "assistant", "content": method}, None

    peer.on_server_request = on_request
    try:
        assert await peer.call("tools/call", {}) == {"done": 1}
        replies = [f for f in posted if f.get("id") == "srv-1"]
        assert replies and replies[0]["result"]["content"] == "sampling/createMessage"
    finally:
        await peer.close()


@pytest.mark.asyncio
async def test_unhandled_server_request_gets_method_not_found() -> None:
    """With no handler wired, a compliant server must still be answered —
    otherwise it waits forever on a reply that never comes."""
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        frame = json.loads(request.content)
        posted.append(frame)
        if frame.get("method") == "tools/call":
            return _sse_response(
                [
                    {"jsonrpc": "2.0", "id": "srv-1", "method": "elicitation/create",
                     "params": {}},
                    {"jsonrpc": "2.0", "id": frame["id"], "result": {}},
                ]
            )
        return httpx.Response(202)

    peer = await _peer(handler)
    try:
        await peer.call("tools/call", {})
        replies = [f for f in posted if f.get("id") == "srv-1"]
        assert replies and replies[0]["error"]["code"] == -32601
    finally:
        await peer.close()


@pytest.mark.asyncio
async def test_stream_ending_without_a_reply_is_a_disconnect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [{"jsonrpc": "2.0", "method": "notifications/message", "params": {}}]
        )

    peer = await _peer(handler)
    try:
        with pytest.raises(McpClientDisconnected, match="ended before reply"):
            await peer.call("tools/list", {})
    finally:
        await peer.close()


# ─── session + protocol headers ──────────────────────────────────────


@pytest.mark.asyncio
async def test_session_id_is_captured_and_echoed() -> None:
    """The server mints ``Mcp-Session-Id`` on the initialize reply; every
    later request must carry it or the server treats us as a stranger."""
    headers_seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers_seen.append(request.headers)
        frame = json.loads(request.content)
        extra = (
            {"mcp-session-id": "sess-42"}
            if frame.get("method") == "initialize"
            else {}
        )
        return _json_response(
            {"jsonrpc": "2.0", "id": frame["id"], "result": {}}, headers=extra
        )

    peer = await _peer(handler)
    try:
        await peer.call("initialize", {})
        assert peer.session_id == "sess-42"
        assert "mcp-session-id" not in headers_seen[0]
        await peer.call("tools/list", {})
        assert headers_seen[1]["mcp-session-id"] == "sess-42"
    finally:
        await peer.close()


@pytest.mark.asyncio
async def test_protocol_version_header_is_sent_once_negotiated() -> None:
    """2025-06-18 requires ``MCP-Protocol-Version`` on every request after
    the handshake — and legitimately *not* on the handshake itself."""
    headers_seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers_seen.append(request.headers)
        frame = json.loads(request.content)
        return _json_response({"jsonrpc": "2.0", "id": frame["id"], "result": {}})

    peer = await _peer(handler)
    try:
        await peer.call("initialize", {})
        assert "mcp-protocol-version" not in headers_seen[0]
        peer.set_protocol_version("2025-11-25")
        await peer.call("tools/list", {})
        assert headers_seen[1]["mcp-protocol-version"] == "2025-11-25"
    finally:
        await peer.close()


@pytest.mark.asyncio
async def test_accept_header_names_both_content_types() -> None:
    headers_seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers_seen.append(request.headers)
        frame = json.loads(request.content)
        return _json_response({"jsonrpc": "2.0", "id": frame["id"], "result": {}})

    peer = await _peer(handler, headers={"Authorization": "Bearer t"})
    try:
        await peer.call("tools/list", {})
        accept = headers_seen[0]["accept"]
        assert "application/json" in accept
        assert "text/event-stream" in accept
        assert headers_seen[0]["authorization"] == "Bearer t"
    finally:
        await peer.close()


# ─── HTTP-level failures ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_error_status_lifts_to_server_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"kaboom")

    peer = await _peer(handler)
    try:
        with pytest.raises(McpClientServerError, match="500"):
            await peer.call("tools/list", {})
    finally:
        await peer.close()


@pytest.mark.asyncio
async def test_expired_session_reads_as_a_disconnect() -> None:
    """A 404 once we hold a session id means the server dropped it — the
    manager should see a dead peer, not a generic tool failure."""
    state = {"issued": False}

    def handler(request: httpx.Request) -> httpx.Response:
        frame = json.loads(request.content)
        if not state["issued"]:
            state["issued"] = True
            return _json_response(
                {"jsonrpc": "2.0", "id": frame["id"], "result": {}},
                headers={"mcp-session-id": "sess-1"},
            )
        return httpx.Response(404, content=b"unknown session")

    peer = await _peer(handler)
    try:
        await peer.call("initialize", {})
        with pytest.raises(McpClientDisconnected, match="session expired"):
            await peer.call("tools/list", {})
    finally:
        await peer.close()


@pytest.mark.asyncio
async def test_call_after_close_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202)

    peer = await _peer(handler)
    await peer.close()
    with pytest.raises(McpClientDisconnected, match="closed"):
        await peer.call("tools/list", {})
