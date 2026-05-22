"""MCP *client* manager — corlinman as a consumer of external MCP servers.

P14. The rest of this package implements corlinman *hosting* an MCP
server (:class:`~corlinman_mcp_server.transport.McpServer`) plus a raw
stdio JSON-RPC peer (:class:`~corlinman_mcp_server.client.McpClient`).
This module is the missing wiring: it turns a list of MCP *server
specs* — read from the gateway config — into a live pool of connected
clients whose tools are discovered and exposed to the agent's tool
plane.

Lifecycle of one managed server
-------------------------------

1. **connect** — spawn (stdio) or dial (ws/http) the server, per its
   :class:`McpServerSpec.transport`.
2. **handshake** — send ``initialize`` then the
   ``notifications/initialized`` notification (MCP 2024-11-05).
3. **discover** — call ``tools/list`` and cache the
   :class:`ToolDescriptor`\\ s the server advertises.
4. **call** — :meth:`McpClientManager.call_tool` routes a
   ``(server, tool, args)`` triple through the connected peer's
   ``tools/call`` and returns a :class:`McpToolCallOutcome`.

Gate, never crash
-----------------

A server that fails to connect / handshake / list tools does **not**
abort :meth:`McpClientManager.connect_all`; it is recorded as a
:class:`McpManagedServer` with ``status="error"`` and a human-readable
``error`` string. The agent simply sees fewer tools. Likewise
:meth:`call_tool` always returns a structured :class:`McpToolCallOutcome`
— never raises — so the reasoning loop keeps making progress.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from .client import McpClient, McpClientError
from .client_ws import McpWebSocketClient
from .session import INITIALIZE_METHOD, INITIALIZED_NOTIFICATION
from .types import (
    MCP_PROTOCOL_VERSION,
    ToolDescriptor,
)

log = structlog.get_logger(__name__)

__all__ = [
    "McpClientManager",
    "McpClientPeer",
    "McpManagedServer",
    "McpServerSpec",
    "McpToolCallOutcome",
    "load_server_specs",
]

#: Default handshake / discovery deadline (seconds).
DEFAULT_HANDSHAKE_TIMEOUT_S: float = 10.0

#: Default per-call deadline for a ``tools/call`` (seconds).
DEFAULT_CALL_TIMEOUT_S: float = 30.0

#: How corlinman identifies itself in the ``initialize`` handshake.
_CLIENT_NAME = "corlinman"
_CLIENT_VERSION = "1.0.0"


class McpClientPeer(Protocol):
    """Structural contract shared by both client transports.

    :class:`~corlinman_mcp_server.client.McpClient` (stdio) and
    :class:`~corlinman_mcp_server.client_ws.McpWebSocketClient` (ws)
    both satisfy this, so the manager treats them uniformly.
    """

    async def call(self, method: str, params: Any = None) -> Any: ...

    async def notify(self, method: str, params: Any = None) -> None: ...

    async def close(self) -> None: ...


# ---------------------------------------------------------------------
# Config-shaped specs.
# ---------------------------------------------------------------------


@dataclass
class McpServerSpec:
    """One configured external MCP server.

    Mirrors the shape of a ``config["mcp"]["servers"]`` /
    ``config["mcp_servers"]`` entry. ``transport`` selects how to
    reach the server:

    * ``"stdio"`` — launch ``command`` + ``args`` as a child process
      (newline-delimited JSON-RPC over its stdio).
    * ``"ws"`` / ``"http"`` — dial ``url`` (a ``ws://`` / ``wss://``
      websocket carrying JSON-RPC frames).
    """

    name: str
    transport: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    handshake_timeout_s: float = DEFAULT_HANDSHAKE_TIMEOUT_S
    call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S

    @classmethod
    def from_mapping(cls, name: str, raw: Any) -> McpServerSpec:
        """Build a spec from a loosely-typed config mapping.

        Accepts both the ``command``/``args`` stdio form and the
        ``url`` ws/http form; ``transport`` is inferred when absent
        (``url`` present → ``ws``, else ``stdio``).
        """
        if not isinstance(raw, dict):
            raise ValueError(f"mcp server {name!r} config must be a mapping")
        url = str(raw.get("url", "") or "")
        transport = str(raw.get("transport", "") or "").lower()
        if not transport:
            transport = "ws" if url else "stdio"
        args_raw = raw.get("args", []) or []
        env_raw = raw.get("env", {}) or {}
        headers_raw = raw.get("headers", {}) or {}
        return cls(
            name=name,
            transport=transport,
            command=str(raw.get("command", "") or ""),
            args=[str(a) for a in args_raw],
            env={str(k): str(v) for k, v in dict(env_raw).items()},
            url=url,
            headers={str(k): str(v) for k, v in dict(headers_raw).items()},
            enabled=bool(raw.get("enabled", True)),
            handshake_timeout_s=float(
                raw.get("handshake_timeout_s", DEFAULT_HANDSHAKE_TIMEOUT_S)
            ),
            call_timeout_s=float(
                raw.get("call_timeout_s", DEFAULT_CALL_TIMEOUT_S)
            ),
        )


def load_server_specs(config: Any) -> list[McpServerSpec]:
    """Extract :class:`McpServerSpec`\\ s from a gateway config object.

    Recognises both layouts the reference projects use:

    * ``config["mcp"]["servers"]`` — a ``{name: {...}}`` mapping or a
      list of ``{"name": ..., ...}`` objects.
    * ``config["mcp_servers"]`` — same, hoisted to the top level.

    Returns an empty list when no MCP config is present — a clean
    no-op degradation, not an error.
    """
    if config is None:
        return []

    def _get(obj: Any, key: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    raw = _get(config, "mcp_servers")
    if raw is None:
        mcp_section = _get(config, "mcp")
        if mcp_section is not None:
            raw = _get(mcp_section, "servers")
    if raw is None:
        return []

    specs: list[McpServerSpec] = []
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, (list, tuple)):
        items = []
        for entry in raw:
            if isinstance(entry, dict) and "name" in entry:
                items.append((str(entry["name"]), entry))
    else:
        return []

    for name, body in items:
        try:
            specs.append(McpServerSpec.from_mapping(str(name), body))
        except Exception as exc:  # malformed entry — skip, don't abort
            log.warning(
                "mcp.client.spec_skipped", server=str(name), error=str(exc)
            )
    return specs


# ---------------------------------------------------------------------
# Outcomes.
# ---------------------------------------------------------------------


@dataclass
class McpManagedServer:
    """Connection state for one managed MCP server."""

    spec: McpServerSpec
    status: str = "pending"  # "ready" | "error"
    peer: McpClientPeer | None = None
    tools: list[ToolDescriptor] = field(default_factory=list)
    error: str | None = None

    @property
    def is_ready(self) -> bool:
        return self.status == "ready" and self.peer is not None


@dataclass
class McpToolCallOutcome:
    """Result of one :meth:`McpClientManager.call_tool`.

    ``content`` is a stringified payload suitable to feed straight back
    into the reasoning loop. ``is_error`` marks a tool-level or
    transport-level failure; the call still returns (never raises).
    """

    content: str
    is_error: bool = False


# ---------------------------------------------------------------------
# The manager.
# ---------------------------------------------------------------------


class McpClientManager:
    """Pool of connected external MCP servers + a tool router.

    Build with :meth:`from_config` (config-driven) or the constructor
    (explicit specs), then :meth:`connect_all`. After that
    :meth:`discovered_tools` exposes every reachable tool and
    :meth:`call_tool` routes a call to the owning server.

    The manager is the seam P16's plugin invoker bridges ``mcp``-kind
    tool calls through.
    """

    def __init__(self, specs: list[McpServerSpec] | None = None) -> None:
        self._servers: dict[str, McpManagedServer] = {}
        for spec in specs or []:
            self._servers[spec.name] = McpManagedServer(spec=spec)
        self._connected: bool = False

    @classmethod
    def from_config(cls, config: Any) -> McpClientManager:
        """Build a manager from a gateway config object. Never raises —
        a config with no MCP section yields an empty (idle) manager."""
        return cls(load_server_specs(config))

    # -- Accessors --

    @property
    def server_count(self) -> int:
        return len(self._servers)

    def server(self, name: str) -> McpManagedServer | None:
        return self._servers.get(name)

    def servers(self) -> list[McpManagedServer]:
        return list(self._servers.values())

    def ready_servers(self) -> list[McpManagedServer]:
        return [s for s in self._servers.values() if s.is_ready]

    def discovered_tools(self) -> dict[str, list[ToolDescriptor]]:
        """All discovered tools, keyed by server name. Only ``ready``
        servers contribute."""
        return {
            name: list(s.tools)
            for name, s in self._servers.items()
            if s.is_ready
        }

    def has_tool(self, server: str, tool: str) -> bool:
        s = self._servers.get(server)
        if s is None or not s.is_ready:
            return False
        return any(t.name == tool for t in s.tools)

    def find_tool(self, tool: str) -> str | None:
        """Find the first ready server advertising ``tool``. Used when
        a caller knows only the bare tool name (the agent collapses
        plugin == tool for OpenAI function calls)."""
        for name, s in self._servers.items():
            if not s.is_ready:
                continue
            if any(t.name == tool for t in s.tools):
                return name
        return None

    # -- Lifecycle --

    async def connect_all(self) -> None:
        """Connect, handshake and discover-tools for every enabled
        server concurrently. Idempotent; failures are recorded per
        server, never raised."""
        if self._connected:
            return
        self._connected = True
        pending = [
            s for s in self._servers.values() if s.spec.enabled
        ]
        for s in self._servers.values():
            if not s.spec.enabled:
                s.status = "error"
                s.error = "server disabled in config"
        if not pending:
            return
        await asyncio.gather(
            *(self._bring_up(s) for s in pending),
            return_exceptions=True,
        )

    async def _bring_up(self, managed: McpManagedServer) -> None:
        """Connect + handshake + discover one server. Folds every
        failure into ``managed.status = "error"``."""
        spec = managed.spec
        try:
            peer = await asyncio.wait_for(
                self._connect_peer(spec),
                timeout=spec.handshake_timeout_s,
            )
        except TimeoutError:
            managed.status = "error"
            managed.error = (
                f"connect timed out after {spec.handshake_timeout_s}s"
            )
            log.warning("mcp.client.connect_timeout", server=spec.name)
            return
        except McpClientError as exc:
            managed.status = "error"
            managed.error = f"connect failed: {exc}"
            log.warning(
                "mcp.client.connect_failed", server=spec.name, error=str(exc)
            )
            return
        except Exception as exc:
            managed.status = "error"
            managed.error = f"connect failed: {exc}"
            log.warning(
                "mcp.client.connect_failed", server=spec.name, error=str(exc)
            )
            return

        managed.peer = peer
        try:
            await asyncio.wait_for(
                self._handshake(peer),
                timeout=spec.handshake_timeout_s,
            )
            tools = await asyncio.wait_for(
                self._list_tools(peer),
                timeout=spec.handshake_timeout_s,
            )
        except Exception as exc:
            managed.status = "error"
            managed.error = f"handshake/discovery failed: {exc}"
            log.warning(
                "mcp.client.handshake_failed",
                server=spec.name,
                error=str(exc),
            )
            with contextlib.suppress(Exception):
                await peer.close()
            managed.peer = None
            return

        managed.tools = tools
        managed.status = "ready"
        managed.error = None
        log.info(
            "mcp.client.server_ready",
            server=spec.name,
            transport=spec.transport,
            tools=len(tools),
        )

    async def _connect_peer(self, spec: McpServerSpec) -> McpClientPeer:
        """Open the transport for ``spec``. Raises
        :class:`McpClientError` on failure."""
        transport = spec.transport.lower()
        if transport == "stdio":
            if not spec.command:
                raise McpClientError(
                    f"mcp server {spec.name!r}: stdio transport needs a command"
                )
            return await self._connect_stdio(spec)
        if transport in ("ws", "http", "https", "wss", "websocket"):
            if not spec.url:
                raise McpClientError(
                    f"mcp server {spec.name!r}: {transport} transport needs a url"
                )
            return await McpWebSocketClient.connect(
                _normalise_ws_url(spec.url),
                headers=spec.headers or None,
                open_timeout=spec.handshake_timeout_s,
            )
        raise McpClientError(
            f"mcp server {spec.name!r}: unknown transport {spec.transport!r}"
        )

    async def _connect_stdio(self, spec: McpServerSpec) -> McpClient:
        """Spawn a stdio MCP child with the spec's env layered on."""
        import os

        if spec.env:
            # McpClient.connect_stdio inherits the gateway's env wholesale
            # and offers no env hook; spawn the process ourselves so the
            # manifest-style env overlay applies.
            child_env = os.environ.copy()
            child_env.update(spec.env)
            try:
                process = await asyncio.create_subprocess_exec(
                    spec.command,
                    *spec.args,
                    env=child_env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except (FileNotFoundError, OSError) as exc:
                raise McpClientError(
                    f"failed to spawn mcp server {spec.name!r}: {exc}"
                ) from exc
            return await McpClient.connect_with_process(process)
        return await McpClient.connect_stdio(spec.command, spec.args)

    async def _handshake(self, peer: McpClientPeer) -> None:
        """Run the MCP ``initialize`` handshake against ``peer``."""
        await peer.call(
            INITIALIZE_METHOD,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": _CLIENT_NAME,
                    "version": _CLIENT_VERSION,
                },
            },
        )
        try:
            await peer.notify(INITIALIZED_NOTIFICATION, {})
        except Exception as exc:  # notification is best-effort
            log.debug("mcp.client.initialized_notify_failed", error=str(exc))

    async def _list_tools(self, peer: McpClientPeer) -> list[ToolDescriptor]:
        """Discover tools via ``tools/list``, paging until exhausted."""
        out: list[ToolDescriptor] = []
        cursor: str | None = None
        for _ in range(64):  # hard page cap — avoid a runaway cursor
            params: dict[str, Any] = {}
            if cursor:
                params["cursor"] = cursor
            result = await peer.call("tools/list", params or None)
            if not isinstance(result, dict):
                break
            for raw in result.get("tools", []) or []:
                try:
                    out.append(ToolDescriptor.model_validate(raw))
                except Exception as exc:
                    log.debug(
                        "mcp.client.bad_tool_descriptor", error=str(exc)
                    )
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return out

    # -- Calls --

    async def call_tool(
        self,
        server: str,
        tool: str,
        arguments: Any,
    ) -> McpToolCallOutcome:
        """Route a ``tools/call`` to the named server. Never raises.

        ``server`` may also be a bare tool name — if it is not a known
        server but a unique tool with that name exists, the call is
        routed there (the agent collapses plugin == tool for OpenAI
        function calls).
        """
        managed = self._servers.get(server)
        if managed is None:
            # `server` might actually be the tool name.
            owner = self.find_tool(server)
            if owner is not None:
                managed = self._servers.get(owner)
                tool = tool or server
            if managed is None:
                return McpToolCallOutcome(
                    content=_err_json(
                        "mcp_server_not_found",
                        f"no configured MCP server named {server!r}",
                    ),
                    is_error=True,
                )

        if not managed.is_ready or managed.peer is None:
            return McpToolCallOutcome(
                content=_err_json(
                    "mcp_server_unavailable",
                    (
                        f"MCP server {managed.spec.name!r} is not ready: "
                        f"{managed.error or 'not connected'}"
                    ),
                ),
                is_error=True,
            )

        tool_name = tool or server
        if not any(t.name == tool_name for t in managed.tools):
            known = sorted(t.name for t in managed.tools)
            return McpToolCallOutcome(
                content=_err_json(
                    "mcp_tool_not_found",
                    (
                        f"MCP server {managed.spec.name!r} has no tool "
                        f"{tool_name!r}; known: {known}"
                    ),
                ),
                is_error=True,
            )

        params = {"name": tool_name, "arguments": arguments or {}}
        try:
            result = await asyncio.wait_for(
                managed.peer.call("tools/call", params),
                timeout=managed.spec.call_timeout_s,
            )
        except TimeoutError:
            return McpToolCallOutcome(
                content=_err_json(
                    "mcp_call_timeout",
                    (
                        f"MCP tool {tool_name!r} on {managed.spec.name!r} "
                        f"timed out after {managed.spec.call_timeout_s}s"
                    ),
                ),
                is_error=True,
            )
        except McpClientError as exc:
            return McpToolCallOutcome(
                content=_err_json("mcp_call_failed", str(exc)),
                is_error=True,
            )
        except Exception as exc:
            return McpToolCallOutcome(
                content=_err_json("mcp_call_failed", str(exc)),
                is_error=True,
            )

        return _outcome_from_call_result(result)

    async def aclose(self) -> None:
        """Close every connected peer. Safe to call multiple times."""
        for managed in self._servers.values():
            peer = managed.peer
            if peer is None:
                continue
            with contextlib.suppress(Exception):
                await peer.close()
            managed.peer = None
            if managed.status == "ready":
                managed.status = "pending"

    async def __aenter__(self) -> McpClientManager:
        await self.connect_all()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()


# ---------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------


def _normalise_ws_url(url: str) -> str:
    """Map an ``http(s)://`` URL to its ``ws(s)://`` equivalent so a
    spec authored with an HTTP URL still dials the websocket transport."""
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    return url


def _err_json(code: str, message: str) -> str:
    import json

    return json.dumps({"error": code, "message": message})


def _outcome_from_call_result(result: Any) -> McpToolCallOutcome:
    """Fold an MCP ``tools/call`` result into a :class:`McpToolCallOutcome`.

    MCP ``tools/call`` replies are ``{"content": [...], "isError": bool}``.
    Text content blocks are concatenated; the whole structured payload is
    serialised when there is no plain-text representation.
    """
    import json

    if not isinstance(result, dict):
        return McpToolCallOutcome(
            content=json.dumps(result) if result is not None else "null",
            is_error=False,
        )

    is_error = bool(result.get("isError", False))
    content = result.get("content")
    if isinstance(content, list):
        texts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(str(block.get("text", "")))
        if texts:
            return McpToolCallOutcome(
                content="\n".join(texts), is_error=is_error
            )
    # No plain-text content — hand back the structured payload verbatim.
    return McpToolCallOutcome(content=json.dumps(result), is_error=is_error)
