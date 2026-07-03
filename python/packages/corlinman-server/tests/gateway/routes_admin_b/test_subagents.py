"""Tests for the W1.3 ``/admin/subagents*`` routes.

Covers the four endpoint shapes the live activity panel needs:

* ``GET /admin/subagents`` — active list (and the ``include_terminal``
  variant).
* ``GET /admin/subagents/{id}/status`` — single-row poll + 404.
* ``POST /admin/subagents/{id}/kill`` — happy path + 404 + 409.
* ``GET /admin/subagents/{id}/events`` — SSE smoke: stream terminates
  on terminal state.
* ``GET /admin/subagents/events/live`` — SSE smoke: snapshot frame on
  connect.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from corlinman_agent.events import EventEnvelope, SubagentSpawned
from corlinman_server.gateway.observability import LiveSubagentRegistry
from corlinman_server.gateway.routes_admin_b.infra import subagents as subagent_routes
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from corlinman_server.system.subagent import (
    AsyncSubagentDispatcher,
    SubagentRequest,
    SubagentTaskStore,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._admin_auth import authenticated_test_client, configure_admin_auth

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def admin_state(tmp_path: Path) -> Iterator[AdminState]:
    state = AdminState()
    configure_admin_auth(state)
    set_admin_state(state)
    try:
        yield state
    finally:
        set_admin_state(None)


@pytest.fixture()
def client(admin_state: AdminState) -> TestClient:
    app = FastAPI()
    app.include_router(subagent_routes.router())
    return authenticated_test_client(app)


def _wire_dispatcher(
    admin_state: AdminState, tmp_path: Path
) -> tuple[SubagentTaskStore, AsyncSubagentDispatcher]:
    """Build a real store + dispatcher with a no-op factory."""

    async def _factory(req: SubagentRequest) -> object:
        # Tests that need a terminal flip drive it via the store
        # directly rather than relying on this factory.
        return type(
            "FakeResult",
            (),
            {
                "output_text": "ok",
                "finish_reason": "stop",
                "elapsed_ms": 1,
                "child_session_key": "sess-A::child::0",
                "tool_calls_made": [],
                "error": None,
            },
        )()

    store = SubagentTaskStore(tmp_path / ".subagent-state.json")
    dispatcher = AsyncSubagentDispatcher(
        store=store, run_child_factory=_factory
    )
    admin_state.subagent_store = store
    admin_state.subagent_dispatcher = dispatcher
    return store, dispatcher


def _make_req(
    request_id: str = "req-1",
    parent_session_key: str = "sess-A",
    subagent_type: str = "researcher",
) -> SubagentRequest:
    return SubagentRequest(
        request_id=request_id,
        parent_session_key=parent_session_key,
        parent_agent_id="agent-parent",
        subagent_type=subagent_type,
        goal="figure stuff out",
        description="brief task",
        requested_at=int(time.time() * 1000),
        requested_by="admin",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _wire_inline_registry(
    admin_state: AdminState, child: str = "sess-A::child::0", depth: int = 1
) -> LiveSubagentRegistry:
    """Attach a live registry holding one running INLINE subagent row."""
    reg = LiveSubagentRegistry()
    reg.observe(
        EventEnvelope(
            turn_id="t1",
            session_key="sess-A",
            sequence=0,
            timestamp_ms=1000,
            event=SubagentSpawned(
                parent_session_key="sess-A",
                child_session_key=child,
                child_agent_id="inline-worker",
                depth=depth,
                prompt_preview="inline task",
            ),
        )
    )
    admin_state.live_subagent_registry = reg
    return reg


def test_list_returns_503_without_dispatcher(
    client: TestClient, admin_state: AdminState
) -> None:
    resp = client.get("/admin/subagents")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "subagent_dispatcher_unavailable"


@pytest.mark.asyncio
async def test_list_merges_inline_registry_rows(
    client: TestClient, admin_state: AdminState, tmp_path: Path
) -> None:
    """Inline subagents (live registry) appear in the overview alongside
    background-store rows, tagged with their source."""
    store, _ = _wire_dispatcher(admin_state, tmp_path)
    await store.begin(_make_req(request_id="bg-A"))
    _wire_inline_registry(admin_state, child="sess-A::child::0")

    resp = client.get("/admin/subagents")
    assert resp.status_code == 200, resp.text
    rows = {r["request_id"]: r for r in resp.json()["rows"]}
    assert "bg-A" in rows and "sess-A::child::0" in rows
    assert rows["bg-A"]["source"] == "background"
    assert rows["sess-A::child::0"]["source"] == "inline"
    assert rows["sess-A::child::0"]["depth"] == 1
    assert rows["sess-A::child::0"]["state"] == "running"


def test_list_registry_only_not_503(
    client: TestClient, admin_state: AdminState
) -> None:
    """With no background dispatcher but a live registry, the overview
    serves the inline rows instead of 503."""
    _wire_inline_registry(admin_state)
    resp = client.get("/admin/subagents")
    assert resp.status_code == 200, resp.text
    ids = {r["request_id"] for r in resp.json()["rows"]}
    assert ids == {"sess-A::child::0"}


@pytest.mark.asyncio
async def test_live_overview_includes_inline_rows(
    client: TestClient, admin_state: AdminState, tmp_path: Path
) -> None:
    """The global live SSE snapshot includes inline registry rows."""
    store, _ = _wire_dispatcher(admin_state, tmp_path)
    await store.begin(_make_req(request_id="bg-A"))
    _wire_inline_registry(admin_state, child="sess-A::child::0")

    r = subagent_routes.router()
    handler = next(
        route.endpoint  # type: ignore[attr-defined]
        for route in r.routes
        if getattr(route, "path", "") == "/admin/subagents/events/live"
    )
    streaming = await handler()
    chunks: list[bytes] = []
    async for chunk in streaming.body_iterator:
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        chunks.append(chunk)
        if b"".join(chunks).decode("utf-8").count("event: subagent") >= 2:
            break
    body = b"".join(chunks).decode("utf-8")
    assert "bg-A" in body and "sess-A::child::0" in body
    assert '"source": "inline"' in body
    if hasattr(streaming.body_iterator, "aclose"):
        await streaming.body_iterator.aclose()


@pytest.mark.asyncio
async def test_list_returns_only_active_by_default(
    client: TestClient, admin_state: AdminState, tmp_path: Path
) -> None:
    store, _ = _wire_dispatcher(admin_state, tmp_path)
    await store.begin(_make_req(request_id="A"))
    await store.begin(_make_req(request_id="B"))
    await store.update("B", state="succeeded")  # terminal

    resp = client.get("/admin/subagents")
    assert resp.status_code == 200, resp.text
    rows = resp.json()["rows"]
    ids = {r["request_id"] for r in rows}
    assert ids == {"A"}

    # With include_terminal=true we see both rows.
    resp_all = client.get("/admin/subagents?include_terminal=true")
    assert resp_all.status_code == 200
    ids_all = {r["request_id"] for r in resp_all.json()["rows"]}
    assert ids_all == {"A", "B"}


@pytest.mark.asyncio
async def test_status_404_on_unknown_id(
    client: TestClient, admin_state: AdminState, tmp_path: Path
) -> None:
    _wire_dispatcher(admin_state, tmp_path)
    resp = client.get("/admin/subagents/does-not-exist/status")
    assert resp.status_code == 404
    assert resp.json()["error"] == "subagent_request_not_found"


@pytest.mark.asyncio
async def test_status_returns_known_row(
    client: TestClient, admin_state: AdminState, tmp_path: Path
) -> None:
    store, _ = _wire_dispatcher(admin_state, tmp_path)
    await store.begin(_make_req(request_id="A", subagent_type="editor"))

    resp = client.get("/admin/subagents/A/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["request_id"] == "A"
    assert body["subagent_type"] == "editor"
    assert body["state"] == "queued"


def test_status_falls_back_to_registry_row(client: TestClient, admin_state: AdminState) -> None:
    """Registry-only inline rows are pollable via /status — the store
    missing them (here: unwired entirely) must not 404/503 when the live
    registry holds the row. Shape matches the list route's registry
    projection."""
    _wire_inline_registry(admin_state, child="sess-A::child::0")

    resp = client.get("/admin/subagents/sess-A::child::0/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["request_id"] == "sess-A::child::0"
    assert body["source"] == "inline"
    assert body["state"] == "running"
    assert body["depth"] == 1
    assert body["subagent_type"] == "inline-worker"


@pytest.mark.asyncio
async def test_status_store_miss_falls_back_to_registry(
    client: TestClient, admin_state: AdminState, tmp_path: Path
) -> None:
    """With the store wired but missing the id, the registry row answers."""
    _wire_dispatcher(admin_state, tmp_path)
    _wire_inline_registry(admin_state, child="sess-A::child::0")

    resp = client.get("/admin/subagents/sess-A::child::0/status")
    assert resp.status_code == 200, resp.text
    assert resp.json()["source"] == "inline"


@pytest.mark.asyncio
async def test_status_prefers_store_row_on_collision(
    client: TestClient, admin_state: AdminState, tmp_path: Path
) -> None:
    """A durable store hit still wins over a same-id registry row."""
    store, _ = _wire_dispatcher(admin_state, tmp_path)
    await store.begin(_make_req(request_id="sess-A::child::0"))
    _wire_inline_registry(admin_state, child="sess-A::child::0")

    resp = client.get("/admin/subagents/sess-A::child::0/status")
    assert resp.status_code == 200, resp.text
    assert resp.json()["source"] == "background"


@pytest.mark.asyncio
async def test_kill_unknown_returns_404(
    client: TestClient, admin_state: AdminState, tmp_path: Path
) -> None:
    _wire_dispatcher(admin_state, tmp_path)
    resp = client.post("/admin/subagents/missing/kill")
    assert resp.status_code == 404
    assert resp.json()["error"] == "subagent_request_not_found"


@pytest.mark.asyncio
async def test_kill_terminal_returns_409(
    client: TestClient, admin_state: AdminState, tmp_path: Path
) -> None:
    store, _ = _wire_dispatcher(admin_state, tmp_path)
    await store.begin(_make_req(request_id="A"))
    await store.update("A", state="succeeded")

    resp = client.post("/admin/subagents/A/kill")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "subagent_already_terminal"
    assert body["state"] == "succeeded"


@pytest.mark.asyncio
async def test_kill_in_flight_flips_state(
    client: TestClient, admin_state: AdminState, tmp_path: Path
) -> None:
    store, _ = _wire_dispatcher(admin_state, tmp_path)
    await store.begin(_make_req(request_id="A"))
    await store.update("A", state="running")

    resp = client.post("/admin/subagents/A/kill")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "killed"
    # The finish_reason carries the actor — TestClient's HTTP-Basic
    # auth establishes ``admin`` as the user.
    assert body["finish_reason"] == "killed_by:admin"


@pytest.mark.asyncio
async def test_per_request_events_terminates_on_terminal_state(
    client: TestClient, admin_state: AdminState, tmp_path: Path
) -> None:
    store, _ = _wire_dispatcher(admin_state, tmp_path)
    # Pre-terminal row → the SSE generator emits the initial frame and
    # closes immediately.
    await store.begin(_make_req(request_id="DONE"))
    await store.update("DONE", state="succeeded")

    with client.stream(
        "GET", "/admin/subagents/DONE/events"
    ) as response:
        assert response.status_code == 200
        body = response.read().decode("utf-8")

    # We get exactly one ``event: status`` frame and then EOF.
    assert "event: status" in body
    assert '"state": "succeeded"' in body or "succeeded" in body


@pytest.mark.asyncio
async def test_per_request_events_404_on_unknown(
    client: TestClient, admin_state: AdminState, tmp_path: Path
) -> None:
    _wire_dispatcher(admin_state, tmp_path)
    resp = client.get("/admin/subagents/missing/events")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_live_overview_emits_initial_snapshot(
    client: TestClient, admin_state: AdminState, tmp_path: Path
) -> None:
    """Drive the live-overview generator manually so we observe the
    initial snapshot frames without engaging fastapi's TestClient SSE
    blocking path (the generator otherwise sleeps on the heartbeat and
    a sync TestClient stream-read keeps the test alive for 10s+)."""
    store, _ = _wire_dispatcher(admin_state, tmp_path)
    await store.begin(_make_req(request_id="A"))
    await store.begin(_make_req(request_id="B"))

    # Direct route call — async generator returns a StreamingResponse,
    # whose ``body_iterator`` we can pump for just the snapshot frames.
    from starlette.requests import Request as StarletteRequest  # noqa: PLC0415

    # Build a minimal ASGI scope so the dependency-resolver doesn't
    # crash on missing ``request`` attributes.
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/admin/subagents/events/live",
        "headers": [],
        "query_string": b"",
    }

    # The route handler is the second item in the APIRouter we built.
    r = subagent_routes.router()
    handler = None
    for route in r.routes:
        if (
            getattr(route, "path", "") == "/admin/subagents/events/live"
        ):
            handler = route.endpoint  # type: ignore[attr-defined]
            break
    assert handler is not None
    streaming = await handler()
    chunks: list[bytes] = []
    async for chunk in streaming.body_iterator:
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        chunks.append(chunk)
        joined = b"".join(chunks).decode("utf-8")
        # Two snapshot frames + we don't want to wait for the heartbeat
        # so break the moment we see them both.
        if joined.count("event: subagent") >= 2:
            break

    body = b"".join(chunks).decode("utf-8")
    assert "event: subagent" in body
    assert "A" in body and "B" in body

    # Avoid leaking the async generator: close explicitly.
    if hasattr(streaming.body_iterator, "aclose"):
        await streaming.body_iterator.aclose()
    _ = StarletteRequest  # silence unused-import for linters
    _ = scope


# ---------------------------------------------------------------------------
# adaptive overview poll cadence (#108 item 4)
# ---------------------------------------------------------------------------


def test_next_poll_interval_backs_off_when_unchanged_up_to_cap() -> None:
    """A run of unchanged ticks grows the interval by ×1.5 up to the 10s
    cap and never past it."""
    base = subagent_routes._SUBAGENT_OVERVIEW_POLL_BASE_SECONDS
    cap = subagent_routes._SUBAGENT_OVERVIEW_POLL_MAX_SECONDS
    assert base == 2.0
    assert cap == 10.0

    interval = base
    seen = [interval]
    for _ in range(20):
        interval = subagent_routes._next_poll_interval(False, interval)
        seen.append(interval)
        # Monotonically non-decreasing and never above the cap.
        assert interval <= cap
        assert interval >= seen[-2]
    # 2 → 3 → 4.5 → 6.75 → 10 (capped) …
    assert seen[1] == pytest.approx(3.0)
    assert seen[2] == pytest.approx(4.5)
    assert seen[-1] == pytest.approx(cap)


def test_next_poll_interval_resets_to_base_on_change() -> None:
    """Any change snaps the interval straight back to the base for liveness,
    regardless of how far it had backed off."""
    base = subagent_routes._SUBAGENT_OVERVIEW_POLL_BASE_SECONDS
    cap = subagent_routes._SUBAGENT_OVERVIEW_POLL_MAX_SECONDS
    assert subagent_routes._next_poll_interval(True, cap) == base
    assert subagent_routes._next_poll_interval(True, base) == base
    assert subagent_routes._next_poll_interval(True, 4.5) == base
