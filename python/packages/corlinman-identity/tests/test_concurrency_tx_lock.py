"""Regression tests for the shared-connection transaction-atomicity bug.

R5-B3 recurrence: ``SqliteIdentityStore`` shares one autocommit
``aiosqlite.Connection`` across concurrent async callers. The
multi-statement transactional paths (``resolve_or_create``,
``redeem_phrase``, ``merge_users``) gate their ``BEGIN..COMMIT`` on
``store.tx_lock``. But ``issue_phrase`` / ``sweep_expired_phrases`` did a
bare ``conn.execute(...)`` + ``conn.commit()`` *without* the lock.

Because ``commit()`` is connection-global, a stray ``commit()`` from one
of those single-statement writers that lands mid-flight of another
coroutine's open transaction ends that transaction. The victim's later
``rollback()`` then finds no live transaction and silently does nothing,
so a half-finished ``_insert_new_user_and_alias`` leaves an orphaned
``user_identities`` row with no ``user_aliases`` binding.

aiosqlite dispatches every statement to a background thread, so the
interleave is driven by waiting on the executor round-trip rather than
by ``asyncio`` scheduling alone.
"""

from __future__ import annotations

import asyncio
import sqlite3

from corlinman_identity import SqliteIdentityStore
from corlinman_identity.error import StorageError

# How long to let a coroutine make progress on the aiosqlite executor
# thread before asserting on its state. aiosqlite hops to a worker
# thread for every statement, so a couple of ``asyncio.sleep(0)`` yields
# are NOT enough to observe completion; we need a real (small) wait.
_EXECUTOR_TICK_S = 0.1


async def test_concurrent_issue_phrase_does_not_orphan_a_user_row(
    fresh_store: SqliteIdentityStore,
) -> None:
    """A concurrent ``issue_phrase`` must not prematurely commit an
    in-flight ``_insert_new_user_and_alias`` transaction, and a forced
    failure on the alias INSERT must roll back cleanly with no orphan.

    We drive ``resolve_or_create`` for a brand-new alias (the slow,
    transactional path). Mid-transaction — after the ``user_identities``
    INSERT but before the ``user_aliases`` INSERT — we let a concurrent
    ``issue_phrase`` fully run (including its ``commit()``), then force
    the alias INSERT to raise a UNIQUE violation.

    Pre-fix: ``issue_phrase``'s un-gated ``commit()`` ends the resolver's
    transaction, so the later ``rollback()`` is a no-op and the
    half-written ``user_identities`` row survives orphaned.

    Post-fix: ``issue_phrase`` blocks on ``tx_lock`` (held implicitly by
    the resolver's open transaction), cannot commit mid-flight, and the
    rollback cleanly removes the partial row.
    """
    # An existing user so issue_phrase has a real user_id to write a
    # phrase row against — it's the bare-commit writer we're racing.
    issuer_uid = await fresh_store.resolve_or_create("qq", "issuer", None)

    real_execute = type(fresh_store.conn).execute

    mid_tx_reached = asyncio.Event()
    resume_tx = asyncio.Event()
    state = {"saw_identity_insert": False}

    async def patched_execute(conn: object, sql: str, *a: object, **kw: object):
        normalized = " ".join(sql.split())
        if (
            normalized.startswith("INSERT INTO user_identities")
            and not state["saw_identity_insert"]
        ):
            state["saw_identity_insert"] = True
            result = await real_execute(conn, sql, *a, **kw)  # type: ignore[arg-type]
            # Park mid-transaction (row written, not yet committed) and
            # wait for the concurrent issue_phrase interleave to happen.
            mid_tx_reached.set()
            await resume_tx.wait()
            return result
        if (
            normalized.startswith("INSERT INTO user_aliases")
            and state["saw_identity_insert"]
        ):
            # Force the new user's alias INSERT to fail → resolver must
            # roll back its whole transaction.
            raise sqlite3.IntegrityError(
                "UNIQUE constraint failed: user_aliases.channel, "
                "user_aliases.channel_user_id"
            )
        return await real_execute(conn, sql, *a, **kw)  # type: ignore[arg-type]

    type(fresh_store.conn).execute = patched_execute  # type: ignore[assignment]
    try:

        async def run_resolver() -> object:
            try:
                return await fresh_store.resolve_or_create("telegram", "newuser", None)
            except StorageError as exc:
                return exc

        resolver_task = asyncio.create_task(run_resolver())

        # Wait until the resolver is parked mid-transaction (identity row
        # written inside an open BEGIN, alias INSERT not yet attempted).
        await asyncio.wait_for(mid_tx_reached.wait(), timeout=2.0)

        # Only now fire the bare-commit writer, so its execute + commit
        # land squarely inside the resolver's open transaction.
        issue_task = asyncio.create_task(
            fresh_store.issue_phrase(issuer_uid, "qq", "issuer")
        )

        # Give issue_phrase a real chance to run its bare commit() on the
        # executor thread *while the resolver's transaction is open*.
        # Pre-fix it completes here (ending the resolver's transaction);
        # post-fix it stays blocked on the lock.
        await asyncio.sleep(_EXECUTOR_TICK_S)

        # Release the resolver so it proceeds to the failing alias INSERT
        # and its rollback. (Post-fix this also lets issue_phrase acquire
        # the lock once the resolver's transaction finishes.)
        resume_tx.set()

        await asyncio.wait_for(
            asyncio.gather(resolver_task, issue_task, return_exceptions=True),
            timeout=2.0,
        )
    finally:
        type(fresh_store.conn).execute = real_execute  # type: ignore[assignment]

    # The forced UNIQUE failure must have rolled back the whole
    # transaction: NO user_identities row left without a matching alias.
    cursor = await fresh_store.conn.execute(
        "SELECT user_id FROM user_identities "
        "WHERE user_id NOT IN (SELECT user_id FROM user_aliases)"
    )
    orphans = await cursor.fetchall()
    await cursor.close()
    assert orphans == [], (
        "orphaned user_identities row(s) with no alias binding survived "
        f"a rolled-back transaction: {orphans}"
    )


async def test_issue_phrase_serialises_behind_tx_lock(
    fresh_store: SqliteIdentityStore,
) -> None:
    """``issue_phrase`` must acquire ``tx_lock`` so its global ``commit()``
    can never land inside another coroutine's open transaction.

    Holding ``tx_lock`` externally must keep ``issue_phrase`` pending
    until release. Pre-fix it ignores the lock and completes on the
    executor thread despite the lock being held.
    """
    uid = await fresh_store.resolve_or_create("qq", "1234", None)

    await fresh_store.tx_lock.acquire()
    try:
        task = asyncio.create_task(fresh_store.issue_phrase(uid, "qq", "1234"))
        # Real wait so the aiosqlite executor thread has time to run the
        # INSERT + commit if the lock is (buggily) bypassed.
        await asyncio.sleep(_EXECUTOR_TICK_S)
        assert not task.done(), (
            "issue_phrase completed while tx_lock was held — it bypasses "
            "the lock, so its connection-global commit() can end another "
            "coroutine's in-flight transaction"
        )
    finally:
        fresh_store.tx_lock.release()

    result = await asyncio.wait_for(task, timeout=2.0)
    assert result.user_id == uid


async def test_sweep_expired_phrases_serialises_behind_tx_lock(
    fresh_store: SqliteIdentityStore,
) -> None:
    """``sweep_expired_phrases`` must likewise acquire ``tx_lock`` before
    its bare ``commit()``."""
    await fresh_store.resolve_or_create("qq", "1234", None)

    await fresh_store.tx_lock.acquire()
    try:
        task = asyncio.create_task(fresh_store.sweep_expired_phrases())
        await asyncio.sleep(_EXECUTOR_TICK_S)
        assert not task.done(), (
            "sweep_expired_phrases completed while tx_lock was held — its "
            "connection-global commit() can end another coroutine's "
            "in-flight transaction"
        )
    finally:
        fresh_store.tx_lock.release()

    removed = await asyncio.wait_for(task, timeout=2.0)
    assert removed == 0
