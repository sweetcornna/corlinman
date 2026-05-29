"""Route-level tests for ``gateway/routes/canvas.py`` (TEST-007).

The only pre-existing canvas coverage was the ``_new_session_id`` entropy
unit test (R2-005) plus the R2-001 *alias-gate* test in
``test_chat_requires_auth.py`` (``GET /canvas/session/{id}/events`` must
401 unauthenticated). Nothing exercised the actual subscribe + frame-push
*surface*. This file fills that gap with three angles:

* **auth required (R2-001)** — the canonical ``/v1/canvas/*`` paths must
  401 without a tenant API key, driven through the production
  :func:`build_app` boot path so a regression that un-gates the prefix
  fails loudly.
* **happy path** — a real session create → SSE subscribe → frame push →
  the pushed event is delivered on the subscriber's stream. Driven
  through the *real* route endpoint coroutines (no mocks, no private
  ``_Session`` poking) because the canvas SSE generator is an infinite
  stream that deadlocks Starlette's single-threaded ``TestClient`` portal.
* **not-found** — pushing a frame to / subscribing to an unknown session
  id must 404 ``session_not_found``.

The ``CanvasState`` is constructed with ``renderer=None`` so the tests
stay independent of the heavy ``corlinman_canvas.Renderer`` and exercise
the route's own session bookkeeping + fan-out, which is what has no
coverage today.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")

from corlinman_server.gateway.lifecycle.entrypoint import build_app  # noqa: E402
from corlinman_server.gateway.routes.canvas import (  # noqa: E402
    CanvasState,
    router,
)
from fastapi.testclient import TestClient  # noqa: E402
from starlette.requests import Request  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_request(method: str, body: Any | None = None) -> Request:
    """Build a minimal Starlette ``Request`` carrying a JSON body.

    Used to drive the real route endpoint coroutines directly — the
    canvas SSE handler returns an endless ``StreamingResponse`` which the
    blocking ``TestClient`` portal cannot consume without deadlock.
    """
    payload = b"" if body is None else json.dumps(body).encode()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": payload, "more_body": False}

    scope = {
        "type": "http",
        "method": method,
        "headers": [(b"content-type", b"application/json")],
        "path": "/x",
        "query_string": b"",
    }
    return Request(scope, receive)


def _endpoints(state: CanvasState) -> dict[tuple[tuple[str, ...], str], Any]:
    """Map ``(sorted-methods, path) -> endpoint coroutine`` for the router."""
    api = router(state)
    return {
        (tuple(sorted(r.methods)), r.path): r.endpoint  # type: ignore[attr-defined]
        for r in api.routes
    }


# ---------------------------------------------------------------------------
# auth required (R2-001) — through the production build_app boot path
# ---------------------------------------------------------------------------


def test_canonical_canvas_render_requires_auth(tmp_path: Path) -> None:
    """``POST /v1/canvas/render`` must 401 without a tenant API key."""
    app = build_app(config_path=None, data_dir=tmp_path)
    with TestClient(app) as client:
        resp = client.post("/v1/canvas/render", json={"kind": "noop"})
    assert resp.status_code == 401, resp.text
    assert resp.json().get("error") == "unauthorized"


def test_canonical_canvas_session_events_requires_auth(tmp_path: Path) -> None:
    """``GET /v1/canvas/session/{id}/events`` is the SSE exfil channel;
    it must 401 without a tenant API key."""
    app = build_app(config_path=None, data_dir=tmp_path)
    with TestClient(app) as client:
        resp = client.get("/v1/canvas/session/cs_anything/events")
    assert resp.status_code == 401, resp.text
    assert resp.json().get("error") == "unauthorized"


# ---------------------------------------------------------------------------
# happy path — create → subscribe → push → delivered
# ---------------------------------------------------------------------------


async def test_subscribe_then_frame_push_is_delivered_on_stream() -> None:
    """The full canvas loop: create a session, open the SSE subscribe
    stream, push a frame, and assert the pushed event arrives on the
    subscriber's stream — exercising the real ``_create_session``,
    ``_stream_events`` fan-out and ``_post_frame`` handlers."""
    state = CanvasState(enabled=True, renderer=None)
    ep = _endpoints(state)
    create = ep[(("POST",), "/v1/canvas/session")]
    frame = ep[(("POST",), "/v1/canvas/frame")]
    stream = ep[(("GET",), "/v1/canvas/session/{id}/events")]

    create_resp = await create(_json_request("POST", {"title": "demo"}))
    assert create_resp.status_code == 201, bytes(create_resp.body)
    created = json.loads(bytes(create_resp.body))
    session_id = created["session_id"]
    assert session_id.startswith("cs_")

    stream_resp = await stream(session_id)
    assert stream_resp.media_type == "text/event-stream"

    async def _consume() -> list[bytes]:
        out: list[bytes] = []
        async for chunk in stream_resp.body_iterator:
            out.append(chunk if isinstance(chunk, bytes) else chunk.encode())
            if b"event: canvas" in out[-1]:
                return out
        return out

    consumer = asyncio.create_task(_consume())
    # Let the generator register its subscriber queue before we push.
    await asyncio.sleep(0.1)

    frame_resp = await frame(
        _json_request(
            "POST",
            {"session_id": session_id, "kind": "navigate", "payload": {"url": "/x"}},
        )
    )
    assert frame_resp.status_code == 202, bytes(frame_resp.body)
    event_id = json.loads(bytes(frame_resp.body))["event_id"]

    chunks = await asyncio.wait_for(consumer, timeout=5.0)
    delivered = b"".join(chunks)
    assert b"event: canvas" in delivered
    # The delivered SSE frame carries the same event id the push returned.
    assert event_id.encode() in delivered
    assert b'"kind": "navigate"' in delivered


def test_create_session_via_testclient_returns_201() -> None:
    """The synchronous create-session path works through the real
    ``TestClient`` too (no streaming involved), proving the JSON contract."""
    state = CanvasState(enabled=True, renderer=None)
    app = fastapi.FastAPI()
    app.include_router(router(state))
    with TestClient(app) as client:
        resp = client.post("/v1/canvas/session", json={"title": "t", "ttl_secs": 30})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["session_id"].startswith("cs_")
    assert body["expires_at_ms"] > body["created_at_ms"]


# ---------------------------------------------------------------------------
# not-found / invalid-session
# ---------------------------------------------------------------------------


def test_frame_push_to_unknown_session_returns_404() -> None:
    state = CanvasState(enabled=True, renderer=None)
    app = fastapi.FastAPI()
    app.include_router(router(state))
    with TestClient(app) as client:
        resp = client.post(
            "/v1/canvas/frame",
            json={"session_id": "cs_nope", "kind": "navigate", "payload": {}},
        )
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["error"] == "session_not_found"
    assert body["session_id"] == "cs_nope"


def test_subscribe_unknown_session_returns_404() -> None:
    state = CanvasState(enabled=True, renderer=None)
    app = fastapi.FastAPI()
    app.include_router(router(state))
    with TestClient(app) as client:
        resp = client.get("/v1/canvas/session/cs_nope/events")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"] == "session_not_found"


def test_frame_push_with_invalid_kind_returns_400() -> None:
    """A frame whose ``kind`` is outside ``ALLOWED_FRAME_KINDS`` must be
    rejected before any session lookup — 400 ``invalid_frame_kind``."""
    state = CanvasState(enabled=True, renderer=None)
    app = fastapi.FastAPI()
    app.include_router(router(state))
    with TestClient(app) as client:
        resp = client.post(
            "/v1/canvas/frame",
            json={"session_id": "cs_x", "kind": "definitely-not-a-kind", "payload": {}},
        )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "invalid_frame_kind"
