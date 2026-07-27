"""``/admin/approvals*`` — W3-4 wiring tests.

Pins the four W3-4 behaviours on the admin surface:

* the list route renders the durable store (wire decision vocabulary
  ``approved`` / ``denied`` / ``timeout`` + ``decision_reason``);
* ``POST .../decide`` persists the decision AND the reason;
* the decide route bridges into the W3-3 ApprovalBroker so an in-flight
  turn receives the ``ApprovalDecision`` client frame (end-to-end);
* after a "restart" (no live stream registered) the decision still
  persists and the route succeeds.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterator
from typing import Any

import pytest
from corlinman_providers.plugins import (
    ApprovalDecision,
    ApprovalQueue,
    ApprovalRequest,
    ApprovalStore,
)
from corlinman_server.gateway.routes_admin_a import (
    AdminState,
    build_router,
    set_admin_state,
)
from corlinman_server.gateway.routes_admin_a._session_store import (
    AdminSessionStore,
)
from corlinman_server.gateway.routes_admin_a.auth import hash_password
from corlinman_server.gateway.services.approval_broker import (
    PendingApproval,
    get_approval_broker,
    reset_approval_broker,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _basic_auth_header(username: str = "admin", password: str = "rootroot") -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


def _request(call_id: str = "call_w34") -> ApprovalRequest:
    return ApprovalRequest(
        call_id=call_id,
        plugin="github",
        tool="create_issue",
        args_preview='{"title": "hi"}',
        session_key="acme::s1",
        reason="permission rule requires approval",
    )


@pytest.fixture()
def store() -> ApprovalStore:
    return ApprovalStore(":memory:")


@pytest.fixture()
def client(store: ApprovalStore) -> Iterator[TestClient]:
    reset_approval_broker()
    state = AdminState(
        approval_store=store,
        approval_queue=ApprovalQueue(store=store),
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
    )
    set_admin_state(state)
    app = FastAPI()
    app.include_router(build_router())
    with TestClient(app, headers={"Authorization": _basic_auth_header()}) as c:
        yield c
    set_admin_state(None)
    reset_approval_broker()


def test_list_renders_wire_decisions(client: TestClient, store: ApprovalStore) -> None:
    async def _seed() -> None:
        await store.insert(_request("call_p"))
        await store.insert(_request("call_a"))
        await store.insert(_request("call_t"))
        await store.decide("call_a", ApprovalDecision.ALLOW)
        await store.decide(
            "call_t", ApprovalDecision.TIMEOUT, reason="stream ended"
        )

    asyncio.run(_seed())

    resp = client.get("/admin/approvals?include_decided=true")
    assert resp.status_code == 200
    rows = {r["call_id"]: r for r in resp.json()}
    assert rows["call_p"]["decision"] is None
    assert rows["call_a"]["decision"] == "approved"
    assert rows["call_t"]["decision"] == "timeout"
    assert rows["call_t"]["decision_reason"] == "stream ended"
    # The ApprovalOut contract fields the UI's Approval type maps onto.
    assert {"call_id", "args_preview", "created_at", "decision_reason"} <= set(
        rows["call_p"]
    )


def test_decide_persists_reason_and_bridges_to_broker(
    client: TestClient, store: ApprovalStore
) -> None:
    """Admin approve → row decided (+reason) AND the parked stream's tx
    queue receives the ApprovalDecision client frame (W3-4 AC1)."""
    asyncio.run(store.insert(_request("call_live")))

    tx: asyncio.Queue[Any] = asyncio.Queue()
    get_approval_broker().register(
        PendingApproval(call_id="call_live", tx=tx, session_key="acme::s1")
    )

    resp = client.post(
        "/admin/approvals/call_live/decide",
        json={"approve": True, "reason": "looks safe"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"id": "call_live", "decision": "approved"}

    rec = asyncio.run(store.get("call_live"))
    assert rec is not None
    assert rec.decision is ApprovalDecision.ALLOW
    assert rec.decision_reason == "looks safe"

    frame = tx.get_nowait()
    assert frame.WhichOneof("kind") == "approval"
    assert frame.approval.call_id == "call_live"
    assert frame.approval.approved is True


def test_decide_without_live_stream_marks_expired(
    client: TestClient, store: ApprovalStore
) -> None:
    """W3-4 review fix: a pending row with NO broker entry (its stream is
    gone) must not be recorded as a successful approve/deny nothing will
    ever execute — the route answers 410 and sweeps the row to timeout.
    (Boot-time reconcile_orphaned handles the restart bulk; this covers
    the in-process stream-just-ended race.)"""
    asyncio.run(store.insert(_request("call_stale")))

    resp = client.post(
        "/admin/approvals/call_stale/decide",
        json={"approve": False, "reason": "too late anyway"},
    )
    assert resp.status_code == 410
    assert resp.json()["detail"]["error"] == "approval_expired"
    rec = asyncio.run(store.get("call_stale"))
    assert rec is not None
    assert rec.decision is ApprovalDecision.TIMEOUT
    assert "no live stream" in (rec.decision_reason or "")


def test_decide_unknown_call_id_is_404(client: TestClient) -> None:
    resp = client.post(
        "/admin/approvals/call_missing/decide", json={"approve": True}
    )
    assert resp.status_code == 404
