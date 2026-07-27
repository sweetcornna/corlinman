"""Tests for :class:`RedisJournalBackend`.

Mirrors the organisation of ``test_agent_journal_postgres.py`` — same
sections, same behavioural assertions — but runs against **fakeredis**
instead of a live server, because (unlike pytest-postgresql, which just
needs a ``pg_ctl`` binary) CI has no Redis service and fakeredis is a
faithful in-memory double of the exact redis-py client the backend
drives. The collection-time skip pattern is the same::

    pytest.importorskip("fakeredis.aioredis", ...)

so the whole module skips cleanly on an environment without the dev
dependencies instead of erroring.

Where the Postgres tests rewrite rows with raw SQL (backdating
``started_at_ms``, injecting metric columns), these tests use the
``_set_turn`` helper which rewrites the turn hash AND re-scores every
index zset the backend keeps — the moral equivalent of the UPDATE.
"""

from __future__ import annotations

import asyncio
import time

import pytest

# Skip the whole module unless the dev extras are available. fakeredis
# pulls redis-py transitively, but assert both so a partial environment
# yields a readable skip reason instead of an ImportError mid-module.
pytest.importorskip(
    "redis",
    reason="redis journal tests need redis-py installed",
)
fakeredis_aioredis = pytest.importorskip(
    "fakeredis.aioredis",
    reason="redis journal tests need fakeredis installed",
)

from corlinman_server.agent_journal_backend import (  # noqa: E402
    RESUME_MAX_AGE_MS,
    TURN_COMPLETED,
    TURN_ERRORED,
    TURN_IN_PROGRESS,
    JournalBackend,
)
from corlinman_server.agent_journal_redis import (  # noqa: E402
    RedisJournalBackend,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def backend():  # type: ignore[no-untyped-def]
    """Open a :class:`RedisJournalBackend` against a fresh fakeredis.

    Each test gets its own in-memory server instance, so there is zero
    cross-test state to clean up — same isolation the per-test database
    gives the Postgres tests.
    """
    client = fakeredis_aioredis.FakeRedis(decode_responses=True)
    be = await RedisJournalBackend.open("redis://journal-test", client=client)
    try:
        yield be
    finally:
        await be.close()


async def _set_turn(backend, turn_id: int, *, started_at_ms: int | None = None, **fields):  # type: ignore[no-untyped-def]
    """Directly rewrite turn fields, the way the Postgres tests issue a
    raw ``UPDATE journal_turns SET …``.

    When ``started_at_ms`` changes, every index zset holding the turn is
    re-scored too (the backend keeps score == started_at_ms in sync on
    the write path; a test-side rewrite must preserve that invariant).
    """
    r = backend._r
    turn_key = backend._turn_key(turn_id)
    row = await r.hgetall(turn_key)
    assert row, f"turn {turn_id} does not exist"
    mapping = {k: str(v) for k, v in fields.items()}
    if started_at_ms is not None:
        mapping["started_at_ms"] = str(int(started_at_ms))
    if mapping:
        await r.hset(turn_key, mapping=mapping)
    if started_at_ms is not None:
        member = str(int(turn_id))
        session_key = row.get("session_key", "")
        for key in (
            backend._k("turns"),
            backend._k("in_progress"),
            backend._session_turns_key(session_key),
            backend._session_errored_key(session_key),
        ):
            if await r.zscore(key, member) is not None:
                await r.zadd(key, {member: int(started_at_ms)})


# ---------------------------------------------------------------------------
# Protocol round-trip
# ---------------------------------------------------------------------------


async def test_backend_satisfies_runtime_protocol(backend) -> None:  # type: ignore[no-untyped-def]
    assert isinstance(backend, JournalBackend)


async def test_same_ms_session_summary_uses_latest_turn_id(backend) -> None:  # type: ignore[no-untyped-def]
    older = await backend.begin_turn("sess-tie", "older", tenant_id="tenant-a")
    newer = await backend.begin_turn("sess-tie", "newer", tenant_id="tenant-a")
    assert older is not None and newer is not None
    await backend.complete_turn(older)
    await _set_turn(backend, older, started_at_ms=5000)
    await _set_turn(backend, newer, started_at_ms=5000)
    rows = await backend.list_session_summaries(tenant_id="tenant-a")
    assert len(rows) == 1
    assert rows[0].last_user_text == "newer"
    assert rows[0].last_status == TURN_IN_PROGRESS


async def test_list_session_turns_tenant_cursor_and_metrics(backend) -> None:  # type: ignore[no-untyped-def]
    first = await backend.begin_turn("sess-page", "first", tenant_id="tenant-a")
    second = await backend.begin_turn("sess-page", "second", tenant_id="tenant-a")
    third = await backend.begin_turn("sess-page", "third", tenant_id="tenant-a")
    foreign = await backend.begin_turn("sess-page", "foreign", tenant_id="tenant-b")
    assert None not in (first, second, third, foreign)
    await _set_turn(
        backend,
        first,
        started_at_ms=1000,
        elapsed_ms=12,
        estimated_cost_usd=0.25,
        cost_status="estimated",
        tool_call_count=2,
        reasoning_token_count=3,
    )
    await _set_turn(backend, second, started_at_ms=2000)
    await _set_turn(backend, third, started_at_ms=2000)
    await _set_turn(backend, foreign, started_at_ms=3000)

    page = await backend.list_session_turns("sess-page", limit=2, tenant_id="tenant-a")
    assert [row["turn_id"] for row in page] == [str(third), str(second)]
    tail = await backend.list_session_turns(
        "sess-page",
        limit=2,
        before_turn_id=str(second),
        tenant_id="tenant-a",
    )
    assert [row["turn_id"] for row in tail] == [str(first)]
    assert tail[0]["elapsed_ms"] == 12
    assert tail[0]["estimated_cost_usd"] == 0.25
    assert tail[0]["cost_status"] == "estimated"
    assert tail[0]["tool_call_count"] == 2
    assert tail[0]["reasoning_token_count"] == 3
    assert await backend.list_session_turns("sess-page", tenant_id="tenant-c") == []


async def test_update_turn_cost_round_trip(backend) -> None:  # type: ignore[no-untyped-def]
    turn_id = await backend.begin_turn("sess-cost", "cost")
    assert turn_id is not None
    await backend.update_turn_cost(
        turn_id,
        estimated_cost_usd=0.75,
        cost_status="estimated",
    )
    rows = await backend.list_session_turns("sess-cost")
    assert rows[0]["estimated_cost_usd"] == 0.75
    assert rows[0]["cost_status"] == "estimated"


async def test_begin_turn_returns_distinct_serial_ids(backend) -> None:  # type: ignore[no-untyped-def]
    a = await backend.begin_turn("sess-1", "first")
    b = await backend.begin_turn("sess-1", "second")
    assert isinstance(a, int)
    assert isinstance(b, int)
    assert a != b


async def test_complete_turn_makes_it_non_resumable(backend) -> None:  # type: ignore[no-untyped-def]
    tid = await backend.begin_turn("sess-c", "do thing")
    await backend.complete_turn(tid)
    assert await backend.find_resumable_turn("sess-c", "do thing") is None


async def test_complete_turn_populates_elapsed_and_tool_count(backend) -> None:  # type: ignore[no-untyped-def]
    tid = await backend.begin_turn("sess-metrics", "do thing")
    assert tid is not None
    await backend.append_message(tid, "tool", '{"ok":true}')
    await _set_turn(backend, tid, started_at_ms=int(time.time() * 1000) - 25)
    await backend.complete_turn(tid)
    rows = await backend.list_session_turns("sess-metrics")
    assert rows[0]["elapsed_ms"] is not None
    assert rows[0]["elapsed_ms"] >= 0
    assert rows[0]["tool_call_count"] == 1


async def test_error_turn_appears_in_recent_errored(backend) -> None:  # type: ignore[no-untyped-def]
    tid = await backend.begin_turn("sess-e", "broken")
    await backend.error_turn(tid, "BANG: provider 500")
    crumbs = await backend.recent_errored_turns("sess-e", limit=5)
    assert len(crumbs) == 1
    assert crumbs[0]["turn_id"] == tid
    assert "BANG" in crumbs[0]["error"]


async def test_append_and_load_messages_round_trip(backend) -> None:  # type: ignore[no-untyped-def]
    tid = await backend.begin_turn("sess-m", "do multi-step")
    await backend.append_message(tid, "user", "do multi-step")
    await backend.append_message(
        tid,
        "assistant",
        "",
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "calculator", "arguments": '{"expression":"2+2"}'},
            }
        ],
    )
    await backend.append_message(tid, "tool", '{"result":4}', tool_call_id="c1")
    msgs = await backend.load_messages(tid)
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool"]
    assert msgs[1]["tool_calls"][0]["id"] == "c1"
    assert msgs[2]["tool_call_id"] == "c1"


async def test_append_messages_batch_preserves_order(backend) -> None:  # type: ignore[no-untyped-def]
    tid = await backend.begin_turn("sess-batch", "batch")
    await backend.append_messages(
        tid,
        [
            {"role": "user", "content": "batch"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
            {"role": "tool", "content": "ok", "tool_call_id": "t1"},
        ],
    )
    msgs = await backend.load_messages(tid)
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool"]
    assert msgs[2]["tool_call_id"] == "t1"


async def test_append_message_to_unknown_turn_is_noop(backend) -> None:  # type: ignore[no-untyped-def]
    """No FK in Redis — the explicit parent probe must stop an orphan
    replay buffer from being created."""
    await backend.append_message(999_999, "user", "ghost")
    assert await backend.load_messages(999_999) == []


async def test_query_messages_matches_sqlite_scope_contract(backend) -> None:  # type: ignore[no-untyped-def]
    first = await backend.begin_turn(
        "telegram:topic-alpha",
        "first",
        user_id="owner",
        channel="telegram",
        tenant_id="tenant-a",
    )
    second = await backend.begin_turn(
        "qq:group-1",
        "second",
        user_id="owner",
        channel="qq",
        tenant_id="tenant-a",
    )
    other = await backend.begin_turn(
        "telegram:topic-alpha",
        "other",
        user_id="other",
        channel="telegram",
        tenant_id="tenant-a",
    )
    assert first is not None and second is not None and other is not None
    await backend.append_message(first, "user", "first-user")
    await backend.append_message(first, "assistant", "first-assistant")
    await backend.append_message(second, "user", "second-user")
    await backend.append_message(other, "user", "other-user")
    await _set_turn(backend, first, started_at_ms=1000)
    await _set_turn(backend, second, started_at_ms=2000)
    await _set_turn(backend, other, started_at_ms=1500)

    rows = await backend.query_messages(
        start_ms=900,
        end_ms=2100,
        roles=["user", "assistant"],
        channels=["telegram", "qq"],
        tenant_id="tenant-a",
        user_id="owner",
    )
    assert [(row["started_at_ms"], row["seq"], row["content"]) for row in rows] == [
        (1000, 0, "first-user"),
        (1000, 1, "first-assistant"),
        (2000, 0, "second-user"),
    ]


async def test_find_resumable_picks_most_recent(backend) -> None:  # type: ignore[no-untyped-def]
    a = await backend.begin_turn("sess-r", "same text", user_id="alice")
    await asyncio.sleep(0.005)
    b = await backend.begin_turn("sess-r", "same text", user_id="bob")
    resume = await backend.find_resumable_turn("sess-r", "same text")
    assert resume is not None
    assert resume.turn_id == b
    assert resume.turn_id != a


# ---------------------------------------------------------------------------
# find_resumable_turn boundary cases
# ---------------------------------------------------------------------------


async def test_find_resumable_returns_none_for_different_session(
    backend,  # type: ignore[no-untyped-def]
) -> None:
    """The session_key is part of the lookup key — a matching user_text
    on a different session must not resume."""
    await backend.begin_turn("sess-A", "shared text")
    assert await backend.find_resumable_turn("sess-B", "shared text") is None


async def test_find_resumable_respects_window_ms(backend) -> None:  # type: ignore[no-untyped-def]
    """Turns older than ``RESUME_MAX_AGE_MS`` are abandoned."""
    tid = await backend.begin_turn("sess-old", "stale task")
    await _set_turn(backend, tid, started_at_ms=0)
    assert await backend.find_resumable_turn("sess-old", "stale task") is None


async def test_find_resumable_requires_text_match(backend) -> None:  # type: ignore[no-untyped-def]
    await backend.begin_turn("sess-t", "task A")
    assert await backend.find_resumable_turn("sess-t", "task B") is None
    assert await backend.find_resumable_turn("sess-t", "task A") is not None


# ---------------------------------------------------------------------------
# Stale-sweep
# ---------------------------------------------------------------------------


async def test_mark_stale_in_progress_as_errored_flips_old_rows(
    backend,  # type: ignore[no-untyped-def]
) -> None:
    tid = await backend.begin_turn("sess-sweep", "abandoned")
    await _set_turn(backend, tid, started_at_ms=0)
    n = await backend.mark_stale_in_progress_as_errored()
    assert n == 1
    crumbs = await backend.recent_errored_turns("sess-sweep", limit=5)
    assert len(crumbs) == 1
    assert "abandoned" in crumbs[0]["error"]


async def test_mark_stale_leaves_fresh_in_progress_alone(backend) -> None:  # type: ignore[no-untyped-def]
    """Recent in-progress rows (younger than RESUME_MAX_AGE_MS) survive
    the sweep. Guards against an over-eager cutoff."""
    tid = await backend.begin_turn("sess-fresh", "still cooking")
    young_ms = int(time.time() * 1000) - 1000
    await _set_turn(backend, tid, started_at_ms=young_ms)
    swept = await backend.mark_stale_in_progress_as_errored()
    assert swept == 0
    resume = await backend.find_resumable_turn("sess-fresh", "still cooking")
    assert resume is not None
    assert resume.turn_id == tid


# ---------------------------------------------------------------------------
# Concurrency — the headline reason this backend exists.
# ---------------------------------------------------------------------------


async def test_two_begin_turn_in_parallel_return_distinct_ids(
    backend,  # type: ignore[no-untyped-def]
) -> None:
    """INCR-allocated turn ids must never collide under concurrent
    begin_turn calls — the precondition for multi-gateway HA."""
    a, b = await asyncio.gather(
        backend.begin_turn("sess-par", "parallel A"),
        backend.begin_turn("sess-par", "parallel B"),
    )
    assert a != b
    assert isinstance(a, int)
    assert isinstance(b, int)


async def test_begin_turn_race_returns_none_on_conflict(
    backend,  # type: ignore[no-untyped-def]
) -> None:
    """C5 on Redis: two ``begin_turn`` calls with the SAME (session_key,
    user_text, user_id) tuple race the SET-NX claim — exactly one
    returns a turn_id; the other returns ``None``. The chat handler
    treats the ``None`` as "another gateway opened the turn; fall back
    to find_resumable_turn"."""
    coros = [
        backend.begin_turn("race-1", "same prompt", user_id="alice"),
        backend.begin_turn("race-1", "same prompt", user_id="alice"),
    ]
    a, b = await asyncio.gather(*coros)
    results = [a, b]
    nones = [r for r in results if r is None]
    ids = [r for r in results if isinstance(r, int)]
    assert len(nones) == 1, f"C5 violation: expected exactly one None on race; got {results}"
    assert len(ids) == 1
    # The surviving row is findable via find_resumable_turn.
    resume = await backend.find_resumable_turn("race-1", "same prompt", user_id="alice")
    assert resume is not None
    assert resume.turn_id == ids[0]


async def test_begin_turn_different_user_ids_do_not_collide(
    backend,  # type: ignore[no-untyped-def]
) -> None:
    """user_id is part of the claim tuple, so two DIFFERENT users in the
    same session typing the same text MUST both succeed."""
    a = await backend.begin_turn("race-2", "ship it", user_id="alice")
    b = await backend.begin_turn("race-2", "ship it", user_id="bob")
    assert a is not None and b is not None and a != b


async def test_completed_turn_releases_begin_claim(backend) -> None:  # type: ignore[no-untyped-def]
    """Redis-specific: the C5 SET-NX claim must be released when the
    turn reaches a terminal status — otherwise a user resending the
    same text right after a completed turn would be locked out for the
    rest of the claim TTL (the Postgres partial index releases the
    moment status leaves in_progress; this pins the parity)."""
    first = await backend.begin_turn("sess-again", "same text", user_id="alice")
    assert first is not None
    await backend.complete_turn(first)
    second = await backend.begin_turn("sess-again", "same text", user_id="alice")
    assert isinstance(second, int) and second != first


async def test_errored_turn_releases_begin_claim(backend) -> None:  # type: ignore[no-untyped-def]
    first = await backend.begin_turn("sess-err-again", "boom", user_id="alice")
    assert first is not None
    await backend.error_turn(first, "exploded")
    second = await backend.begin_turn("sess-err-again", "boom", user_id="alice")
    assert isinstance(second, int) and second != first


async def test_stale_owner_does_not_release_newer_turns_claim(backend) -> None:  # type: ignore[no-untyped-def]
    """A turn that outlives its claim TTL must not release the claim a
    NEWER turn has since taken on the same tuple. The release is
    value-checked (``_release_claim``): the stale owner's member no
    longer matches, so terminalising the old turn leaves the new turn's
    claim standing and a third identical ``begin_turn`` still conflicts.
    """
    first = await backend.begin_turn("sess-ttl", "same text", user_id="alice")
    assert first is not None
    # Simulate the claim TTL expiring mid-turn: drop the key by hand.
    open_key = await backend._r.hget(backend._turn_key(first), "open_key")
    assert open_key
    await backend._r.delete(open_key)
    # A newer turn re-claims the exact same tuple.
    second = await backend.begin_turn("sess-ttl", "same text", user_id="alice")
    assert isinstance(second, int) and second != first
    # The stale owner terminalises long after losing its claim…
    await backend.complete_turn(first)
    # …and the newer turn's claim must survive it.
    third = await backend.begin_turn("sess-ttl", "same text", user_id="alice")
    assert third is None, "stale owner released a claim it no longer held"


async def test_begin_turn_rolls_back_claim_when_write_fails(backend) -> None:  # type: ignore[no-untyped-def]
    """A transient write failure after the SET-NX claim must roll the
    claim back — otherwise the tuple is wedged for the full resume
    window with no resumable row behind it."""
    real_pipeline = backend._r.pipeline
    calls = {"n": 0}

    def flaky_pipeline(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            # First pipeline after the claim = the row/index write burst.
            raise ConnectionError("write burst failed")
        return real_pipeline(*args, **kwargs)

    backend._r.pipeline = flaky_pipeline
    try:
        with pytest.raises(ConnectionError):
            await backend.begin_turn("sess-rb", "text", user_id="alice")
    finally:
        backend._r.pipeline = real_pipeline
    # The rollback (second pipeline call) released the claim, so the
    # retry claims the tuple instead of hitting the 5-minute wedge.
    retry = await backend.begin_turn("sess-rb", "text", user_id="alice")
    assert isinstance(retry, int)


async def test_find_resumable_scopes_by_user_id(
    backend,  # type: ignore[no-untyped-def]
) -> None:
    """S4 on Redis: a turn opened by Alice is NOT visible to Mallory
    even with the same session_key + user_text."""
    tid = await backend.begin_turn("g1", "ship it", user_id="alice")
    assert tid is not None
    assert (await backend.find_resumable_turn("g1", "ship it", user_id="mallory")) is None
    found = await backend.find_resumable_turn("g1", "ship it", user_id="alice")
    assert found is not None
    assert found.turn_id == tid


async def test_recent_errored_turns_is_session_scoped(backend) -> None:  # type: ignore[no-untyped-def]
    a = await backend.begin_turn("sess-a", "a-task")
    b = await backend.begin_turn("sess-b", "b-task")
    await backend.error_turn(a, "fail-a")
    await backend.error_turn(b, "fail-b")
    a_crumbs = await backend.recent_errored_turns("sess-a")
    b_crumbs = await backend.recent_errored_turns("sess-b")
    assert {c["error"] for c in a_crumbs} == {"fail-a"}
    assert {c["error"] for c in b_crumbs} == {"fail-b"}


# ---------------------------------------------------------------------------
# Schema invariants
# ---------------------------------------------------------------------------


async def test_status_strings_match_protocol_constants(backend) -> None:  # type: ignore[no-untyped-def]
    """The status field stores the same string constants the SQL
    backends use, so resume logic that compares status across backends
    keeps working."""
    tid = await backend.begin_turn("sess-status", "x")
    status = await backend._r.hget(backend._turn_key(tid), "status")
    assert status == TURN_IN_PROGRESS
    await backend.complete_turn(tid)
    status = await backend._r.hget(backend._turn_key(tid), "status")
    assert status == TURN_COMPLETED


async def test_resume_window_constant_is_used(backend) -> None:  # type: ignore[no-untyped-def]
    """A row started ``RESUME_MAX_AGE_MS + 1s`` ago is past the window;
    one started 1s ago is still inside."""
    tid_old = await backend.begin_turn("sess-w", "old")
    tid_young = await backend.begin_turn("sess-w2", "young")
    now_ms = int(time.time() * 1000)
    await _set_turn(backend, tid_old, started_at_ms=now_ms - RESUME_MAX_AGE_MS - 1000)
    await _set_turn(backend, tid_young, started_at_ms=now_ms - 1000)
    assert await backend.find_resumable_turn("sess-w", "old") is None
    young = await backend.find_resumable_turn("sess-w2", "young")
    assert young is not None and young.turn_id == tid_young


async def test_close_is_idempotent(backend) -> None:  # type: ignore[no-untyped-def]
    """Closing twice must not raise — the fixture also closes on exit
    so the second call goes through the ``self._client is None`` branch."""
    await backend.close()
    await backend.close()  # second call: no-op
    assert TURN_ERRORED == "errored"


# ---------------------------------------------------------------------------
# Auto-resume — channel column + list_resumable_in_progress (Redis parity)
# ---------------------------------------------------------------------------


async def test_redis_begin_turn_persists_channel(backend) -> None:  # type: ignore[no-untyped-def]
    """The ``channel`` field round-trips through the row so the
    auto-resume scanner can dispatch re-delivery to the right surface.
    """
    tid_tg = await backend.begin_turn("sess-tg", "telegram task", channel="telegram")
    tid_qq = await backend.begin_turn(
        "sess-qq",
        "qq task",
        channel="qq",
        runtime_instance_id="bot-a",
    )
    assert tid_tg is not None and tid_qq is not None

    rows = await backend.list_resumable_in_progress()
    by_id = {r.turn_id: r for r in rows}
    assert by_id[tid_tg].channel == "telegram"
    assert by_id[tid_tg].runtime_instance_id == ""
    assert by_id[tid_qq].channel == "qq"
    assert by_id[tid_qq].runtime_instance_id == "bot-a"


async def test_redis_same_turn_shape_isolated_by_runtime_instance(
    backend,  # type: ignore[no-untyped-def]
) -> None:
    first = await backend.begin_turn(
        "shared-session",
        "same text",
        user_id="sender",
        channel="qq",
        runtime_instance_id="bot-a",
    )
    second = await backend.begin_turn(
        "shared-session",
        "same text",
        user_id="sender",
        channel="qq",
        runtime_instance_id="bot-b",
    )
    assert first is not None and second is not None and first != second

    match = await backend.find_resumable_turn(
        "shared-session",
        "same text",
        user_id="sender",
        channel="qq",
        runtime_instance_id="bot-b",
    )
    wrong = await backend.find_resumable_turn(
        "shared-session",
        "same text",
        user_id="sender",
        channel="qq",
        runtime_instance_id="bot-c",
    )
    assert match is not None and match.turn_id == second
    assert wrong is None


async def test_redis_list_resumable_in_progress_respects_window(
    backend,  # type: ignore[no-untyped-def]
) -> None:
    """Window cutoff matches the SQL peers."""
    tid = await backend.begin_turn("sess-w-r", "stale", channel="telegram")
    await _set_turn(backend, tid, started_at_ms=0)
    # Default window excludes it.
    rows = await backend.list_resumable_in_progress()
    assert tid not in {r.turn_id for r in rows}

    # A huge window picks it up — sanity check the param wires through.
    wide = await backend.list_resumable_in_progress(window_ms=10**13)
    assert tid in {r.turn_id for r in wide}


async def test_redis_mark_stale_accepts_older_than_seconds(
    backend,  # type: ignore[no-untyped-def]
) -> None:
    """The boot-time sweep passes a multi-hour cutoff; verify the Redis
    backend honours it rather than the default 5-min window."""
    tid = await backend.begin_turn("sess-mid-r", "mid-age", channel="telegram")
    backdated = int(time.time() * 1000) - 10 * 60 * 1000
    await _set_turn(backend, tid, started_at_ms=backdated)
    # 1 h cutoff — row stays (younger than 1 h).
    swept_long = await backend.mark_stale_in_progress_as_errored(older_than_seconds=3600)
    assert swept_long == 0
    # 1 min cutoff — row flips.
    swept_short = await backend.mark_stale_in_progress_as_errored(older_than_seconds=60)
    assert swept_short == 1


# ---------------------------------------------------------------------------
# Sessions surface — delete + meta (no dedicated Postgres twin exists for
# these; they exercise the fresh Redis fold logic).
# ---------------------------------------------------------------------------


async def test_delete_session_scopes_by_tenant_then_wipes(backend) -> None:  # type: ignore[no-untyped-def]
    a = await backend.begin_turn("sess-del", "one", tenant_id="tenant-a")
    b = await backend.begin_turn("sess-del", "two", tenant_id="tenant-a")
    c = await backend.begin_turn("sess-del", "three", tenant_id="tenant-b")
    assert None not in (a, b, c)
    await backend.append_message(a, "user", "one")

    # Cross-tenant delete matches nothing.
    assert await backend.delete_session("sess-del", tenant_id="tenant-c") == 0
    # Tenant-scoped delete removes only that tenant's turns.
    assert await backend.delete_session("sess-del", tenant_id="tenant-a") == 2
    assert await backend.load_messages(a) == []
    assert await backend.session_exists("sess-del", tenant_id="tenant-b") is True
    assert await backend.session_exists("sess-del", tenant_id="tenant-a") is False
    # Unscoped delete wipes the remainder and unlists the session.
    assert await backend.delete_session("sess-del") == 1
    assert await backend.session_exists("sess-del") is False
    assert await backend.delete_session("sess-del") == 0
    assert all(s.session_key != "sess-del" for s in await backend.list_session_summaries())


async def test_update_session_meta_partial_update_and_pinned_ordering(
    backend,  # type: ignore[no-untyped-def]
) -> None:
    assert await backend.update_session_meta("ghost", title="nope") is None

    old = await backend.begin_turn("sess-meta-old", "old stuff")
    assert old is not None
    await asyncio.sleep(0.002)
    new = await backend.begin_turn("sess-meta-new", "new stuff")
    assert new is not None

    updated = await backend.update_session_meta("sess-meta-old", title="Kept", pinned=True)
    assert updated is not None
    assert updated.title == "Kept"
    assert updated.pinned is True
    # Partial update: archived flips, title/pinned survive.
    updated = await backend.update_session_meta("sess-meta-old", archived=True)
    assert updated is not None
    assert updated.title == "Kept"
    assert updated.pinned is True
    assert updated.archived is True

    # Pinned sessions sort above unpinned regardless of recency.
    rows = await backend.list_session_summaries()
    assert [s.session_key for s in rows] == ["sess-meta-old", "sess-meta-new"]
