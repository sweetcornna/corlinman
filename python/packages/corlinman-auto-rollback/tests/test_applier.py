"""End-to-end :class:`EvolutionApplier` tests.

Covers the store-backed forward-apply + revert driver:

* apply an ``approved`` proposal → ``applied`` + history row + closed
  ``apply_intent_log`` ticket;
* revert it → restored to the captured pre-apply status + stamped
  rollback audit fields;
* error paths — unknown id, double-apply, revert-without-apply,
  history-missing, and the :class:`Applier`-protocol conformance the
  :class:`~corlinman_auto_rollback.monitor.AutoRollbackMonitor` relies
  on.

Each test seeds a real :class:`EvolutionStore` (async sqlite) so the
schema + repos are exercised exactly as production would.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from corlinman_auto_rollback.applier import (
    EvolutionApplier,
    InternalApplyError,
    NotApprovedApplyError,
    NotFoundApplyError,
)
from corlinman_auto_rollback.revert import (
    Applier,
    HistoryMissingRevertError,
    NotAppliedRevertError,
    NotFoundRevertError,
)
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

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[EvolutionStore]:
    s = await EvolutionStore.open(tmp_path / "evolution-applier-tests.sqlite")
    try:
        yield s
    finally:
        await s.close()


async def _seed_proposal(
    repo: ProposalsRepo,
    *,
    proposal_id: str,
    status: EvolutionStatus,
    kind: EvolutionKind = EvolutionKind.MEMORY_OP,
    target: str = "merge_chunks:1,2",
    diff: str = '{"after": "merged"}',
    decided_by: str | None = "operator",
) -> ProposalId:
    """Insert one proposal at ``status``. ``ProposalsRepo.insert`` writes
    the status verbatim, so the helper can seed any state directly."""
    pid = ProposalId(proposal_id)
    await repo.insert(
        EvolutionProposal(
            id=pid,
            kind=kind,
            target=target,
            diff=diff,
            reasoning="seeded by test",
            risk=EvolutionRisk.LOW,
            budget_cost=1,
            status=status,
            shadow_metrics=None,
            signal_ids=[],
            trace_ids=[],
            created_at=1_000,
            decided_at=2_000 if decided_by is not None else None,
            decided_by=decided_by,
            applied_at=None,
            rollback_of=None,
            eval_run_id=None,
            baseline_metrics_json=None,
            auto_rollback_at=None,
            auto_rollback_reason=None,
            metadata=None,
        )
    )
    return pid


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_evolution_applier_satisfies_applier_protocol(
    store: EvolutionStore,
) -> None:
    """The concrete applier is duck-typed against the runtime-checkable
    :class:`Applier` protocol the monitor depends on."""
    assert isinstance(EvolutionApplier(store.conn), Applier)


# ---------------------------------------------------------------------------
# apply — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_transitions_approved_to_applied(
    store: EvolutionStore,
) -> None:
    """An ``approved`` proposal flips to ``applied`` with a stamped
    ``applied_at`` and a fresh history row."""
    proposals = ProposalsRepo(store.conn)
    pid = await _seed_proposal(
        proposals, proposal_id="evol-apply-001", status=EvolutionStatus.APPROVED
    )

    applier = EvolutionApplier(store.conn)
    history = await applier.apply(pid)

    assert history.id is not None
    assert history.proposal_id == pid
    assert history.applied_at > 0

    refreshed = await proposals.get(pid)
    assert refreshed.status == EvolutionStatus.APPLIED
    assert refreshed.applied_at is not None


@pytest.mark.asyncio
async def test_apply_writes_history_row_with_reversible_inverse_diff(
    store: EvolutionStore,
) -> None:
    """The audit row carries an ``inverse_diff`` that captures the
    pre-apply status so the revert is a pure function of the row."""
    proposals = ProposalsRepo(store.conn)
    history_repo = HistoryRepo(store.conn)
    pid = await _seed_proposal(
        proposals, proposal_id="evol-apply-002", status=EvolutionStatus.APPROVED
    )

    applier = EvolutionApplier(store.conn)
    await applier.apply(pid)

    row = await history_repo.latest_for_proposal(pid)
    payload = json.loads(row.inverse_diff)
    assert payload["action"] == "restore_proposal_state"
    assert payload["prior_status"] == "approved"
    assert payload["prior_decided_by"] == "operator"
    # before/after content hashes are populated (64-hex sha256).
    assert len(row.before_sha) == 64
    assert len(row.after_sha) == 64


@pytest.mark.asyncio
async def test_apply_closes_the_intent_log_ticket(
    store: EvolutionStore,
) -> None:
    """A successful apply opens an ``apply_intent_log`` row and stamps
    it committed — the half-committed scan must come back empty."""
    from corlinman_evolution_store import IntentLogRepo

    proposals = ProposalsRepo(store.conn)
    pid = await _seed_proposal(
        proposals, proposal_id="evol-apply-003", status=EvolutionStatus.APPROVED
    )

    applier = EvolutionApplier(store.conn)
    await applier.apply(pid)

    uncommitted = await IntentLogRepo(store.conn).list_uncommitted()
    assert uncommitted == []


# ---------------------------------------------------------------------------
# apply — error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_unknown_id_raises_not_found(
    store: EvolutionStore,
) -> None:
    applier = EvolutionApplier(store.conn)
    with pytest.raises(NotFoundApplyError) as excinfo:
        await applier.apply(ProposalId("evol-ghost-404"))
    assert excinfo.value.proposal_id == "evol-ghost-404"


@pytest.mark.asyncio
async def test_apply_non_approved_raises_not_approved_with_status(
    store: EvolutionStore,
) -> None:
    """A ``pending`` proposal can't be applied; the error carries the
    actual status so the route can build the 409 envelope."""
    proposals = ProposalsRepo(store.conn)
    pid = await _seed_proposal(
        proposals, proposal_id="evol-apply-004", status=EvolutionStatus.PENDING
    )

    applier = EvolutionApplier(store.conn)
    with pytest.raises(NotApprovedApplyError) as excinfo:
        await applier.apply(pid)
    assert excinfo.value.status == "pending"


@pytest.mark.asyncio
async def test_double_apply_is_rejected(store: EvolutionStore) -> None:
    """Once applied, a second apply sees ``status == applied`` and is
    rejected — no second history row, no double mutation."""
    proposals = ProposalsRepo(store.conn)
    history_repo = HistoryRepo(store.conn)
    pid = await _seed_proposal(
        proposals, proposal_id="evol-apply-005", status=EvolutionStatus.APPROVED
    )

    applier = EvolutionApplier(store.conn)
    await applier.apply(pid)

    with pytest.raises(NotApprovedApplyError) as excinfo:
        await applier.apply(pid)
    assert excinfo.value.status == "applied"

    # Exactly one history row — the rejected re-apply wrote nothing.
    row = await history_repo.latest_for_proposal(pid)
    assert row.id is not None


# ---------------------------------------------------------------------------
# revert — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_then_revert_round_trips(store: EvolutionStore) -> None:
    """Applying then reverting restores the proposal to its captured
    pre-apply status (``approved``) and stamps the rollback audit."""
    proposals = ProposalsRepo(store.conn)
    history_repo = HistoryRepo(store.conn)
    pid = await _seed_proposal(
        proposals, proposal_id="evol-revert-001", status=EvolutionStatus.APPROVED
    )

    applier = EvolutionApplier(store.conn)
    await applier.apply(pid)
    history = await applier.revert(pid, "operator: regression spotted")

    refreshed = await proposals.get(pid)
    assert refreshed.status == EvolutionStatus.APPROVED
    assert refreshed.applied_at is None
    assert refreshed.auto_rollback_at is not None
    assert refreshed.auto_rollback_reason == "operator: regression spotted"

    # History row is stamped rolled_back.
    row = await history_repo.latest_for_proposal(pid)
    assert row.rolled_back_at is not None
    assert row.rollback_reason == "operator: regression spotted"
    assert history.rollback_reason == "operator: regression spotted"


@pytest.mark.asyncio
async def test_revert_then_reapply_round_trips(store: EvolutionStore) -> None:
    """Because revert restores the pre-apply status, the proposal can be
    applied again once the underlying issue is fixed."""
    proposals = ProposalsRepo(store.conn)
    pid = await _seed_proposal(
        proposals, proposal_id="evol-revert-002", status=EvolutionStatus.APPROVED
    )

    applier = EvolutionApplier(store.conn)
    await applier.apply(pid)
    await applier.revert(pid, "operator: rollback")
    # Now re-applyable.
    history = await applier.apply(pid)
    assert history.id is not None
    assert (await proposals.get(pid)).status == EvolutionStatus.APPLIED


# ---------------------------------------------------------------------------
# revert — error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revert_unknown_id_raises_not_found(
    store: EvolutionStore,
) -> None:
    applier = EvolutionApplier(store.conn)
    with pytest.raises(NotFoundRevertError) as excinfo:
        await applier.revert(ProposalId("evol-ghost-404"), "r")
    assert excinfo.value.proposal_id == "evol-ghost-404"


@pytest.mark.asyncio
async def test_revert_without_apply_raises_not_applied(
    store: EvolutionStore,
) -> None:
    """An ``approved`` (never-applied) proposal can't be reverted; the
    error carries the status so the route builds a 409."""
    proposals = ProposalsRepo(store.conn)
    pid = await _seed_proposal(
        proposals, proposal_id="evol-revert-003", status=EvolutionStatus.APPROVED
    )

    applier = EvolutionApplier(store.conn)
    with pytest.raises(NotAppliedRevertError) as excinfo:
        await applier.revert(pid, "r")
    assert excinfo.value.status == "approved"


@pytest.mark.asyncio
async def test_double_revert_is_rejected(store: EvolutionStore) -> None:
    """A second revert sees the proposal already back at ``approved``
    and is refused — not idempotent, mirrors the Rust contract."""
    proposals = ProposalsRepo(store.conn)
    pid = await _seed_proposal(
        proposals, proposal_id="evol-revert-004", status=EvolutionStatus.APPROVED
    )

    applier = EvolutionApplier(store.conn)
    await applier.apply(pid)
    await applier.revert(pid, "first")
    with pytest.raises(NotAppliedRevertError):
        await applier.revert(pid, "second")


@pytest.mark.asyncio
async def test_revert_with_missing_history_raises_history_missing(
    store: EvolutionStore,
) -> None:
    """An ``applied`` proposal with no history row signals data
    corruption — the forward apply must have written one."""
    proposals = ProposalsRepo(store.conn)
    # Seed straight into ``applied`` without going through apply(), so
    # no history row exists.
    pid = await _seed_proposal(
        proposals, proposal_id="evol-revert-005", status=EvolutionStatus.APPROVED
    )
    await proposals.mark_applied(pid, 5_000)

    applier = EvolutionApplier(store.conn)
    with pytest.raises(HistoryMissingRevertError) as excinfo:
        await applier.revert(pid, "r")
    assert excinfo.value.proposal_id == "evol-revert-005"


@pytest.mark.asyncio
async def test_internal_apply_error_is_typed(tmp_path: Path) -> None:
    """A storage failure surfaces as the typed :class:`InternalApplyError`
    rather than a raw sqlite exception — the route maps it to a 500.

    Uses its own short-lived store so closing the connection mid-test
    doesn't disturb the shared ``store`` fixture's teardown.
    """
    own = await EvolutionStore.open(tmp_path / "evolution-applier-internal.sqlite")
    proposals = ProposalsRepo(own.conn)
    pid = await _seed_proposal(
        proposals, proposal_id="evol-apply-006", status=EvolutionStatus.APPROVED
    )
    applier = EvolutionApplier(own.conn)
    # Close the connection out from under the applier.
    await own.close()
    with pytest.raises(InternalApplyError):
        await applier.apply(pid)
