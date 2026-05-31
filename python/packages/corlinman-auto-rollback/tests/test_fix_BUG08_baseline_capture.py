"""BUG-08 repro + fix: auto-rollback applier must persist a real
``metrics_baseline`` snapshot, not an empty ``{}`` that the monitor's
strict :meth:`MetricSnapshot.from_dict` decoder always rejects.

End-to-end shape mirrors ``test_monitor.py`` / ``test_applier.py``:
seed an ``approved`` MEMORY_OP proposal, apply it through the real
:class:`EvolutionApplier`, emit failure signals on its target inside
the grace window, then run one monitor pass.

Before the fix the applier wrote ``metrics_baseline={}`` →
``MetricSnapshot.from_dict({})`` raises ``ValueError`` (missing
``target``) → the monitor logs malformed, bumps ``errors`` and never
inspects (``errors == 1``, ``inspected == 0``), making the feature
dead. After the fix a valid baseline is stored and the breach is
evaluated (``inspected == 1`` and the rollback fires).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from corlinman_auto_rollback.applier import EvolutionApplier
from corlinman_auto_rollback.config import (
    AutoRollbackThresholds,
    EvolutionAutoRollbackConfig,
)
from corlinman_auto_rollback.metrics import MetricSnapshot
from corlinman_auto_rollback.monitor import AutoRollbackMonitor, now_ms
from corlinman_auto_rollback.revert import RevertError
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


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[EvolutionStore]:
    s = await EvolutionStore.open(tmp_path / "evolution-bug08.sqlite")
    try:
        yield s
    finally:
        await s.close()


class _MockApplier:
    """Captures (id, reason) the monitor passes to revert."""

    def __init__(self, result: RevertError | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._result = result

    async def revert(self, proposal_id: ProposalId, reason: str) -> None:
        self.calls.append((str(proposal_id), reason))
        if self._result is not None:
            raise self._result


def _config() -> EvolutionAutoRollbackConfig:
    return EvolutionAutoRollbackConfig(
        enabled=True,
        grace_window_hours=72,
        thresholds=AutoRollbackThresholds(
            default_err_rate_delta_pct=50.0,
            default_p95_latency_delta_pct=25.0,
            signal_window_secs=1_800,
            min_baseline_signals=5,
        ),
    )


async def _seed_approved(
    repo: ProposalsRepo, *, proposal_id: str, target: str
) -> ProposalId:
    pid = ProposalId(proposal_id)
    await repo.insert(
        EvolutionProposal(
            id=pid,
            kind=EvolutionKind.MEMORY_OP,
            target=target,
            diff='{"after": "merged"}',
            reasoning="seeded by BUG-08 test",
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
    return pid


async def _seed_signals(
    store: EvolutionStore, *, target: str, n: int, observed_at: int
) -> None:
    for _ in range(n):
        await store.conn.execute(
            """INSERT INTO evolution_signals
                 (event_kind, target, severity, payload_json, observed_at)
               VALUES ('tool.call.failed', ?, 'error', '{}', ?)""",
            (target, observed_at),
        )
    await store.conn.commit()


@pytest.mark.asyncio
async def test_applier_persists_real_baseline_snapshot(
    store: EvolutionStore,
) -> None:
    """The history row the applier writes must decode cleanly through
    the monitor's strict ``MetricSnapshot.from_dict`` (was ``{}``)."""
    proposals = ProposalsRepo(store.conn)
    history_repo = HistoryRepo(store.conn)
    target = "merge_chunks:1,2"
    pid = await _seed_approved(proposals, proposal_id="bug08-baseline", target=target)

    applier = EvolutionApplier(store.conn, config=_config())
    await applier.apply(pid)

    row = await history_repo.latest_for_proposal(pid)
    # Must NOT be the empty dict that the strict decoder rejects.
    snapshot = MetricSnapshot.from_dict(row.metrics_baseline)
    assert snapshot.target == target
    assert snapshot.window_secs == 1_800
    assert set(snapshot.counts) == {"tool.call.failed", "search.recall.dropped"}


@pytest.mark.asyncio
async def test_monitor_inspects_and_reverts_after_apply(
    store: EvolutionStore,
) -> None:
    """The acceptance criterion: apply -> emit failure signals within
    grace -> run_once inspects (was errors==1/inspected==0) and rolls
    back on the breach."""
    proposals = ProposalsRepo(store.conn)
    history_repo = HistoryRepo(store.conn)
    now = now_ms()
    target = "merge_chunks:9"
    pid = await _seed_approved(proposals, proposal_id="bug08-breach", target=target)

    # Seed a non-quiet pre-apply baseline (>= min_baseline_signals=5) so
    # the quiet-target guard doesn't suppress the breach; these fall
    # inside the signal window so apply-time capture sees them.
    await _seed_signals(store, target=target, n=10, observed_at=now - 1_000)

    # Apply: the baseline snapshot now captures ~10 pre-apply failures.
    applier = EvolutionApplier(store.conn, config=_config())
    history = await applier.apply(pid)
    # Sanity: the persisted baseline is a real snapshot above the floor.
    baseline = MetricSnapshot.from_dict(history.metrics_baseline)
    assert baseline.counts.get("tool.call.failed", 0) >= 5

    # Emit a post-apply surge on the target inside the grace window:
    # baseline ~10 -> current ~110 = +1000%, well past +50%.
    await _seed_signals(store, target=target, n=100, observed_at=now)

    # Re-stamp applied_at to "now" so it lands in the grace window
    # regardless of clock drift between apply() and the seed.
    await proposals.mark_applied(pid, now)

    mock = _MockApplier()
    monitor = AutoRollbackMonitor(
        proposals, history_repo, store, mock, _config()
    )
    summary = await monitor.run_once()

    assert summary.errors == 0, "real baseline must decode, not error out"
    assert summary.proposals_inspected == 1, "feature must actually inspect"
    assert summary.thresholds_breached == 1
    assert summary.rollbacks_triggered == 1
    assert len(mock.calls) == 1
    assert mock.calls[0][0] == "bug08-breach"
