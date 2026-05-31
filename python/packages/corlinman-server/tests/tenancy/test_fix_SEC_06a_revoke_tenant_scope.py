"""SEC-06a: ``AdminDb.revoke_api_key`` must be tenant-scoped.

The bug: ``revoke_api_key`` UPDATEd ``WHERE key_id=? AND revoked_at_ms
IS NULL`` with no tenant predicate, so any tenant could revoke any
other tenant's key purely by id. This test mints a key for tenant
``victim`` and asks tenant ``attacker`` to revoke it; the revoke must
NOT succeed (returns ``False``) and the victim's key must stay active.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from corlinman_server.tenancy import AdminDb, TenantId


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[AdminDb]:
    path = tmp_path / "tenants.sqlite"
    instance = await AdminDb.open(path)
    try:
        yield instance
    finally:
        await instance.close()


async def test_revoke_is_tenant_scoped(db: AdminDb) -> None:
    victim = TenantId.new("victim")
    attacker = TenantId.new("attacker")
    await db.create_tenant(victim, "Victim", 1)
    await db.create_tenant(attacker, "Attacker", 1)

    minted = await db.mint_api_key(victim, "admin", "chat", None)
    key_id = minted.row.key_id

    # Attacker tries to revoke the victim's key by id — must be a no-op.
    revoked = await db.revoke_api_key(attacker, key_id)
    assert revoked is False, "cross-tenant revoke must not match"

    # The victim's key is still active.
    active = await db.list_api_keys(victim)
    assert any(r.key_id == key_id for r in active), "victim key still active"

    # The rightful tenant can revoke it.
    revoked_own = await db.revoke_api_key(victim, key_id)
    assert revoked_own is True
    active_after = await db.list_api_keys(victim)
    assert all(r.key_id != key_id for r in active_after)
