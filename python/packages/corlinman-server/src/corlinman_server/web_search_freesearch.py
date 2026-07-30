"""The ``freesearch`` backend for the builtin ``web_search`` tool.

Backs ``web_search`` with corlinman's bundled multi-engine MCP search server
instead of the keyless DuckDuckGo HTML scrape, so the *builtin* tool gets the
same engine pool and fallback behaviour the model gets from the advertised
``search_*`` MCP tools. That matters because the two reach the model by
different routes: MCP tools are advertised into ``tools_json`` by the
gateway, while ``web_search`` is a builtin every channel and every persona
can name directly.

Why this lives here and not in ``corlinman-agent``
--------------------------------------------------

``web_search`` executes in the **agent** process
(``agent_servicer`` → ``corlinman_agent.web.search``), which is a separate
process from the gateway that owns the live
:class:`~corlinman_mcp_server.McpClientManager`. The agent cannot borrow the
gateway's connection, and ``corlinman-agent`` deliberately does not depend on
``corlinman-mcp-server``. So this module — in ``corlinman-server``, which
depends on both — owns its own short client and registers itself through the
:func:`~corlinman_agent.web.search.register_search_backend` seam.

The cost is honest and worth naming: with ``backend = "freesearch"`` the
agent process runs its *own* search child, separate from the one the gateway
spawned for the advertised tools. That is why this is opt-in rather than the
default — see ``[web_search].backend`` in ``docs/config.example.toml``.

The child is spawned lazily on the first search and reused afterwards, so an
operator who never selects this backend never pays for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

__all__ = [
    "FREESEARCH_BACKEND",
    "FreeSearchBackend",
    "install_freesearch_backend",
]

#: The name an operator selects in ``[web_search].backend``.
FREESEARCH_BACKEND = "freesearch"

#: How long one search may take before the caller gets a degraded envelope.
#: Generous because a cold child pays a package resolve on first use.
_FIRST_CALL_TIMEOUT_S = 180.0
_CALL_TIMEOUT_S = 60.0


class FreeSearchBackend:
    """Lazily-connected MCP client wrapping the bundled search server.

    One instance per process. :meth:`search` is safe to call concurrently:
    the first caller through the door does the connect while the rest wait on
    the same lock, so a burst of parallel tool calls cannot spawn a pile of
    duplicate children.
    """

    def __init__(self, spec: Any) -> None:
        self._spec = spec
        self._manager: Any | None = None
        self._lock = asyncio.Lock()

    async def _ensure_manager(self) -> Any:
        """Connect on first use; reuse thereafter.

        A manager whose server failed to come up is torn down rather than
        cached, so a transient failure (uv not on PATH yet, no network on the
        first boot) does not poison the backend for the process lifetime.
        """
        if self._manager is not None:
            return self._manager
        async with self._lock:
            if self._manager is not None:
                return self._manager
            from corlinman_mcp_server import McpClientManager

            manager = McpClientManager([self._spec])
            await manager.connect_all()
            server = manager.server(self._spec.name)
            if server is None or not server.is_ready:
                reason = (server.error if server else "no such server") or "unknown"
                with contextlib.suppress(Exception):
                    await manager.aclose()
                raise RuntimeError(f"bundled search server unavailable: {reason}")
            logger.info(
                "web_search.freesearch_connected",
                protocol_version=getattr(server, "protocol_version", ""),
                tools=len(server.tools),
            )
            self._manager = manager
            return manager

    async def search(self, query: str, max_results: int) -> list[dict[str, str]]:
        """Run one search. Returns ``[{"title","url","snippet"}, ...]``.

        Raises on failure — :func:`corlinman_agent.web.search.\
_dispatch_external` owns turning that into the degraded envelope, so the
        error text reaches the model instead of being swallowed here.
        """
        cold = self._manager is None
        manager = await self._ensure_manager()
        timeout = _FIRST_CALL_TIMEOUT_S if cold else _CALL_TIMEOUT_S
        outcome = await asyncio.wait_for(
            manager.call_tool(
                "search",
                "search",
                {
                    "query": query,
                    "max_results": max_results,
                    # The JSON rendering is the parseable one; the default
                    # markdown is written for a model to read, not for us to
                    # scrape back apart.
                    "format": "json",
                },
            ),
            timeout=timeout,
        )
        if outcome.is_error:
            raise RuntimeError(f"search tool failed: {outcome.content[:300]}")
        return _rows_from_payload(outcome.content)

    async def aclose(self) -> None:
        manager, self._manager = self._manager, None
        if manager is not None:
            with contextlib.suppress(Exception):
                await manager.aclose()


def _rows_from_payload(content: str) -> list[dict[str, str]]:
    """Pull the result rows out of a ``search`` tool result.

    The server returns ``{"query", "engines", "results": [...], ...}`` where
    each row carries ``title`` / ``url`` / ``snippet`` (plus provenance we
    drop — ``web_search``'s wire contract has no field for it). Anything
    unparseable yields no rows rather than raising: an empty result set is a
    truthful "found nothing", which beats an exception for a search tool.
    """
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    rows = payload.get("results")
    if not isinstance(rows, list):
        return []
    out: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "title": str(row.get("title") or ""),
                "url": str(row.get("url") or ""),
                "snippet": str(row.get("snippet") or ""),
            }
        )
    return out


def install_freesearch_backend() -> FreeSearchBackend | None:
    """Register the ``freesearch`` backend for this process.

    Called unconditionally on the agent-process config path: registration is
    inert until an operator actually selects the backend, and doing it
    unconditionally means selecting it in the UI takes effect on the next
    sidecar reload rather than needing a restart.

    Returns the backend (for teardown / tests), or ``None`` when the pieces
    aren't importable — a build without the MCP package simply has no
    ``freesearch``, which ``web_search`` reports as ``backend_unavailable``.
    """
    try:
        from corlinman_agent.web.search import register_search_backend
        from corlinman_mcp_server import McpServerSpec

        from corlinman_server.gateway.lifecycle.bundled_mcp import (
            BUNDLED_MCP_SERVERS,
        )
    except Exception as exc:  # noqa: BLE001 — optional wiring, never fatal
        logger.debug("web_search.freesearch_unavailable", error=str(exc))
        return None

    entry = next((e for e in BUNDLED_MCP_SERVERS if e.name == "search"), None)
    if entry is None:  # pragma: no cover — bundle always ships one
        return None

    backend = FreeSearchBackend(McpServerSpec.from_mapping(entry.name, entry.spec))
    register_search_backend(FREESEARCH_BACKEND, backend.search)
    return backend
