"""Repro for BUG-09 — ProposalsRepo must persist tenant_id and the meta
cooldown (Clause B) must scope per (tenant_id, kind), not collapse to the
global DEFAULT_TENANT_ID constant."""

from __future__ import annotations

import pytest
from corlinman_evolution_store import EvolutionStore
from corlinman_evolution_store.repo import (
    EvolutionGuardConfig,
    ProposalsRepo,
    RecursionGuardCooldownError,
)
from corlinman_evolution_store.types import (
    EvolutionKind,
    EvolutionProposal,
    EvolutionRisk,
    EvolutionStatus,
    ProposalId,
)

pytestmark = pytest.mark.asyncio


def _proposal(
    id_: str,
    kind: EvolutionKind,
    *,
    tenant_id: str,
    created_at: int = 1_000,
    status: EvolutionStatus = EvolutionStatus.PENDING,
    applied_at: int | None = None,
) -> EvolutionProposal:
    return EvolutionProposal(
        id=ProposalId(id_),
        kind=kind,
        target=f"target-{id_}",
        diff="",
        reasoning="bug09 fixture",
        risk=EvolutionRisk.HIGH,
        budget_cost=1,
        status=status,
        created_at=created_at,
        decided_at=None if applied_at is None else applied_at - 1,
        decided_by=None if applied_at is None else "operator",
        applied_at=applied_at,
        tenant_id=tenant_id,
    )


async def test_insert_persists_tenant_id(store: EvolutionStore) -> None:
    """Insert a proposal for tenant 'acme'; the row must SELECT back as
    'acme', not collapse to 'default'."""
    repo = ProposalsRepo(store.conn)
    await repo.insert(
        _proposal("p-acme", EvolutionKind.MEMORY_OP, tenant_id="acme")
    )

    cursor = await store.conn.execute(
        "SELECT tenant_id FROM evolution_proposals WHERE id = ?",
        ("p-acme",),
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None
    assert row[0] == "acme"

    # Round-trips back through the decoder too.
    got = await repo.get(ProposalId("p-acme"))
    assert got.tenant_id == "acme"


async def test_meta_cooldown_is_per_tenant(store: EvolutionStore) -> None:
    """Two meta proposals of the same kind in different tenants within the
    cooldown window must NOT gate each other."""
    seed = ProposalsRepo(store.conn)
    first_applied = 5_000_000
    await seed.insert(
        _proposal(
            "meta-acme",
            EvolutionKind.ENGINE_CONFIG,
            tenant_id="acme",
            created_at=first_applied - 1_000,
            status=EvolutionStatus.APPLIED,
            applied_at=first_applied,
        )
    )

    guarded = ProposalsRepo(store.conn).with_guard(EvolutionGuardConfig())
    # 30 minutes later — inside acme's 1h cooldown, but a DIFFERENT tenant.
    second = _proposal(
        "meta-globex",
        EvolutionKind.ENGINE_CONFIG,
        tenant_id="globex",
        created_at=first_applied + 1_800_000,
    )
    # Must succeed: globex has no applied meta of this kind in its window.
    await guarded.insert(second)


async def test_meta_cooldown_still_gates_same_tenant(store: EvolutionStore) -> None:
    """Sanity: same tenant + kind still gates inside the window."""
    seed = ProposalsRepo(store.conn)
    first_applied = 7_000_000
    await seed.insert(
        _proposal(
            "meta-acme-1",
            EvolutionKind.ENGINE_CONFIG,
            tenant_id="acme",
            created_at=first_applied - 1_000,
            status=EvolutionStatus.APPLIED,
            applied_at=first_applied,
        )
    )

    guarded = ProposalsRepo(store.conn).with_guard(EvolutionGuardConfig())
    second = _proposal(
        "meta-acme-2",
        EvolutionKind.ENGINE_CONFIG,
        tenant_id="acme",
        created_at=first_applied + 1_800_000,
    )
    with pytest.raises(RecursionGuardCooldownError):
        await guarded.insert(second)
