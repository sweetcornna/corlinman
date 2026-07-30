"""Streamable HTTP JSON-RPC peer — the transport modern remote MCP servers use.

Three transports now sit behind the one
:class:`~corlinman_mcp_server.client_manager.McpClientPeer` protocol:

* :mod:`corlinman_mcp_server.client` — stdio child process.
* :mod:`corlinman_mcp_server.client_ws` — WebSocket, the shape corlinman's
  own hosted server speaks (gateway-to-gateway).
* **this module** — MCP *Streamable HTTP* (2025-03-26, tightened in
  2025-06-18), which is what essentially every hosted third-party MCP server
  actually serves.

Why it had to exist
-------------------

``McpServerSpec`` has always accepted ``transport = "http"``, but
``_connect_peer`` rewrote the URL to ``ws://`` and dialled the WebSocket
client. Against a real Streamable HTTP server that fails at the handshake —
the endpoint answers HTTP, not a WebSocket upgrade — so the whole class of
remote MCP servers was unreachable while *looking* configurable.

The protocol, briefly
---------------------

One endpoint URL. Every client→server message is a POST whose body is a
single JSON-RPC frame and whose ``Accept`` names both content types:

* the server answers a **request** with either ``application/json`` (one
  response frame) or ``text/event-stream`` (an SSE stream that may carry
  server→client requests and notifications *before* the response we are
  waiting for — that interleaving is the whole point of the stream);
* the server answers a **notification** or a **response** with ``202`` and
  no body.

Two headers carry connection state: ``Mcp-Session-Id`` (minted by the server
on the ``initialize`` reply, echoed on every later request) and
``MCP-Protocol-Version`` (required from 2025-06-18 once a version has been
negotiated — :meth:`McpStreamableHttpClient.set_protocol_version` is how the
manager hands it over after the handshake).

Not implemented: the standalone ``GET`` SSE listener for unsolicited
server→client traffic. Everything corlinman uses a peer for (handshake,
discovery, ``tools/call``) is request-scoped, and each POST's own stream
already delivers the server→client requests belonging to that call.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import structlog

from .client import (
    McpClientDisconnected,
    McpClientServerError,
    McpClientSpawnError,
    McpClientWriteError,
    NotificationHandler,
    ServerRequestHandler,
)
from .types import JSONRPC_VERSION, JsonRpcRequest, JsonValue, classify_inbound, error_codes

log = structlog.get_logger(__name__)

__all__ = ["McpStreamableHttpClient"]

_JSON = "application/json"
_SSE = "text/event-stream"
_ACCEPT = f"{_JSON}, {_SSE}"

#: Cap on frames consumed from one POST's SSE stream while waiting for the
#: response. A server that streams notifications forever without ever
#: answering must not wedge the caller; the manager's ``call_timeout_s``
#: bounds wall-clock, this bounds work.
_MAX_STREAM_FRAMES: int = 4096


class McpStreamableHttpClient:
    """Outbound MCP client over Streamable HTTP.

    Exposes the same ``call`` / ``notify`` / ``close`` surface as the stdio
    and WebSocket peers, plus :meth:`set_protocol_version` (HTTP is the only
    transport where the negotiated version is carried in a header).

    Construct via :meth:`connect`, which validates the URL and builds the
    pooled :class:`httpx.AsyncClient` but performs **no** network I/O — an
    unreachable endpoint surfaces on the first ``call``, matching how the
    manager already folds handshake failures into a per-server error.
    """

    def __init__(
        self,
        url: str,
        client: httpx.AsyncClient,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._url = url
        self._http = client
        self._extra_headers = dict(headers or {})
        self._session_id: str | None = None
        self._protocol_version: str | None = None
        self._next_id: int = 0
        self._closed: bool = False
        self.on_server_request: ServerRequestHandler | None = None
        self.on_notification: NotificationHandler | None = None

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    async def connect(
        cls,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        open_timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> McpStreamableHttpClient:
        """Build a peer for the Streamable HTTP endpoint at ``url``.

        ``client`` is a test seam — pass a pre-built
        :class:`httpx.AsyncClient` (e.g. one wired to
        :class:`httpx.MockTransport`) to drive the peer without a socket.
        """
        if not url.startswith(("http://", "https://")):
            raise McpClientSpawnError(
                f"streamable-http transport needs an http(s) url, got {url!r}"
            )
        if client is None:
            # No total timeout: an SSE stream is long-lived by design and the
            # manager already bounds each call. Connect/read are bounded so a
            # black-holed endpoint still fails instead of hanging.
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(None, connect=open_timeout, read=open_timeout),
                follow_redirects=True,
            )
        return cls(url, client, headers=headers)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_protocol_version(self, version: str) -> None:
        """Record the negotiated revision for the ``MCP-Protocol-Version``
        header. Called by the manager once ``initialize`` has been answered
        — the handshake POST itself legitimately carries no such header."""
        self._protocol_version = version

    @property
    def session_id(self) -> str | None:
        """The server-minted session id, once it has issued one."""
        return self._session_id

    def _generate_id(self) -> JsonValue:
        n = self._next_id
        self._next_id += 1
        return f"req-{n}"

    def _headers(self) -> dict[str, str]:
        out = {
            "Accept": _ACCEPT,
            "Content-Type": _JSON,
            **self._extra_headers,
        }
        if self._session_id:
            out["Mcp-Session-Id"] = self._session_id
        if self._protocol_version:
            out["MCP-Protocol-Version"] = self._protocol_version
        return out

    async def call(self, method: str, params: JsonValue = None) -> JsonValue:
        """Send a request and await the matching response."""
        if self._closed:
            raise McpClientDisconnected("client closed")
        request_id = self._generate_id()
        frame = _frame(request_id, method, params)
        response = await self._post(frame)
        try:
            return await self._await_response(response, request_id)
        finally:
            await response.aclose()

    async def notify(self, method: str, params: JsonValue = None) -> None:
        """Send a notification (no id, no response expected)."""
        if self._closed:
            raise McpClientDisconnected("client closed")
        response = await self._post(_frame(None, method, params))
        # 202 + empty body is the specified answer; anything streamed back
        # here is unsolicited and belongs to no pending call, so drain and
        # drop it rather than blocking the caller on it.
        await response.aclose()

    async def close(self) -> None:
        """Release the session and the pooled connections.

        The ``DELETE`` is advisory — a server is free to answer 405 (session
        termination not supported) and many do, so its failure is logged at
        debug and never propagated.
        """
        if self._closed:
            return
        self._closed = True
        if self._session_id:
            try:
                await self._http.delete(self._url, headers=self._headers())
            except Exception as err:  # noqa: BLE001 — teardown is best-effort
                log.debug("mcp http: session delete failed", err=str(err))
        try:
            await self._http.aclose()
        except Exception as err:  # noqa: BLE001
            log.debug("mcp http: client close failed", err=str(err))

    async def __aenter__(self) -> McpStreamableHttpClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Wire plumbing
    # ------------------------------------------------------------------

    async def _post(self, frame: dict[str, Any]) -> httpx.Response:
        """POST one JSON-RPC frame and return the *unread* response.

        The response is returned streaming so an SSE body can be consumed
        incrementally; every caller is responsible for ``aclose()``.
        """
        try:
            body = json.dumps(frame, ensure_ascii=False)
        except (TypeError, ValueError) as err:
            raise McpClientWriteError(f"serialize request: {err}") from err
        request = self._http.build_request(
            "POST", self._url, headers=self._headers(), content=body.encode("utf-8")
        )
        try:
            response = await self._http.send(request, stream=True)
        except httpx.HTTPError as err:
            raise McpClientDisconnected(f"http post failed: {err}") from err

        # The session id is minted on the initialize reply and echoed from
        # then on; capture it wherever it shows up.
        session = response.headers.get("mcp-session-id")
        if session:
            self._session_id = session

        if response.status_code >= 400:
            detail = await _read_error_body(response)
            await response.aclose()
            # A 404 after we hold a session id means the server dropped it;
            # surface that as a disconnect so the manager can mark the peer
            # unhealthy rather than reporting a generic tool failure.
            if response.status_code == 404 and self._session_id:
                self._session_id = None
                raise McpClientDisconnected("session expired (404)")
            raise McpClientServerError(
                code=error_codes.INTERNAL_ERROR,
                message=f"http {response.status_code}: {detail}",
            )
        return response

    async def _await_response(
        self, response: httpx.Response, request_id: JsonValue
    ) -> JsonValue:
        """Pull frames off ``response`` until the reply to ``request_id``."""
        content_type = response.headers.get("content-type", "").split(";")[0].strip()

        if content_type == _JSON:
            raw = await response.aread()
            frames = _parse_json_body(raw)
        elif content_type == _SSE:
            return await self._consume_sse(response, request_id)
        elif response.status_code == 202:
            raise McpClientDisconnected(
                "server accepted the request without answering it"
            )
        else:
            raw = await response.aread()
            frames = _parse_json_body(raw)

        for frame in frames:
            done, value = await self._handle_frame(frame, request_id)
            if done:
                return value
        raise McpClientDisconnected("response body carried no matching reply")

    async def _consume_sse(
        self, response: httpx.Response, request_id: JsonValue
    ) -> JsonValue:
        seen = 0
        async for frame in _iter_sse_frames(response):
            seen += 1
            if seen > _MAX_STREAM_FRAMES:
                raise McpClientDisconnected(
                    f"event stream exceeded {_MAX_STREAM_FRAMES} frames "
                    "without answering"
                )
            done, value = await self._handle_frame(frame, request_id)
            if done:
                return value
        raise McpClientDisconnected("event stream ended before reply")

    async def _handle_frame(
        self, frame: dict[str, Any], request_id: JsonValue
    ) -> tuple[bool, JsonValue]:
        """Route one inbound frame.

        Returns ``(True, result)`` when the frame is the response we are
        waiting for; ``(False, None)`` for anything else (server→client
        requests and notifications are dispatched as a side effect). A
        response for a *different* id is dropped: without the standalone GET
        stream there is no other in-flight request it could belong to.
        """
        kind = classify_inbound(frame)
        if kind == "request":
            await self._dispatch_server_request(frame)
            return False, None
        if kind == "notification":
            await self._dispatch_notification(frame)
            return False, None
        if frame.get("id") != request_id:
            log.debug("mcp http: dropped unmatched response", id=frame.get("id"))
            return False, None
        if "result" in frame:
            return True, frame["result"]
        err = frame.get("error") or {}
        raise McpClientServerError(
            code=int(err.get("code", error_codes.INTERNAL_ERROR)),
            message=str(err.get("message", "")),
            data=err.get("data"),
        )

    async def _dispatch_server_request(self, frame: dict[str, Any]) -> None:
        """Answer a server→client request by POSTing the reply back."""
        rid = frame.get("id")
        method = str(frame.get("method") or "")
        params = frame.get("params")
        if not isinstance(params, dict):
            params = {}
        handler = self.on_server_request
        if handler is None:
            await self._post_reply(
                {
                    "jsonrpc": JSONRPC_VERSION,
                    "id": rid,
                    "error": {
                        "code": error_codes.METHOD_NOT_FOUND,
                        "message": f"method not supported by client: {method}",
                    },
                }
            )
            return
        try:
            result, error = await handler(method, params)
        except Exception as err:  # noqa: BLE001 — a handler never wedges the stream
            log.warning("mcp http: server-request handler error", method=method, err=str(err))
            result, error = None, {
                "code": error_codes.INTERNAL_ERROR,
                "message": f"handler error: {err}",
            }
        reply: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": rid}
        if error is not None:
            reply["error"] = error
        else:
            reply["result"] = result
        await self._post_reply(reply)

    async def _post_reply(self, reply: dict[str, Any]) -> None:
        try:
            response = await self._post(reply)
        except Exception as err:  # noqa: BLE001 — replying is best-effort
            log.warning("mcp http: reply post failed", err=str(err))
            return
        await response.aclose()

    async def _dispatch_notification(self, frame: dict[str, Any]) -> None:
        handler = self.on_notification
        if handler is None:
            return
        method = str(frame.get("method") or "")
        params = frame.get("params")
        if not isinstance(params, dict):
            params = {}
        try:
            await handler(method, params)
        except Exception as err:  # noqa: BLE001 — notifications never break a call
            log.warning("mcp http: notification handler error", method=method, err=str(err))


# ---------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------


def _frame(
    request_id: JsonValue, method: str, params: JsonValue
) -> dict[str, Any]:
    """Build a JSON-RPC frame; ``request_id=None`` makes it a notification
    (``JsonRpcRequest`` elides the absent id on dump)."""
    return JsonRpcRequest(
        jsonrpc=JSONRPC_VERSION, id=request_id, method=method, params=params
    ).model_dump()


def _parse_json_body(raw: bytes) -> list[dict[str, Any]]:
    """Parse an ``application/json`` body into a list of JSON-RPC frames.

    A lone object is the common case; a JSON array is accepted because
    2025-03-26 permitted batched replies (2025-06-18 removed batching, but a
    server still speaking the older revision is a peer we negotiated with).
    """
    if not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as err:
        raise McpClientDisconnected(f"malformed json body: {err}") from err
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        return [f for f in parsed if isinstance(f, dict)]
    raise McpClientDisconnected("json body was neither an object nor an array")


async def _iter_sse_frames(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Yield JSON-RPC frames carried by an SSE body.

    Minimal, deliberately: MCP only uses ``data:`` (multi-line per the SSE
    grammar, joined with newlines) and terminates an event on a blank line.
    ``event:`` / ``id:`` / ``retry:`` and comments are consumed and ignored —
    resumability via ``Last-Event-ID`` is a separate feature this client does
    not claim.
    """
    data_lines: list[str] = []
    try:
        async for line in response.aiter_lines():
            line = line.rstrip("\r")
            if not line:
                if data_lines:
                    payload = "\n".join(data_lines)
                    data_lines = []
                    try:
                        parsed = json.loads(payload)
                    except json.JSONDecodeError as err:
                        log.warning("mcp http: malformed sse payload", err=str(err))
                        continue
                    if isinstance(parsed, dict):
                        yield parsed
                    elif isinstance(parsed, list):
                        for item in parsed:
                            if isinstance(item, dict):
                                yield item
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            if field == "data":
                data_lines.append(value[1:] if value.startswith(" ") else value)
    except (httpx.HTTPError, asyncio.CancelledError) as err:
        if isinstance(err, asyncio.CancelledError):
            raise
        raise McpClientDisconnected(f"event stream failed: {err}") from err


async def _read_error_body(response: httpx.Response, limit: int = 512) -> str:
    """Best-effort excerpt of an error body for the exception message."""
    try:
        raw = await response.aread()
    except Exception:  # noqa: BLE001
        return "<unreadable body>"
    text = raw.decode("utf-8", errors="replace").strip()
    return text[:limit] if text else "<empty body>"
