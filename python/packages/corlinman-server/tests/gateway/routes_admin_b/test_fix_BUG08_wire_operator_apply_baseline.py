"""BUG-08-wire repro + fix: the operator ``/admin/evolution/{id}/apply``
route must thread the configured ``[evolution.auto_rollback]`` config
into the :class:`EvolutionApplier` so the captured ``metrics_baseline``
uses the *operator-configured* signal window — not the conservative
default the bare constructor falls back to.

Root cause: the route built ``EvolutionApplier(_resolve_connection(store))``
with no config, so the apply-time baseline was captured over the default
1800s window regardless of the operator's ``signal_window_secs``. The
AutoRollback monitor re-samples over the *configured* window, so a
non-default config produced an asymmetric window → false breaches (the
exact failure mode the applier docstring warns about).

The repro sets a non-default ``signal_window_secs`` (4242) on the admin
config snapshot, applies a proposal through the operator route, then reads
the persisted ``evolution_history.metrics_baseline`` back. Before the fix
the window is 1800 (default); after the fix it is 4242 (configured).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from corlinman_auto_rollback.metrics import MetricSnapshot
from corlinman_evolution_store import (
    EvolutionKind,
    EvolutionProposal,
    EvolutionRisk,
    EvolutionStatus,
    EvolutionStore,
    HistoryRepo,
    ProposalId,
    ProposalsRepo,
)
from corlinman_server.gateway.routes_admin_b import evolution as evolution_routes
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._admin_auth import authenticated_test_client, configure_admin_auth

CONFIGURED_WINDOW_SECS = 4_242


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[EvolutionStore]:
    s = await EvolutionStore.open(tmp_path / "evolution-bug08-wire.sqlite")
    try:
        yield s
    finally:
        await s.close()


def _config_loader() -> dict:
    """Mirror the on-disk corlinman.toml shape the route should read:
    ``[evolution.auto_rollback].thresholds.signal_window_secs``."""
    return {
        "evolution": {
            "auto_rollback": {
                "enabled": True,
                "grace_window_hours": 72,
                "thresholds": {
                    "default_err_rate_delta_pct": 50.0,
                    "default_p95_latency_delta_pct": 25.0,
                    "signal_window_secs": CONFIGURED_WINDOW_SECS,
                    "min_baseline_signals": 5,
                },
            }
        }
    }


@pytest_asyncio.fixture
async def client(store: EvolutionStore) -> AsyncIterator[TestClient]:
    state = AdminState(evolution_store=store, config_loader=_config_loader)
    configure_admin_auth(state)
    set_admin_state(state)
    try:
        app = FastAPI()
        app.include_router(evolution_routes.router())
        yield authenticated_test_client(app)
    finally:
        set_admin_state(None)


async def _seed_approved(store: EvolutionStore, *, proposal_id: str) -> None:
    await ProposalsRepo(store.conn).insert(
        EvolutionProposal(
            id=ProposalId(proposal_id),
            kind=EvolutionKind.MEMORY_OP,
            target="merge_chunks:1,2",
            diff='{"after": "merged"}',
            reasoning="seeded by BUG-08-wire test",
            risk=EvolutionRisk.LOW,
            budget_cost=1,
            status=EvolutionStatus.APPROVED,
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


@pytest.mark.asyncio
async def test_operator_apply_threads_configured_signal_window(
    client: TestClient, store: EvolutionStore
) -> None:
    """The operator apply route captures a baseline over the configured
    signal window, not the bare-constructor default of 1800s."""
    await _seed_approved(store, proposal_id="bug08-wire-001")

    resp = client.post("/admin/evolution/bug08-wire-001/apply")
    assert resp.status_code == 200, resp.text

    row = await HistoryRepo(store.conn).latest_for_proposal(
        ProposalId("bug08-wire-001")
    )
    snapshot = MetricSnapshot.from_dict(row.metrics_baseline)
    # Baseline must be a real, monitor-decodable snapshot (never {}).
    assert snapshot.target == "merge_chunks:1,2"
    assert set(snapshot.counts) == {"tool.call.failed", "search.recall.dropped"}
    # The load-bearing assertion: the window matches the operator config,
    # so the monitor re-samples symmetrically. Before the fix this was
    # the default 1800.
    assert snapshot.window_secs == CONFIGURED_WINDOW_SECS
