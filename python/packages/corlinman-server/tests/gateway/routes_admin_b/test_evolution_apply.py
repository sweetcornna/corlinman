"""Tests for ``POST /admin/evolution/{id}/apply`` + ``/rollback``.

Covers the P6 gap-closure that replaced the 501 ``applier_not_wired``
stubs with the real :class:`corlinman_auto_rollback.EvolutionApplier`
wiring:

* apply an ``approved`` proposal → 200 ``applied`` + history row;
* rollback the applied proposal → 200 ``rolled_back``;
* error envelopes — unknown id (404), double-apply / apply-of-pending
  (409 ``invalid_state_transition``), rollback-without-apply (409),
  rollback with no history (410 ``history_missing``);
* the ``evolution_disabled`` 503 path stays intact when no store is
  wired.

Each test mounts just the evolution router with a real
:class:`EvolutionStore` so the applier exercises the live schema.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from corlinman_evolution_store import (
    EvolutionKind,
    EvolutionProposal,
    EvolutionRisk,
    EvolutionStatus,
    EvolutionStore,
    ProposalId,
    ProposalsRepo,
)
from corlinman_server.gateway.routes_admin_b.infra import evolution as evolution_routes
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._admin_auth import authenticated_test_client, configure_admin_auth

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[EvolutionStore]:
    s = await EvolutionStore.open(tmp_path / "evolution-apply-routes.sqlite")
    try:
        yield s
    finally:
        await s.close()


@pytest_asyncio.fixture
async def client(store: EvolutionStore) -> AsyncIterator[TestClient]:
    """Mount the evolution router with a fully-wired admin state."""
    state = AdminState(evolution_store=store)
    configure_admin_auth(state)
    set_admin_state(state)
    try:
        app = FastAPI()
        app.include_router(evolution_routes.router())
        yield authenticated_test_client(app)
    finally:
        set_admin_state(None)


async def _seed(
    store: EvolutionStore,
    *,
    proposal_id: str,
    status: EvolutionStatus,
    kind: EvolutionKind = EvolutionKind.MEMORY_OP,
) -> None:
    await ProposalsRepo(store.conn).insert(
        EvolutionProposal(
            id=ProposalId(proposal_id),
            kind=kind,
            target="merge_chunks:1,2",
            diff='{"after": "merged"}',
            reasoning="seeded by test",
            risk=EvolutionRisk.LOW,
            budget_cost=1,
            status=status,
            shadow_metrics=None,
            signal_ids=[],
            trace_ids=[],
            created_at=1_000,
            decided_at=2_000,
            decided_by="operator",
            applied_at=None,
            rollback_of=None,
            eval_run_id=None,
            baseline_metrics_json=None,
            auto_rollback_at=None,
            auto_rollback_reason=None,
            metadata=None,
        )
    )


# ---------------------------------------------------------------------------
# apply — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_approved_proposal_returns_200(
    client: TestClient, store: EvolutionStore
) -> None:
    await _seed(store, proposal_id="evol-r-001", status=EvolutionStatus.APPROVED)

    resp = client.post("/admin/evolution/evol-r-001/apply")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == "evol-r-001"
    assert body["status"] == "applied"
    assert body["history_id"] is not None

    # The proposal is now `applied` — the GET detail route reflects it.
    detail = client.get("/admin/evolution/evol-r-001")
    assert detail.status_code == 200
    assert detail.json()["status"] == "applied"


# ---------------------------------------------------------------------------
# rollback — happy path (end-to-end apply then rollback)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_then_rollback_round_trips(
    client: TestClient, store: EvolutionStore
) -> None:
    await _seed(store, proposal_id="evol-r-002", status=EvolutionStatus.APPROVED)

    apply = client.post("/admin/evolution/evol-r-002/apply")
    assert apply.status_code == 200, apply.text

    rollback = client.post(
        "/admin/evolution/evol-r-002/rollback",
        json={"reason": "operator: metrics regressed"},
    )
    assert rollback.status_code == 200, rollback.text
    body = rollback.json()
    assert body["id"] == "evol-r-002"
    assert body["status"] == "rolled_back"
    assert body["reason"] == "operator: metrics regressed"

    # Revert restores the pre-apply status (`approved`).
    detail = client.get("/admin/evolution/evol-r-002").json()
    assert detail["status"] == "approved"
    assert detail["auto_rollback_reason"] == "operator: metrics regressed"


@pytest.mark.asyncio
async def test_rollback_without_body_uses_default_reason(
    client: TestClient, store: EvolutionStore
) -> None:
    """A rollback POST with no body still records an audit reason."""
    await _seed(store, proposal_id="evol-r-003", status=EvolutionStatus.APPROVED)
    client.post("/admin/evolution/evol-r-003/apply")

    resp = client.post("/admin/evolution/evol-r-003/rollback")
    assert resp.status_code == 200, resp.text
    assert resp.json()["reason"] == "operator: unknown"


# ---------------------------------------------------------------------------
# apply — error paths
# ---------------------------------------------------------------------------


def test_apply_unknown_id_returns_404(client: TestClient) -> None:
    resp = client.post("/admin/evolution/evol-ghost/apply")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "not_found"
    assert body["id"] == "evol-ghost"


@pytest.mark.asyncio
async def test_apply_pending_proposal_returns_409(
    client: TestClient, store: EvolutionStore
) -> None:
    """A ``pending`` proposal can't be applied — 409 with the
    ``invalid_state_transition`` envelope the approve route also uses."""
    await _seed(store, proposal_id="evol-r-004", status=EvolutionStatus.PENDING)

    resp = client.post("/admin/evolution/evol-r-004/apply")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "invalid_state_transition"
    assert body["from"] == "pending"
    assert body["to"] == "applied"


@pytest.mark.asyncio
async def test_double_apply_returns_409(
    client: TestClient, store: EvolutionStore
) -> None:
    """A second apply sees ``status == applied`` and is rejected."""
    await _seed(store, proposal_id="evol-r-005", status=EvolutionStatus.APPROVED)

    first = client.post("/admin/evolution/evol-r-005/apply")
    assert first.status_code == 200

    second = client.post("/admin/evolution/evol-r-005/apply")
    assert second.status_code == 409
    body = second.json()
    assert body["error"] == "invalid_state_transition"
    assert body["from"] == "applied"
    assert body["to"] == "applied"


# ---------------------------------------------------------------------------
# rollback — error paths
# ---------------------------------------------------------------------------


def test_rollback_unknown_id_returns_404(client: TestClient) -> None:
    resp = client.post("/admin/evolution/evol-ghost/rollback")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


@pytest.mark.asyncio
async def test_rollback_without_apply_returns_409(
    client: TestClient, store: EvolutionStore
) -> None:
    """An ``approved`` (never-applied) proposal can't be rolled back."""
    await _seed(store, proposal_id="evol-r-006", status=EvolutionStatus.APPROVED)

    resp = client.post("/admin/evolution/evol-r-006/rollback")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "invalid_state_transition"
    assert body["from"] == "approved"
    assert body["to"] == "rolled_back"


@pytest.mark.asyncio
async def test_double_rollback_returns_409(
    client: TestClient, store: EvolutionStore
) -> None:
    """A second rollback sees the proposal back at ``approved`` — not
    idempotent, mirrors the Rust contract."""
    await _seed(store, proposal_id="evol-r-007", status=EvolutionStatus.APPROVED)
    client.post("/admin/evolution/evol-r-007/apply")

    first = client.post("/admin/evolution/evol-r-007/rollback")
    assert first.status_code == 200

    second = client.post("/admin/evolution/evol-r-007/rollback")
    assert second.status_code == 409
    assert second.json()["error"] == "invalid_state_transition"


@pytest.mark.asyncio
async def test_rollback_with_missing_history_returns_410(
    client: TestClient, store: EvolutionStore
) -> None:
    """An ``applied`` proposal with no history row → 410 history_missing.

    The proposal is moved straight to ``applied`` via the repo (not the
    apply route) so no audit row exists — the same data-corruption
    signal the Rust route maps to 410 Gone.
    """
    await _seed(store, proposal_id="evol-r-008", status=EvolutionStatus.APPROVED)
    await ProposalsRepo(store.conn).mark_applied(ProposalId("evol-r-008"), 5_000)

    resp = client.post("/admin/evolution/evol-r-008/rollback")
    assert resp.status_code == 410
    body = resp.json()
    assert body["error"] == "history_missing"
    assert body["proposal_id"] == "evol-r-008"


# ---------------------------------------------------------------------------
# disabled mode — store not wired
# ---------------------------------------------------------------------------


def test_apply_evolution_disabled_returns_503() -> None:
    """No ``evolution_store`` → 503 ``evolution_disabled`` (unchanged)."""
    state = AdminState()
    configure_admin_auth(state)
    set_admin_state(state)
    try:
        app = FastAPI()
        app.include_router(evolution_routes.router())
        with authenticated_test_client(app) as c:
            apply = c.post("/admin/evolution/evol-x/apply")
            assert apply.status_code == 503
            assert apply.json()["error"] == "evolution_disabled"

            rollback = c.post("/admin/evolution/evol-x/rollback")
            assert rollback.status_code == 503
            assert rollback.json()["error"] == "evolution_disabled"
    finally:
        set_admin_state(None)
