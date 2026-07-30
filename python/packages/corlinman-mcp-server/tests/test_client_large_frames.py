"""Large stdio frames must not kill the connection.

Found in production the minute the bundled search server went live: a
``fetch_batch`` result exceeded asyncio's default 64 KiB StreamReader limit,
``readline()`` raised, the reader loop broke, and from then on the server was
"connected" but answered nothing — every later tool call sat until its 30s
timeout and the user got an empty reply.

Two properties are pinned here: normal large payloads go through, and a frame
past even the raised ceiling costs one call rather than the whole peer.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path

import pytest
from corlinman_mcp_server.client import (
    STDIO_STREAM_LIMIT,
    McpClient,
    McpClientDisconnected,
)

# Emits a reply whose size the caller dictates, then keeps serving — so a
# test can prove the peer is still usable after a huge (or too-huge) frame.
_BIG_FRAME_SERVER = textwrap.dedent(
    """
    import json, sys

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        rid = req.get("id")
        params = req.get("params") or {}
        if req.get("method") == "big":
            payload = "x" * int(params.get("size", 0))
            sys.stdout.write(
                json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"data": payload}})
                + "\\n"
            )
        else:
            sys.stdout.write(
                json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"ok": True}}) + "\\n"
            )
        sys.stdout.flush()
    """
).strip()


@pytest.fixture
def big_frame_server(tmp_path: Path) -> Path:
    script = tmp_path / "big_frame_server.py"
    script.write_text(_BIG_FRAME_SERVER, encoding="utf-8")
    return script


@pytest.mark.asyncio
async def test_a_frame_far_over_the_asyncio_default_is_read(
    big_frame_server: Path,
) -> None:
    """1 MiB is unremarkable for a document read or a batch fetch, and is
    16x asyncio's default limit — the exact size class that broke prod."""
    client = await McpClient.connect_stdio(sys.executable, [str(big_frame_server)])
    try:
        size = 1024 * 1024
        result = await asyncio.wait_for(
            client.call("big", {"size": size}), timeout=30
        )
        assert len(result["data"]) == size
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_the_peer_survives_a_frame_past_the_ceiling(
    big_frame_server: Path,
) -> None:
    """Over the ceiling the owning call fails — but the server must stay
    usable. Breaking the reader loop (the old behaviour) left a peer that
    accepted calls and answered none of them, which is how one oversized
    result turned into an empty reply for the user.

    Driven through a deliberately tiny stream limit rather than the real
    32 MiB one: this exercises the identical ``ValueError`` branch in the
    reader loop without spending a minute of CI generating 33 MiB.
    """
    small_limit = 64 * 1024
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(big_frame_server),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=small_limit,
    )
    client = await McpClient.connect_with_process(process)
    try:
        # The dropped frame cannot be attributed to a request id (it never
        # parsed), so its caller waits out its own deadline rather than
        # failing fast. Bounded and acceptable; the short timeout here just
        # asserts "did not succeed" without paying for a real one.
        # ``call`` turns the resulting cancellation into McpClientDisconnected.
        with pytest.raises((asyncio.TimeoutError, McpClientDisconnected)):
            await asyncio.wait_for(
                client.call("big", {"size": small_limit * 4}), timeout=3
            )

        # The peer is still alive and answering — the property that matters.
        again = await asyncio.wait_for(client.call("ping", {}), timeout=20)
        assert again == {"ok": True}
    finally:
        await client.close()


def test_the_limit_is_far_above_asyncio_default() -> None:
    """A regression guard on the constant itself: reverting it to the
    stdlib default silently restores the production failure."""
    assert STDIO_STREAM_LIMIT >= 8 * 1024 * 1024
    assert STDIO_STREAM_LIMIT > 64 * 1024


def test_json_line_of_a_megabyte_round_trips() -> None:
    """Sanity check on the framing assumption: one tool result is one
    line, so the line length is the payload length."""
    payload = {"data": "y" * (1024 * 1024)}
    encoded = json.dumps(payload)
    assert "\n" not in encoded
    assert len(encoded) > 1024 * 1024
