"""The MCP client against a *modern* server.

The old client hard-coded ``2024-11-05`` as its ``initialize`` offer. Modern
servers accept that verbatim, so nothing ever failed — they just served the
2024 feature set, and ``structuredContent`` / ``resource_link`` / audio
blocks were dropped on the floor between the wire and the reasoning loop.

These tests pin the fixed behaviour end-to-end against a fake stdio server
that (a) reports back which revision the client offered and (b) returns the
post-2024 payload shapes.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest
from corlinman_mcp_server import (
    CLIENT_PROTOCOL_VERSION,
    McpClientManager,
    McpServerSpec,
    load_server_specs,
)
from corlinman_mcp_server.client_manager import _outcome_from_call_result
from corlinman_mcp_server.protocol import OLDEST_SUPPORTED_VERSION

# ─── A fake stdio MCP server that speaks the modern payload shapes ────
#
# `initialize` echoes back whatever protocolVersion the client offered, so
# `managed.protocol_version` is direct evidence of what went out on the wire.
# It advertises `tools` only — no `resources` — and records every method it
# saw to a sidecar file so a test can assert a probe did *not* happen.

_MODERN_MCP_SERVER = textwrap.dedent(
    """
    import json, os, sys

    SEEN = os.environ["FAKE_MCP_SEEN"]
    CAPS = json.loads(os.environ.get("FAKE_MCP_CAPS", '{"tools": {}}'))
    FORCE_VERSION = os.environ.get("FAKE_MCP_FORCE_VERSION", "")

    TOOLS = [
        {
            "name": "structured",
            "description": "returns prose and a machine payload",
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": "object"},
        },
        {
            "name": "structured_only",
            "description": "returns only a machine payload",
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": "object"},
        },
        {
            "name": "blocks",
            "description": "returns non-text content blocks",
            "inputSchema": {"type": "object"},
        },
    ]

    def note(method):
        with open(SEEN, "a", encoding="utf-8") as fh:
            fh.write(method + "\\n")

    def reply(rid, result):
        sys.stdout.write(
            json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n"
        )
        sys.stdout.flush()

    def err(rid, code, message):
        sys.stdout.write(
            json.dumps(
                {"jsonrpc": "2.0", "id": rid,
                 "error": {"code": code, "message": message}}
            )
            + "\\n"
        )
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        method = req.get("method")
        rid = req.get("id")
        note(method)
        if method == "initialize":
            offered = (req.get("params") or {}).get("protocolVersion")
            reply(rid, {
                "protocolVersion": FORCE_VERSION or offered,
                "capabilities": CAPS,
                "serverInfo": {"name": "fake-modern", "version": "9.9.9"},
                "instructions": "prefer the structured tool",
            })
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            reply(rid, {"tools": TOOLS})
        elif method == "resources/list":
            reply(rid, {"resources": [
                {"uri": "file:///a.txt", "name": "a", "title": "A file",
                 "size": 12}
            ]})
        elif method == "tools/call":
            name = (req.get("params") or {}).get("name")
            if name == "structured":
                reply(rid, {
                    "content": [{"type": "text", "text": "3 hits"}],
                    "structuredContent": {"hits": 3},
                    "isError": False,
                })
            elif name == "structured_only":
                reply(rid, {
                    "content": [],
                    "structuredContent": {"hits": 7},
                    "isError": False,
                })
            elif name == "blocks":
                reply(rid, {
                    "content": [
                        {"type": "resource_link", "uri": "https://x/doc",
                         "name": "doc", "title": "The Doc"},
                        {"type": "image", "data": "AAAA", "mimeType": "image/png"},
                        {"type": "audio", "data": "BBBB", "mimeType": "audio/wav"},
                        {"type": "resource", "resource": {
                            "uri": "file:///b.txt", "text": "inlined body"}},
                    ],
                    "isError": False,
                })
            else:
                err(rid, -32601, "no such tool: " + str(name))
        else:
            err(rid, -32601, "no such method: " + str(method))
    """
).strip()


@pytest.fixture
def modern_server(tmp_path: Path) -> Path:
    script = tmp_path / "fake_modern_mcp.py"
    script.write_text(_MODERN_MCP_SERVER, encoding="utf-8")
    return script


def _spec(
    script: Path,
    seen: Path,
    *,
    caps: str = '{"tools": {}}',
    force_version: str = "",
) -> McpServerSpec:
    return McpServerSpec(
        name="modern",
        transport="stdio",
        command=sys.executable,
        args=[str(script)],
        env={
            "FAKE_MCP_SEEN": str(seen),
            "FAKE_MCP_CAPS": caps,
            "FAKE_MCP_FORCE_VERSION": force_version,
        },
    )


def _methods_seen(seen: Path) -> list[str]:
    if not seen.exists():
        return []
    return seen.read_text(encoding="utf-8").split()


# ─── version negotiation ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_client_offers_the_modern_revision(
    modern_server: Path, tmp_path: Path
) -> None:
    """The handshake offers the newest revision we understand — the
    regression that made every modern server serve its 2024 feature set."""
    seen = tmp_path / "seen.txt"
    manager = McpClientManager([_spec(modern_server, seen)])
    try:
        await manager.connect_all()
        server = manager.server("modern")
        assert server is not None and server.is_ready, server and server.error
        # The fake echoes our offer back, so this *is* what we sent.
        assert server.protocol_version == CLIENT_PROTOCOL_VERSION
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_handshake_records_capabilities_and_server_info(
    modern_server: Path, tmp_path: Path
) -> None:
    """``initialize`` carries more than a version; none of it used to be
    kept."""
    seen = tmp_path / "seen.txt"
    manager = McpClientManager([_spec(modern_server, seen)])
    try:
        await manager.connect_all()
        server = manager.server("modern")
        assert server is not None
        assert server.capabilities == {"tools": {}}
        assert server.server_info["name"] == "fake-modern"
        assert server.instructions == "prefer the structured tool"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_unknown_counter_offer_degrades_to_the_floor(
    modern_server: Path, tmp_path: Path
) -> None:
    """A server naming a revision we don't know must not take the
    connection down, and must not be believed either."""
    seen = tmp_path / "seen.txt"
    manager = McpClientManager(
        [_spec(modern_server, seen, force_version="2099-01-01")]
    )
    try:
        await manager.connect_all()
        server = manager.server("modern")
        assert server is not None
        assert server.is_ready, server.error
        assert server.protocol_version == OLDEST_SUPPORTED_VERSION
    finally:
        await manager.aclose()


# ─── capability-gated discovery ──────────────────────────────────────


@pytest.mark.asyncio
async def test_resources_probe_is_skipped_when_not_advertised(
    modern_server: Path, tmp_path: Path
) -> None:
    """A tools-only server should not be asked for resources at all."""
    seen = tmp_path / "seen.txt"
    manager = McpClientManager([_spec(modern_server, seen)])
    try:
        await manager.connect_all()
        assert "resources/list" not in _methods_seen(seen)
        server = manager.server("modern")
        assert server is not None and server.resources == []
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_resources_are_discovered_when_advertised(
    modern_server: Path, tmp_path: Path
) -> None:
    """…and a server that does advertise them still gets probed, with the
    2025-06-18 descriptor fields carried through."""
    seen = tmp_path / "seen.txt"
    manager = McpClientManager(
        [_spec(modern_server, seen, caps='{"tools": {}, "resources": {}}')]
    )
    try:
        await manager.connect_all()
        assert "resources/list" in _methods_seen(seen)
        server = manager.server("modern")
        assert server is not None
        assert [r.uri for r in server.resources] == ["file:///a.txt"]
        assert server.resources[0].title == "A file"
        assert server.resources[0].size == 12
    finally:
        await manager.aclose()


# ─── result folding ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rendered_text_wins_over_structured_content(
    modern_server: Path, tmp_path: Path
) -> None:
    """A compliant server sends ``structuredContent`` *and* its
    serialisation as a text block, so emitting both would hand the model
    the same payload twice and bill it twice."""
    seen = tmp_path / "seen.txt"
    manager = McpClientManager([_spec(modern_server, seen)])
    try:
        await manager.connect_all()
        outcome = await manager.call_tool("modern", "structured", {})
        assert outcome.is_error is False
        assert outcome.content == "3 hits"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_structured_only_result_is_returned_unlabelled(
    modern_server: Path, tmp_path: Path
) -> None:
    """…but a tool with an outputSchema and *no* prose used to fold to an
    empty string. That is the case structuredContent rescues."""
    seen = tmp_path / "seen.txt"
    manager = McpClientManager([_spec(modern_server, seen)])
    try:
        await manager.connect_all()
        outcome = await manager.call_tool("modern", "structured_only", {})
        assert json.loads(outcome.content) == {"hits": 7}
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_non_text_blocks_render_as_placeholders(
    modern_server: Path, tmp_path: Path
) -> None:
    """resource_link / image / audio / embedded resource each render to
    something readable instead of the whole envelope being json-dumped."""
    seen = tmp_path / "seen.txt"
    manager = McpClientManager([_spec(modern_server, seen)])
    try:
        await manager.connect_all()
        outcome = await manager.call_tool("modern", "blocks", {})
        content = outcome.content
        assert "https://x/doc" in content
        assert "The Doc" in content
        assert "[image content (image/png) omitted]" in content
        assert "[audio content (audio/wav) omitted]" in content
        # The embedded resource's body is inlined verbatim, not summarised.
        assert "inlined body" in content
        # The base64 payloads must not reach the model.
        assert "AAAA" not in content
        assert "BBBB" not in content
    finally:
        await manager.aclose()


# ─── folding edge cases (direct) ─────────────────────────────────────


def test_fold_preserves_is_error() -> None:
    outcome = _outcome_from_call_result(
        {"content": [{"type": "text", "text": "boom"}], "isError": True}
    )
    assert outcome.is_error is True
    assert outcome.content == "boom"


def test_fold_unrenderable_envelope_is_kept_verbatim() -> None:
    """An all-unknown-blocks result must stay inspectable rather than
    silently folding to an empty string."""
    payload = {"content": [{"type": "future_block", "x": 1}], "isError": False}
    outcome = _outcome_from_call_result(payload)
    assert json.loads(outcome.content) == payload


def test_fold_unserialisable_structured_content_does_not_raise() -> None:
    """``structuredContent`` is whatever the peer sent; a value json can't
    encode must degrade, not crash the tool call."""
    outcome = _outcome_from_call_result(
        {"content": [], "structuredContent": {"bad": {1, 2}}, "isError": False}
    )
    assert "bad" in outcome.content


# ─── transport inference ─────────────────────────────────────────────


def test_https_url_infers_streamable_http() -> None:
    """The bug this replaces: a URL-only spec was coerced to a websocket
    dial, making every remote MCP server unreachable."""
    specs = {
        s.name: s
        for s in load_server_specs(
            {"mcp_servers": {"remote": {"url": "https://example.com/mcp"}}}
        )
    }
    assert specs["remote"].transport == "http"


def test_ws_url_still_infers_websocket() -> None:
    """corlinman's own hosted MCP server is a websocket — gateway-to-
    gateway config must keep working untouched."""
    specs = {
        s.name: s
        for s in load_server_specs(
            {"mcp_servers": {"peer": {"url": "ws://localhost:9000/mcp"}}}
        )
    }
    assert specs["peer"].transport == "ws"


def test_explicit_transport_always_wins() -> None:
    """An operator naming ``ws`` with an https URL still gets a websocket."""
    specs = {
        s.name: s
        for s in load_server_specs(
            {
                "mcp_servers": {
                    "forced": {"transport": "ws", "url": "https://h/mcp"}
                }
            }
        )
    }
    assert specs["forced"].transport == "ws"


@pytest.mark.asyncio
async def test_teardown_clears_the_negotiated_state(
    modern_server: Path, tmp_path: Path
) -> None:
    """A disabled/torn-down server must not keep reporting the capabilities
    and revision of its previous connection."""
    seen = tmp_path / "seen.txt"
    manager = McpClientManager(
        [_spec(modern_server, seen, caps='{"tools": {}, "resources": {}}')]
    )
    try:
        await manager.connect_all()
        server = manager.server("modern")
        assert server is not None and server.capabilities
        assert await manager.disable_one("modern") is True
        assert server.capabilities == {}
        assert server.server_info == {}
        assert server.instructions == ""
        assert server.resources == []
        assert server.protocol_version == OLDEST_SUPPORTED_VERSION
    finally:
        await manager.aclose()
