"""The MCP-backed ``freesearch`` backend for ``web_search``.

Covers the payload mapping and the lazy-connect contract without spawning a
real search server: the connect path is exercised through a stub manager, so
these stay hermetic.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, ClassVar

import pytest
from corlinman_agent.web.search import (
    registered_search_backends,
    unregister_search_backend,
)
from corlinman_server.web_search_freesearch import (
    FREESEARCH_BACKEND,
    FreeSearchBackend,
    _rows_from_payload,
    install_freesearch_backend,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    for name in list(registered_search_backends()):
        unregister_search_backend(name)
    yield
    for name in list(registered_search_backends()):
        unregister_search_backend(name)


# ─── payload mapping ─────────────────────────────────────────────────


def test_rows_are_mapped_from_the_search_payload() -> None:
    payload = json.dumps(
        {
            "query": "rrf",
            "engines": ["duckduckgo", "mojeek"],
            "results": [
                {
                    "title": "Reciprocal rank fusion",
                    "url": "https://example.com/rrf",
                    "snippet": "RRF combines rankings",
                    "engines": ["mojeek"],
                    "score": 0.9,
                }
            ],
        }
    )
    assert _rows_from_payload(payload) == [
        {
            "title": "Reciprocal rank fusion",
            "url": "https://example.com/rrf",
            "snippet": "RRF combines rankings",
        }
    ]


@pytest.mark.parametrize(
    "content",
    ["", "not json", "[]", '"a string"', '{"results": "not a list"}', "{}"],
)
def test_unparseable_payloads_yield_no_rows(content: str) -> None:
    """An empty result set is a truthful "found nothing"; raising would turn
    a bad payload into a failed turn."""
    assert _rows_from_payload(content) == []


def test_missing_fields_become_empty_strings() -> None:
    """``web_search``'s envelope has three string fields; a row missing one
    must not carry ``None`` into it (the agent-side filter drops the row)."""
    rows = _rows_from_payload(json.dumps({"results": [{"url": "https://x/a"}]}))
    assert rows == [{"title": "", "url": "https://x/a", "snippet": ""}]


# ─── lazy connect ────────────────────────────────────────────────────


class _StubServer:
    def __init__(self, ready: bool) -> None:
        self.status = "ready" if ready else "error"
        self.error = None if ready else "spawn failed: uvx not found"
        self.tools: list[Any] = []
        self.protocol_version = "2025-11-25"

    @property
    def is_ready(self) -> bool:
        return self.status == "ready"


class _StubManager:
    """Stands in for McpClientManager; counts connects."""

    instances: ClassVar[list[_StubManager]] = []

    def __init__(self, specs, ready: bool = True, result: Any = None) -> None:
        self.specs = specs
        self._server = _StubServer(ready)
        self._result = result
        self.connects = 0
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False
        _StubManager.instances.append(self)

    async def connect_all(self) -> None:
        self.connects += 1

    def server(self, name: str) -> _StubServer:
        return self._server

    async def call_tool(self, server: str, tool: str, args: dict[str, Any]):
        self.calls.append((server, tool, args))
        return self._result

    async def aclose(self) -> None:
        self.closed = True


class _Outcome:
    def __init__(self, content: str, is_error: bool = False) -> None:
        self.content = content
        self.is_error = is_error


def _install_stub(monkeypatch, **kw) -> None:
    import corlinman_mcp_server

    _StubManager.instances.clear()
    monkeypatch.setattr(
        corlinman_mcp_server,
        "McpClientManager",
        lambda specs, **_: _StubManager(specs, **kw),
    )


class _Spec:
    name = "search"


@pytest.mark.asyncio
async def test_connects_once_and_reuses(monkeypatch) -> None:
    payload = json.dumps({"results": [{"title": "t", "url": "https://x/a"}]})
    _install_stub(monkeypatch, result=_Outcome(payload))
    backend = FreeSearchBackend(_Spec())

    await backend.search("a", 5)
    await backend.search("b", 5)

    assert len(_StubManager.instances) == 1
    assert _StubManager.instances[0].connects == 1


@pytest.mark.asyncio
async def test_concurrent_first_calls_spawn_one_child(monkeypatch) -> None:
    """A burst of parallel tool calls must not each spawn a search server."""
    payload = json.dumps({"results": []})
    _install_stub(monkeypatch, result=_Outcome(payload))
    backend = FreeSearchBackend(_Spec())

    await asyncio.gather(*(backend.search(f"q{i}", 5) for i in range(8)))
    assert len(_StubManager.instances) == 1


@pytest.mark.asyncio
async def test_the_json_format_is_requested(monkeypatch) -> None:
    """The markdown rendering is written for a model to read; we need the
    parseable one."""
    _install_stub(monkeypatch, result=_Outcome(json.dumps({"results": []})))
    backend = FreeSearchBackend(_Spec())
    await backend.search("rrf", 3)

    _server, tool, args = _StubManager.instances[0].calls[0]
    assert tool == "search"
    assert args["format"] == "json"
    assert args["query"] == "rrf"
    assert args["max_results"] == 3


@pytest.mark.asyncio
async def test_a_failed_connect_is_not_cached(monkeypatch) -> None:
    """A transient failure (no uv on PATH yet, no network on first boot)
    must not poison the backend for the process lifetime."""
    _install_stub(monkeypatch, ready=False)
    backend = FreeSearchBackend(_Spec())

    for _ in range(2):
        with pytest.raises(RuntimeError, match="unavailable"):
            await backend.search("a", 5)

    assert len(_StubManager.instances) == 2  # retried, not memoised
    assert all(m.closed for m in _StubManager.instances)


@pytest.mark.asyncio
async def test_tool_level_error_raises(monkeypatch) -> None:
    """The agent-side dispatcher turns this into the degraded envelope, so
    the reason has to survive as an exception rather than empty results."""
    _install_stub(monkeypatch, result=_Outcome("rate limited", is_error=True))
    backend = FreeSearchBackend(_Spec())
    with pytest.raises(RuntimeError, match="rate limited"):
        await backend.search("a", 5)


# ─── registration ────────────────────────────────────────────────────


def test_install_registers_under_the_configured_name() -> None:
    backend = install_freesearch_backend()
    assert backend is not None
    assert FREESEARCH_BACKEND in registered_search_backends()


def test_install_uses_the_bundled_spec() -> None:
    """One source of truth for the command: the same bundle entry the
    gateway seeds into the marketplace."""
    from corlinman_server.gateway.lifecycle.bundled_mcp import BUNDLED_MCP_SERVERS

    backend = install_freesearch_backend()
    assert backend is not None
    entry = next(e for e in BUNDLED_MCP_SERVERS if e.name == "search")
    assert backend._spec.command == entry.spec["command"]
    assert backend._spec.args == entry.spec["args"]
