"""Malformed-frame resilience tests for the server connection loop.

A well-formed-JSON frame carrying a known ``kind`` but an unexpected or
missing field used to crash the per-connection reader loop with a
``TypeError`` (escaping the per-frame ``except (ValueError,
json.JSONDecodeError)`` handler). Because the connection-loop cleanup
was not in a ``finally``, the crash left the runner registered forever
and any in-flight ``invoke`` waiter hung until its deadline.

These tests dial the gateway with a raw websocket so we can inject an
arbitrary text frame the runner client library would never produce.
"""

from __future__ import annotations

import asyncio

import pytest
import websockets

from corlinman_wstool import Disconnected
from corlinman_wstool.protocol import WsToolMessage
from corlinman_wstool.server import _connection_loop

from .conftest import Harness, simple_advert


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_malformed_frame_is_tolerated_then_cleans_up_on_close(
    harness: Harness,
) -> None:
    """A single malformed frame must NOT crash the reader loop. The runner
    stays connected and its pending invoke stays in-flight; only the
    eventual socket close tears it down (deregister + fail_pending).
    """
    url = (
        f"{harness.ws_url}/wstool/connect"
        f"?auth_token={harness.token}&runner_id=rx-bad&version=0.1.0"
    )
    invoke_task: asyncio.Task[object]
    async with websockets.connect(url, ping_interval=None) as ws:
        accept = WsToolMessage.Accept(
            server_version="0.1.0",
            heartbeat_secs=15,
            supported_tools=[simple_advert("bad.echo")],
        )
        await ws.send(accept.to_json())
        await _wait_until(lambda: "bad.echo" in harness.server.advertised_tools())
        assert harness.server.runner_count() == 1

        # Kick off an invoke the runner will never answer (generous
        # timeout so any premature failure can't be a deadline timeout).
        invoke_task = asyncio.create_task(
            harness.server.invoke("bad.echo", {"hello": "world"}, timeout_ms=30_000)
        )
        await asyncio.sleep(0.05)

        # Inject a well-formed-JSON frame with a known kind but an
        # unexpected field. ``from_dict`` previously raised TypeError
        # (escaping the reader's handler + leaking the runner). Now it is
        # a ValueError caught as a bad frame and ignored.
        await ws.send('{"kind":"pong","extra":1}')
        await asyncio.sleep(0.1)

        # The connection survived the bad frame: runner still registered,
        # invoke still pending (not crashed, not failed early).
        assert harness.server.runner_count() == 1
        assert not invoke_task.done()

    # Socket closed -> the (finally-guarded) cleanup must deregister the
    # runner and fail the pending invoke promptly, well under the 30s
    # invoke deadline.
    with pytest.raises(Disconnected):
        await asyncio.wait_for(invoke_task, timeout=3.0)
    await _wait_until(lambda: harness.server.runner_count() == 0)
    assert "bad.echo" not in harness.server.advertised_tools()


class _BoomConnection:
    """Minimal ``ws`` stand-in whose first frame triggers a fatal Accept,
    then whose async iteration raises an unexpected (non-ConnectionClosed)
    error inside the reader loop — proving the cleanup ``finally`` runs
    even on an exception the loop does not explicitly catch.
    """

    def __init__(self, accept_json: str) -> None:
        self._accept_json = accept_json
        self.closed = False

    async def recv(self) -> str:
        return self._accept_json

    def __aiter__(self) -> _BoomConnection:
        return self

    async def __anext__(self) -> str:
        raise RuntimeError("boom: unexpected reader-loop failure")

    async def send(self, _text: str) -> None:  # pragma: no cover - writer drain
        return None

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_connection_loop_cleanup_runs_on_unexpected_exception(
    harness: Harness,
) -> None:
    """Even an exception the reader loop does not catch must still run the
    deregister + fail_pending cleanup via ``finally`` (no leaked runner).
    """
    state = harness.server.state
    accept = WsToolMessage.Accept(
        server_version="0.1.0",
        heartbeat_secs=15,
        supported_tools=[simple_advert("boom.echo")],
    )
    ws = _BoomConnection(accept.to_json())

    with pytest.raises(RuntimeError, match="boom"):
        await _connection_loop(ws, state, "rx-boom")  # type: ignore[arg-type]

    # The runner that registered during handshake must be gone.
    assert state.runner_count() == 0
    assert "boom.echo" not in state.advertised_tools()
