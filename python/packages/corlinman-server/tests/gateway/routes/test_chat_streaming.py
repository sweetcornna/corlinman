"""Regression coverage for client-disconnect cancel propagation (TEST-002).

The streaming branch of ``POST /v1/chat/completions`` polls
:meth:`starlette.requests.Request.is_disconnected` between every SSE
chunk and flips the shared :class:`asyncio.Event` ``cancel`` when the
client drops the connection. The downstream
:meth:`ChatService.run` consumes that event to tear down the upstream
provider call so the user is **not** billed for tokens generated after
their browser tab closed.

The existing ``test_chat_tracing.py`` only asserts span attributes and
never exercises this branch. The audit scout-test pass flagged it as a
cost / leak gap (audit#TEST-002): a regression that silently broke the
``cancel.set()`` write — for example refactoring the handler to drop the
``finally:`` arm, or moving the disconnect poll to *after* the next
``yield chunk`` — would not be caught by any existing test.

Strategy
--------
* Drive the FastAPI app through the raw ASGI protocol so we control the
  ``receive`` callable directly (the synchronous ``TestClient`` cannot
  simulate a mid-stream disconnect — it pre-drains the response body).
* Wire a scripted :class:`ChatService` stand-in whose ``run`` generator
  yields slow tokens and **records the live ``cancel`` event reference**
  it was handed. After the disconnect fires, the test asserts on that
  reference.
* Assert both halves of the contract:
    1. **Direct observation** — the scripted service saw ``cancel.set()``
       become true (this is what the real
       :func:`chat_service._run_chat` loop uses to short-circuit).
    2. **Structural** — the scripted service emitted strictly fewer
       tokens than it was scripted to (proves the route stopped pulling
       events, which is what stops upstream provider work in production).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from corlinman_server.gateway.routes.chat import (
    ChatState,
    ModelRedirect,
    router,
)
from corlinman_server.gateway_api import (
    DoneEvent,
    InternalChatRequest,
    TokenDeltaEvent,
)
from fastapi import FastAPI

# ─── Scripted service that records the live ``cancel`` reference ──────────
#
# The route hands its locally-constructed ``asyncio.Event`` to
# ``service.run(req, cancel)``; in production
# :func:`corlinman_server.gateway.services.chat_service._run_chat` polls
# ``cancel.is_set()`` between frames to abort the upstream gRPC call.
#
# We replicate the "yields slow tokens, watches cancel" half so the test
# observes the very same boolean flip the production loop would observe.


class _SlowScriptedChatService:
    """Scripted ``ChatService`` whose generator emits N tokens with a
    short sleep between each and stops the moment ``cancel.is_set()``.

    Records the bound ``cancel`` event and the number of tokens actually
    yielded so the test can assert directly on cancel propagation.
    """

    # How many tokens the script *would* emit if never cancelled.
    SCRIPTED_TOKENS: int = 30
    # Per-token sleep — must be long enough that the disconnect arrives
    # before all tokens are drained, short enough that the test stays
    # snappy. 50 ms × 30 = 1.5 s budget.
    PER_TOKEN_SLEEP_S: float = 0.05

    def __init__(self) -> None:
        self.observed_cancel: asyncio.Event | None = None
        self.tokens_emitted: int = 0
        self.cancel_seen_set: bool = False
        self.run_started: asyncio.Event = asyncio.Event()

    def run(
        self,
        req: InternalChatRequest,
        cancel: asyncio.Event,
    ) -> AsyncIterator[Any]:
        # Capture the live ``cancel`` reference so the test can poll it
        # after triggering the disconnect.
        self.observed_cancel = cancel
        return self._aiter(cancel)

    async def _aiter(self, cancel: asyncio.Event) -> AsyncIterator[Any]:
        self.run_started.set()
        try:
            for i in range(self.SCRIPTED_TOKENS):
                # Mirror what _run_chat does: bail on cancel between frames.
                if cancel.is_set():
                    self.cancel_seen_set = True
                    return
                yield TokenDeltaEvent(text=f"tok{i} ")
                self.tokens_emitted += 1
                await asyncio.sleep(self.PER_TOKEN_SLEEP_S)
            # Only reached if never cancelled — the test should never hit
            # this in the disconnect path.
            yield DoneEvent(finish_reason="stop", usage=None)
        finally:
            # Final observation: the route's ``finally: cancel.set()``
            # should always make this true on real teardown.
            if cancel.is_set():
                self.cancel_seen_set = True


# ─── ASGI driver: speak the protocol directly ─────────────────────────────


def _make_app(service: Any) -> FastAPI:
    app = FastAPI()
    state = ChatState(service=service, model_redirect=ModelRedirect())
    app.include_router(router(state))
    return app


async def _drive_streaming_chat_then_disconnect(
    app: FastAPI,
    *,
    chunks_to_consume_before_disconnect: int = 1,
    overall_budget_s: float = 2.0,
) -> tuple[list[bytes], dict[str, Any]]:
    """Open a streaming POST against *app* via raw ASGI, consume a few
    chunks, then send ``http.disconnect`` and wait for the response coroutine
    to finish.

    Returns ``(chunks, summary)`` where:

    * ``chunks`` is the list of ``http.response.body`` bytes the server
      sent before the disconnect propagated.
    * ``summary`` is a small dict with ``{"status", "headers", "more_body_at_end"}``
      for downstream assertions.
    """
    body = (
        b'{"model":"test-model",'
        b'"messages":[{"role":"user","content":"hi"}],'
        b'"stream":true}'
    )
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    }

    # Request-body delivery + a controllable disconnect event.
    body_delivered = False
    disconnect_event = asyncio.Event()

    async def receive() -> dict[str, Any]:
        nonlocal body_delivered
        if not body_delivered:
            body_delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        # After the body, block until the test triggers disconnect.
        await disconnect_event.wait()
        return {"type": "http.disconnect"}

    chunks: list[bytes] = []
    summary: dict[str, Any] = {"status": None, "headers": None, "more_body_at_end": None}

    async def send(message: dict[str, Any]) -> None:
        mt = message.get("type")
        if mt == "http.response.start":
            summary["status"] = message["status"]
            summary["headers"] = message.get("headers", [])
        elif mt == "http.response.body":
            chunk = message.get("body", b"")
            if chunk:
                chunks.append(chunk)
            summary["more_body_at_end"] = message.get("more_body", False)

    async def _run_app() -> None:
        # FastAPI exposes itself as the ASGI3 callable directly.
        await app(scope, receive, send)

    app_task = asyncio.create_task(_run_app())

    # Wait for at least ``chunks_to_consume_before_disconnect`` SSE body
    # frames to arrive so the route is actively inside the
    # ``async for chunk in _sse_iter(...)`` loop. SSE chunks are flushed
    # individually by Starlette so each token == one body frame.
    deadline = asyncio.get_event_loop().time() + overall_budget_s
    while len(chunks) < chunks_to_consume_before_disconnect:
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(
                f"timed out waiting for {chunks_to_consume_before_disconnect} "
                f"SSE chunk(s); got {len(chunks)}"
            )
        await asyncio.sleep(0.01)

    # Trigger the simulated client-side disconnect.
    disconnect_event.set()

    # Wait for the app coroutine to finish unwinding (the route should
    # break out of the loop on the next ``is_disconnected`` poll, fall
    # through the ``finally: cancel.set()``, then emit ``data: [DONE]``).
    await asyncio.wait_for(app_task, timeout=overall_budget_s)

    return chunks, summary


# ─── The test ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_streaming_cancel_propagates_on_client_disconnect() -> None:
    """When the SSE client disconnects mid-stream, ``cancel`` must flip.

    The chat handler is responsible for keeping the user from being billed
    for tokens generated after they close the tab. The mechanism is:

        async for chunk in _sse_iter(...):
            if await request.is_disconnected():
                cancel.set()         # <-- this regression test guards
                break
            yield chunk

    A scripted service receives the live ``cancel`` event, watches it
    while yielding slow tokens, and stops cooperatively. We then assert:

    1. The scripted service saw ``cancel.is_set() == True`` — proves the
       Event reference the route owns is the same one downstream consumes.
    2. It emitted strictly fewer tokens than scripted — proves the route
       actually short-circuited; otherwise we'd see all 30.
    """
    service = _SlowScriptedChatService()
    app = _make_app(service)

    chunks, summary = await _drive_streaming_chat_then_disconnect(
        app,
        # Consume a couple of frames so we're definitely inside the
        # ``async for`` loop (one would be enough but two is more robust
        # against scheduling jitter).
        chunks_to_consume_before_disconnect=2,
        overall_budget_s=3.0,
    )

    # ---- 1. Direct observation: ``cancel.set()`` was flipped. -----------
    assert service.observed_cancel is not None, (
        "ChatService.run was never invoked — the route did not enter the "
        "streaming branch as expected"
    )
    assert service.observed_cancel.is_set(), (
        "client disconnected mid-stream but the route never flipped "
        "``cancel``; upstream provider work would keep billing"
    )

    # ---- 2. Structural: the route stopped pulling events. ---------------
    # The script would emit 30 tokens if never cancelled. We consumed 2,
    # then disconnected; even with a generous race window the service
    # must have stopped well short of the full 30.
    assert service.tokens_emitted < _SlowScriptedChatService.SCRIPTED_TOKENS, (
        f"scripted service emitted all {service.tokens_emitted} tokens — "
        f"cancel did not propagate to the producer (the loop drained the "
        f"full stream as if the client never disconnected)"
    )

    # Sanity: we actually streamed *something* before the disconnect.
    assert len(chunks) >= 2, (
        f"expected at least 2 streamed chunks before disconnect; got "
        f"{len(chunks)} — the disconnect may have fired before the route "
        f"started writing"
    )

    # Sanity: the response started with a 200 + SSE content-type before
    # we tore it down (anything else means the disconnect path isn't even
    # being exercised).
    assert summary["status"] == 200, summary
    content_type = next(
        (v for k, v in (summary["headers"] or []) if k.lower() == b"content-type"),
        b"",
    )
    assert b"text/event-stream" in content_type, (
        f"expected SSE content-type; got {content_type!r}"
    )
