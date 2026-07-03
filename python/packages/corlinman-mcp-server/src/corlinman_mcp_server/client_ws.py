"""WebSocket JSON-RPC peer — the "HTTP" transport for the MCP client.

The stdio peer (:mod:`corlinman_mcp_server.client`) covers MCP servers
launched as a child process. This module covers the other reference
transport: an MCP server reachable over a URL. MCP's HTTP-flavoured
transport in this codebase is a WebSocket carrying newline-free JSON-RPC
frames — the exact mirror of the server :class:`corlinman_mcp_server.\
transport.McpServer` so a corlinman gateway can act as an MCP *client*
of another corlinman gateway (or any websocket-JSON-RPC MCP server).

:class:`McpWebSocketClient` exposes the same ``call`` / ``notify`` /
``close`` surface as :class:`~corlinman_mcp_server.client.McpClient`, so
the :class:`~corlinman_mcp_server.client_manager.McpClientManager` can
treat both transports through one structural protocol.

``websockets`` is a hard dependency of this package (it powers the
server side too), so importing it here adds no new requirement.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import structlog

from .client import (
    _DISCONNECTED_MARKER,
    McpClientDisconnected,
    McpClientServerError,
    McpClientSpawnError,
    McpClientWriteError,
    NotificationHandler,
    ServerRequestHandler,
    _id_key,
)
from .types import (
    JSONRPC_VERSION,
    JsonRpcRequest,
    JsonValue,
    classify_inbound,
    error_codes,
)

log = structlog.get_logger(__name__)

__all__ = ["McpWebSocketClient"]


class McpWebSocketClient:
    """Outbound MCP client over a WebSocket JSON-RPC connection.

    Construct via :meth:`connect` (the only entry point). One reader
    task pulls inbound frames off the socket and resolves the matching
    parked :class:`asyncio.Future`; :meth:`call` writes directly to the
    socket (websockets serialises writes internally).

    The surface — :meth:`call`, :meth:`notify`, :meth:`close` and async
    context-manager support — is intentionally identical to
    :class:`~corlinman_mcp_server.client.McpClient` so both transports
    are interchangeable behind the client manager.
    """

    def __init__(self, connection: Any) -> None:
        self._conn = connection
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._next_id: int = 0
        self._closed: bool = False
        # Server->client handlers (set by the manager after connect); see
        # McpClient for the contract. Unset → METHOD_NOT_FOUND reply /
        # dropped notification.
        self.on_server_request: ServerRequestHandler | None = None
        self.on_notification: NotificationHandler | None = None
        self._inbound_tasks: set[asyncio.Task[Any]] = set()
        self._reader_task: asyncio.Task = asyncio.create_task(self._reader_loop())

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
    ) -> McpWebSocketClient:
        """Dial ``url`` and connect.

        ``url`` is a ``ws://`` / ``wss://`` endpoint. Raises
        :class:`~corlinman_mcp_server.client.McpClientSpawnError` if the
        connection cannot be established (the manager folds this into a
        degraded-server marker rather than crashing).
        """
        try:
            from websockets.asyncio.client import connect as ws_connect
        except Exception as exc:  # pragma: no cover — websockets is a hard dep
            raise McpClientSpawnError(
                f"websockets client transport unavailable: {exc}"
            ) from exc

        kwargs: dict[str, Any] = {"open_timeout": open_timeout}
        if headers:
            kwargs["additional_headers"] = headers
        try:
            connection = await ws_connect(url, **kwargs)
        except Exception as exc:
            raise McpClientSpawnError(
                f"failed to connect to MCP websocket {url!r}: {exc}"
            ) from exc
        return cls(connection)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _generate_id(self) -> JsonValue:
        n = self._next_id
        self._next_id += 1
        return f"req-{n}"

    async def call(self, method: str, params: JsonValue = None) -> JsonValue:
        """Send a request and await the matching response.

        Returns the reply's ``result`` on success; a JSON-RPC error
        frame raises :class:`~corlinman_mcp_server.client.\
McpClientServerError`.
        """
        id_value = self._generate_id()
        key = _id_key(id_value)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[key] = fut

        req = JsonRpcRequest(
            jsonrpc=JSONRPC_VERSION,
            id=id_value,
            method=method,
            params=params,
        )
        try:
            frame = json.dumps(req.model_dump(), ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            self._pending.pop(key, None)
            raise McpClientWriteError(f"serialize request: {exc}") from exc

        try:
            await self._conn.send(frame)
        except Exception as exc:
            self._pending.pop(key, None)
            raise McpClientDisconnected(f"websocket send failed: {exc}") from exc

        try:
            resp = await fut
        except asyncio.CancelledError:
            raise McpClientDisconnected("future cancelled") from None

        if "result" in resp:
            return resp["result"]
        err = resp.get("error", {})
        message = err.get("message", "")
        if isinstance(message, str) and message.startswith(_DISCONNECTED_MARKER):
            raise McpClientDisconnected(message[len(_DISCONNECTED_MARKER):])
        raise McpClientServerError(
            code=int(err.get("code", error_codes.INTERNAL_ERROR)),
            message=str(message),
            data=err.get("data"),
        )

    async def notify(self, method: str, params: JsonValue = None) -> None:
        """Send a notification (no id, no response expected)."""
        req = JsonRpcRequest(
            jsonrpc=JSONRPC_VERSION,
            id=None,
            method=method,
            params=params,
        )
        try:
            frame = json.dumps(req.model_dump(), ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise McpClientWriteError(f"serialize notification: {exc}") from exc
        try:
            await self._conn.send(frame)
        except Exception as exc:
            raise McpClientDisconnected(f"websocket send failed: {exc}") from exc

    async def close(self) -> None:
        """Close the socket and stop the reader task."""
        if self._closed:
            return
        self._closed = True
        for task in list(self._inbound_tasks):
            task.cancel()
        with contextlib.suppress(Exception):
            await self._conn.close()
        self._reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._reader_task
        self._fail_pending("client closed")

    # ------------------------------------------------------------------
    # Server -> client inbound handling (shared shape with McpClient)
    # ------------------------------------------------------------------

    def _spawn_inbound(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._inbound_tasks.add(task)
        task.add_done_callback(self._inbound_tasks.discard)

    async def _send_frame(self, frame: dict[str, Any]) -> None:
        try:
            await self._conn.send(json.dumps(frame, ensure_ascii=False))
        except Exception as err:  # noqa: BLE001 — reply is best-effort
            log.warning("mcp ws client: reply send failed", err=str(err))

    async def _dispatch_server_request(self, frame: dict[str, Any]) -> None:
        rid = frame.get("id")
        method = str(frame.get("method") or "")
        params = frame.get("params")
        if not isinstance(params, dict):
            params = {}
        handler = self.on_server_request
        if handler is None:
            await self._send_frame(
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
        except Exception as err:  # noqa: BLE001
            log.warning("mcp ws client: server-request handler error", method=method, err=str(err))
            result, error = None, {
                "code": error_codes.INTERNAL_ERROR,
                "message": f"handler error: {err}",
            }
        reply: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": rid}
        if error is not None:
            reply["error"] = error
        else:
            reply["result"] = result
        await self._send_frame(reply)

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
        except Exception as err:  # noqa: BLE001
            log.warning("mcp ws client: notification handler error", method=method, err=str(err))

    async def __aenter__(self) -> McpWebSocketClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------

    async def _reader_loop(self) -> None:
        try:
            async for message in self._conn:
                if isinstance(message, bytes):
                    try:
                        message = message.decode("utf-8")
                    except UnicodeDecodeError as err:
                        log.warning("mcp ws client: decode error", err=str(err))
                        continue
                try:
                    parsed = json.loads(message)
                except json.JSONDecodeError as err:
                    log.warning("mcp ws client: parse failed", err=str(err))
                    continue
                if not isinstance(parsed, dict):
                    continue
                kind = classify_inbound(parsed)
                if kind == "request":
                    self._spawn_inbound(self._dispatch_server_request(parsed))
                    continue
                if kind == "notification":
                    self._spawn_inbound(self._dispatch_notification(parsed))
                    continue
                key = _id_key(parsed.get("id"))
                fut = self._pending.pop(key, None)
                if fut is not None and not fut.done():
                    fut.set_result(parsed)
                else:
                    log.debug("mcp ws client: dropped unmatched response", id=key)
        except asyncio.CancelledError:
            return
        except Exception as err:  # connection closed / read error
            log.debug("mcp ws client: reader loop ended", err=str(err))
        finally:
            self._fail_pending("websocket closed")

    def _fail_pending(self, reason: str) -> None:
        """Resolve every still-parked waiter with a disconnected marker."""
        for key, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_result(
                    {
                        "jsonrpc": JSONRPC_VERSION,
                        "id": None,
                        "error": {
                            "code": error_codes.INTERNAL_ERROR,
                            "message": f"{_DISCONNECTED_MARKER}{reason}",
                        },
                    }
                )
            self._pending.pop(key, None)
