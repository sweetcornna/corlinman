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
    "installed_freesearch_backend",
    "reset_freesearch_backend",
]

#: The name an operator selects in ``[web_search].backend``.
FREESEARCH_BACKEND = "freesearch"

#: Ceiling on bringing the child up: package resolve + spawn + handshake +
#: tool discovery. ``McpClientManager`` bounds each of those steps
#: separately by the spec's ``handshake_timeout_s``, which means a pathological
#: server can burn that budget several times over; this is the one number that
#: bounds the whole cold start.
#:
#: The per-*search* ceiling is deliberately not here — it is the spec's
#: ``call_timeout_s``, enforced inside ``call_tool``. A second ``wait_for``
#: wrapped around that call would just be a competing bound (whichever is
#: smaller silently wins) for no gain.
_CONNECT_TIMEOUT_S = 240.0


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
            try:
                await asyncio.wait_for(
                    manager.connect_all(), timeout=_CONNECT_TIMEOUT_S
                )
            except BaseException:
                # Includes the timeout and task cancellation: a half-connected
                # manager owns a spawned child, so it has to be closed here or
                # the process is orphaned with no reference left to reap it.
                with contextlib.suppress(Exception):
                    await manager.aclose()
                raise
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
        manager = await self._ensure_manager()
        # No extra ``wait_for`` here on purpose: ``call_tool`` already bounds
        # itself by the spec's ``call_timeout_s`` and never raises, returning
        # a structured ``mcp_call_timeout`` outcome instead. Wrapping it would
        # add a second, competing deadline whose only effect is to decide
        # which of the two error messages the model gets.
        outcome = await manager.call_tool(
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


#: The one backend this process owns. Module-level because the install
#: path runs repeatedly (see :func:`install_freesearch_backend`) and each
#: instance can own a live child process.
_INSTALLED: FreeSearchBackend | None = None


def installed_freesearch_backend() -> FreeSearchBackend | None:
    """The process's backend, if one has been installed."""
    return _INSTALLED


def reset_freesearch_backend() -> None:
    """Forget the installed backend without closing it (tests only)."""
    global _INSTALLED
    _INSTALLED = None


def install_freesearch_backend() -> FreeSearchBackend | None:
    """Register the ``freesearch`` backend for this process.

    Called unconditionally on the agent-process config path: registration is
    inert until an operator actually selects the backend, and doing it
    unconditionally means selecting it in the UI takes effect on the next
    sidecar reload rather than needing a restart.

    **Idempotent, and that is load-bearing.** This runs on *every* sidecar
    reload — i.e. every time an operator saves any agent-facing config in
    the UI, which since the hot-apply work is a routine, restart-free
    action. Building a fresh backend each time would drop the previous
    instance while its ``uvx`` child process was still running, orphaning
    one search server per config save for the life of the process. So an
    already-installed backend is reused, and only its registry entry is
    re-asserted (cheap, and repairs the entry if something unregistered it).

    Returns the backend (for teardown / tests), or ``None`` when the pieces
    aren't importable — a build without the MCP package simply has no
    ``freesearch``, which ``web_search`` reports as ``backend_unavailable``.
    """
    global _INSTALLED

    if _INSTALLED is not None:
        try:
            from corlinman_agent.web.search import register_search_backend

            register_search_backend(FREESEARCH_BACKEND, _INSTALLED.search)
        except Exception as exc:  # noqa: BLE001 — optional wiring, never fatal
            logger.debug("web_search.freesearch_reassert_failed", error=str(exc))
        return _INSTALLED

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
    _INSTALLED = backend
    return backend
