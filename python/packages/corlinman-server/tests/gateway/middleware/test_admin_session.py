"""Unit tests for :class:`AdminSessionStore` (gateway admin sessions).

Exercises the real expiry / sliding-refresh / GC logic against a
*controllable clock*. The store reads wall-time via the module-level
``datetime.now(timezone.utc)`` symbol, so we monkeypatch
``admin_session.datetime`` with a fake whose ``.now()`` returns genuine
``datetime`` instances offset by an advanceable counter. Using real
``datetime`` objects keeps ``timedelta`` subtraction and
``dataclasses.replace`` working exactly as in production — only the
*reading* of "now" is under test control, never the arithmetic.

TTLs are kept tiny (the clock is virtual anyway) and ``start_gc``'s
wiring is asserted by driving its inner ``gc()`` sweep directly, per
the assignment (no flaky reliance on the background loop's tick).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest
from corlinman_server.gateway.middleware import admin_session as admin_session_mod
from corlinman_server.gateway.middleware.admin_session import AdminSessionStore


class _Clock:
    """Virtual wall clock.

    Drop-in for the ``datetime`` *class* the store imports: only
    ``.now(tz)`` is referenced. Returns real ``datetime`` values shifted
    by ``self.offset`` seconds so downstream arithmetic is genuine.
    """

    def __init__(self) -> None:
        # A fixed base so the test is deterministic regardless of host time.
        self._base = datetime(2026, 1, 1, tzinfo=UTC)
        self.offset = 0.0

    def now(self, tz: timezone | None = None) -> datetime:
        return self._base + timedelta(seconds=self.offset)

    def advance(self, seconds: float) -> None:
        self.offset += seconds


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(admin_session_mod, "datetime", c)
    return c


# ---------------------------------------------------------------------------
# create / validate happy path
# ---------------------------------------------------------------------------


def test_create_then_validate_returns_session(clock: _Clock) -> None:
    store = AdminSessionStore(ttl_seconds=100.0)
    token = store.create("alice")

    session = store.validate(token)
    assert session is not None
    assert session.user == "alice"
    # created_at / last_used both stamped at clock t=0.
    assert session.created_at == clock.now()
    assert len(store) == 1


def test_validate_unknown_token_returns_none(clock: _Clock) -> None:
    store = AdminSessionStore(ttl_seconds=100.0)
    assert store.validate("not-a-real-token") is None


def test_invalidate_drops_session(clock: _Clock) -> None:
    store = AdminSessionStore(ttl_seconds=100.0)
    token = store.create("bob")
    store.invalidate(token)
    assert store.validate(token) is None
    assert store.is_empty()


# ---------------------------------------------------------------------------
# TTL expiry: validate returns None once elapsed > ttl
# ---------------------------------------------------------------------------


def test_validate_within_ttl_succeeds(clock: _Clock) -> None:
    store = AdminSessionStore(ttl_seconds=10.0)
    token = store.create("alice")

    clock.advance(10.0)  # elapsed == ttl, not strictly greater → still valid
    assert store.validate(token) is not None


def test_validate_after_ttl_expiry_returns_none_and_evicts(clock: _Clock) -> None:
    store = AdminSessionStore(ttl_seconds=10.0)
    token = store.create("alice")
    assert len(store) == 1

    clock.advance(10.5)  # elapsed > ttl
    assert store.validate(token) is None
    # Inline expiry sweep evicted the entry as a side effect.
    assert len(store) == 0


# ---------------------------------------------------------------------------
# sliding refresh: last_used bumped on validate within ttl
# ---------------------------------------------------------------------------


def test_validate_slides_last_used_forward(clock: _Clock) -> None:
    store = AdminSessionStore(ttl_seconds=10.0)
    token = store.create("alice")

    clock.advance(6.0)
    first = store.validate(token)
    assert first is not None
    assert first.last_used == clock.now()  # bumped to t=6
    # created_at must NOT slide.
    assert first.created_at == clock.now() - timedelta(seconds=6)

    # Another 6s passes. Absolute age is now 12s (> ttl), but because the
    # last validate slid last_used to t=6, elapsed-since-last-used is only
    # 6s < ttl → the session is still alive thanks to the sliding window.
    clock.advance(6.0)
    second = store.validate(token)
    assert second is not None
    assert second.last_used == clock.now()  # bumped again to t=12


def test_session_expires_without_activity_despite_earlier_refresh(
    clock: _Clock,
) -> None:
    store = AdminSessionStore(ttl_seconds=10.0)
    token = store.create("alice")

    clock.advance(5.0)
    assert store.validate(token) is not None  # slid to t=5

    # Now go quiet past the ttl from the last touch.
    clock.advance(11.0)  # t=16, last_used=5 → elapsed 11 > 10
    assert store.validate(token) is None


# ---------------------------------------------------------------------------
# gc(): eviction of expired entries only
# ---------------------------------------------------------------------------


def test_gc_evicts_only_expired_entries(clock: _Clock) -> None:
    store = AdminSessionStore(ttl_seconds=10.0)

    stale = store.create("stale")  # last_used = t=0
    clock.advance(8.0)
    fresh = store.create("fresh")  # last_used = t=8

    clock.advance(3.0)  # t=11: stale elapsed=11 (>10), fresh elapsed=3 (<10)
    evicted = store.gc()

    assert evicted == 1
    assert store.validate(stale) is None
    assert store.validate(fresh) is not None
    assert len(store) == 1


def test_gc_noop_when_nothing_expired(clock: _Clock) -> None:
    store = AdminSessionStore(ttl_seconds=10.0)
    store.create("a")
    store.create("b")

    clock.advance(5.0)
    assert store.gc() == 0
    assert len(store) == 2


def test_gc_returns_count_of_all_evicted(clock: _Clock) -> None:
    store = AdminSessionStore(ttl_seconds=5.0)
    store.create("a")
    store.create("b")
    store.create("c")

    clock.advance(6.0)  # all elapsed 6 > 5
    assert store.gc() == 3
    assert store.is_empty()


# ---------------------------------------------------------------------------
# start_gc wiring: the background sweep actually calls gc()
# ---------------------------------------------------------------------------


def test_start_gc_sweep_invokes_gc(clock: _Clock) -> None:
    """The background loop's tick must route through :meth:`gc`.

    We drive the *real* ``_gc_loop`` body (the same coroutine
    ``start_gc`` schedules) with a tiny interval so its
    ``await asyncio.wait_for(..., timeout=interval)`` fires immediately,
    triggering exactly the sweep the production loop performs — then we
    trip ``_gc_stop`` so the loop exits. This proves the wiring
    (loop body → ``self.gc()`` → eviction) without burning the 60s
    clamp and without a flaky sleep.
    """

    async def _run() -> int:
        store = AdminSessionStore(ttl_seconds=5.0)
        store.create("alice")
        clock.advance(6.0)  # now expired

        calls = {"n": 0}
        real_gc = store.gc

        def _spy_gc() -> int:
            calls["n"] += 1
            return real_gc()

        # Spy on the exact method the loop body calls.
        store.gc = _spy_gc  # type: ignore[method-assign]

        # _gc_loop reads self._gc_stop; start_gc normally sets it.
        store._gc_stop = asyncio.Event()
        task = asyncio.create_task(store._gc_loop(interval=0.001))

        # Let the loop wake once (timeout fires → gc() runs), then stop it.
        # Bounded poll so a scheduling hiccup can't busy-spin forever.
        for _ in range(1000):
            if calls["n"] > 0:
                break
            await asyncio.sleep(0.001)
        store._gc_stop.set()
        await task

        assert task.done()
        assert calls["n"] >= 1
        # The expired session was evicted through the wired gc() path.
        assert store.is_empty()
        return calls["n"]

    n = asyncio.run(_run())
    assert n >= 1


def test_start_gc_is_idempotent_returns_same_task(clock: _Clock) -> None:
    async def _run() -> None:
        store = AdminSessionStore(ttl_seconds=5.0)
        t1 = store.start_gc()
        t2 = store.start_gc()
        assert t1 is t2  # second call returns the live task, no duplicate
        await store.stop_gc()
        assert t1.done()

    asyncio.run(_run())


def test_stop_gc_without_start_is_safe(clock: _Clock) -> None:
    async def _run() -> None:
        store = AdminSessionStore(ttl_seconds=5.0)
        await store.stop_gc()  # no task → must not raise

    asyncio.run(_run())
