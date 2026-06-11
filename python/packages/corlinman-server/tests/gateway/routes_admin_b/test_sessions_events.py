"""Tests for ``/admin/sessions/{key}/events*`` (W1.3 SSE + JSON replay).

Covers:

* SSE generator end-to-end — three envelopes emitted on the
  :class:`JournalBackedEmitter` round-trip through the streaming
  generator in arrival order, with composite ``<turn_id>:<sequence>``
  ``id:`` lines and JSON ``data:`` payloads.
* Catch-up via ``Last-Event-ID`` — journal pre-populated with five
  events; the generator (called with ``catch_up_sequence=2``) yields
  events 3 and 4 from the journal before settling into the live loop.
* Heartbeat — when the emitter is silent past the heartbeat interval
  the generator yields a ``:keepalive`` comment frame.
* JSON replay pagination — events fold into pages bounded by ``limit``
  with a ``next_cursor`` pointing past the last returned sequence.
* ``Last-Event-ID`` header / query parsing.
* 503 envelope when no observability handle is wired.

We test the SSE wire format by driving the underlying ``_sse_stream``
generator directly with a real emitter + journal — this is the layer
that produces the bytes and is exactly what the FastAPI route hands
back to ``StreamingResponse``. Going through HTTP would just add the
``StreamingResponse`` wrapper and the ASGI chunked encoding on top —
neither is the unit we're verifying here, and both make the test
race-prone under the synchronous ``TestClient``.

The non-streaming replay and 503 paths still go through ``TestClient``
because those don't involve open-ended generator iteration.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from corlinman_agent.events import EventEnvelope, TextDelta, TurnComplete
from corlinman_server.agent_journal import AgentJournal
from corlinman_server.gateway.observability import JournalBackedEmitter
from corlinman_server.gateway.routes_admin_b.infra import sessions_events
from corlinman_server.gateway.routes_admin_b.infra.sessions_events import (
    _parse_last_event_id,
    _sse_stream,
)
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    require_admin,
    set_admin_state,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def journal(tmp_path: Path) -> AsyncIterator[AgentJournal]:
    j = await AgentJournal.open(tmp_path / "journal.sqlite")
    try:
        yield j
    finally:
        await j.close()


@pytest_asyncio.fixture
async def emitter(journal: AgentJournal) -> AsyncIterator[JournalBackedEmitter]:
    yield JournalBackedEmitter(journal)


@pytest_asyncio.fixture
async def state(
    tmp_path: Path,
    journal: AgentJournal,
    emitter: JournalBackedEmitter,
) -> AsyncIterator[AdminState]:
    """Wired AdminState — emitter + journal both present."""
    s = AdminState(
        data_dir=tmp_path,
        journal=journal,
        event_emitter=emitter,
    )
    set_admin_state(s)
    try:
        yield s
    finally:
        set_admin_state(None)


@pytest_asyncio.fixture
async def app(state: AdminState) -> AsyncIterator[FastAPI]:
    """FastAPI app with the sessions_events router mounted. Admin auth
    bypassed via dependency override."""
    application = FastAPI()
    application.include_router(sessions_events.router())
    application.dependency_overrides[require_admin] = lambda: None
    yield application


def _make_envelope(
    *,
    turn_id: str = "turn-1",
    session_key: str = "sess-1",
    sequence: int = 0,
    text: str = "hi",
) -> EventEnvelope:
    return EventEnvelope(
        turn_id=turn_id,
        session_key=session_key,
        sequence=sequence,
        timestamp_ms=1_700_000_000_000 + sequence,
        event=TextDelta(index=0, text=text),
    )


def _parse_sse_frames(payload: bytes) -> list[dict[str, str]]:
    """Split SSE bytes into ``[{id, event, data}]`` dicts.

    Comment frames (``: keepalive``) emit as ``event="comment"``,
    ``data=`` the comment text.
    """
    frames: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw in payload.decode("utf-8").splitlines():
        if not raw:
            if current:
                frames.append(current)
                current = {}
            continue
        if raw.startswith(":"):
            frames.append({"event": "comment", "data": raw[1:].strip()})
            continue
        if ":" not in raw:
            continue
        field, _, value = raw.partition(":")
        current[field.strip()] = value.lstrip()
    if current:
        frames.append(current)
    return frames


async def _drain_frames(
    gen: AsyncIterator[bytes], *, until_count_of: str, count: int
) -> bytes:
    """Drive ``gen`` until ``b''.join(out).count(until_count_of) >= count``."""
    chunks: list[bytes] = []
    async for chunk in gen:
        chunks.append(chunk)
        if b"".join(chunks).count(until_count_of.encode()) >= count:
            break
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# Live SSE — drive the generator directly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_sse_receives_emitted_envelopes(
    state: AdminState,
    emitter: JournalBackedEmitter,
) -> None:
    """Push 3 envelopes after the generator subscribes; expect 3 frames
    in order with composite ``turn_id:sequence`` ids."""
    gen = _sse_stream(state, "sess-1", catch_up_turn_id=None, catch_up_sequence=-1)

    async def _producer() -> None:
        # Wait until the SSE handler has registered its subscriber.
        for _ in range(50):
            if emitter.subscriber_count("sess-1") > 0:
                break
            await asyncio.sleep(0.02)
        for seq in range(3):
            await emitter.emit(_make_envelope(sequence=seq, text=f"tok-{seq}"))

    producer_task = asyncio.create_task(_producer())
    try:
        body = await asyncio.wait_for(
            _drain_frames(gen, until_count_of="event: TextDelta", count=3),
            timeout=5.0,
        )
    finally:
        producer_task.cancel()
        with contextlib_suppress():
            await producer_task
        await gen.aclose()

    frames = _parse_sse_frames(body)
    text_frames = [f for f in frames if f.get("event") == "TextDelta"]
    assert len(text_frames) >= 3
    texts = []
    for f in text_frames[:3]:
        payload = json.loads(f["data"])
        texts.append(payload["payload"]["text"])
    assert texts == ["tok-0", "tok-1", "tok-2"]
    assert text_frames[0]["id"] == "turn-1:0"
    assert text_frames[2]["id"] == "turn-1:2"


def contextlib_suppress():  # noqa: D401 — trivial helper
    """Mimic ``contextlib.suppress(CancelledError, Exception)``."""
    import contextlib

    return contextlib.suppress(asyncio.CancelledError, Exception)


# ---------------------------------------------------------------------------
# Catch-up via Last-Event-ID
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_last_event_id_catches_up_from_journal(
    state: AdminState,
    journal: AgentJournal,
) -> None:
    """Pre-populate 5 events; resume at seq 2 and receive events 3 + 4."""
    tid = await journal.begin_turn("sess-1", "hello")
    assert tid is not None

    for seq in range(5):
        await journal.append_event(
            _make_envelope(turn_id=str(tid), sequence=seq, text=f"old-{seq}")
        )

    gen = _sse_stream(
        state,
        "sess-1",
        catch_up_turn_id=str(tid),
        catch_up_sequence=2,
    )
    try:
        body = await asyncio.wait_for(
            _drain_frames(gen, until_count_of="old-4", count=1),
            timeout=5.0,
        )
    finally:
        await gen.aclose()

    frames = _parse_sse_frames(body)
    text_frames = [f for f in frames if f.get("event") == "TextDelta"]
    seqs = [int(f["id"].split(":", 1)[1]) for f in text_frames]
    assert 3 in seqs
    assert 4 in seqs
    # Already-seen sequences MUST NOT replay.
    assert 0 not in seqs
    assert 1 not in seqs
    assert 2 not in seqs


@pytest.mark.asyncio
async def test_no_duplicate_when_event_emitted_during_catch_up(
    state: AdminState,
    journal: AgentJournal,
    emitter: JournalBackedEmitter,
) -> None:
    """An event committed to the journal AND pushed to the live queue
    while catch-up is paging must be delivered exactly once — the live
    loop drops what catch-up already sent (high-water dedup)."""
    tid = await journal.begin_turn("sess-1", "hello")
    assert tid is not None
    for seq in range(3):
        await journal.append_event(
            _make_envelope(turn_id=str(tid), sequence=seq, text=f"old-{seq}")
        )

    gen = _sse_stream(
        state, "sess-1", catch_up_turn_id=str(tid), catch_up_sequence=0
    )

    async def _producer() -> None:
        # After catch-up has subscribed, push a seq the journal already
        # holds (seq 2) — simulating the replay/live overlap window — then
        # a genuinely new one (seq 3).
        for _ in range(50):
            if emitter.subscriber_count("sess-1") > 0:
                break
            await asyncio.sleep(0.02)
        await emitter.emit(_make_envelope(turn_id=str(tid), sequence=2, text="old-2"))
        await emitter.emit(_make_envelope(turn_id=str(tid), sequence=3, text="new-3"))

    producer = asyncio.create_task(_producer())
    try:
        body = await asyncio.wait_for(
            _drain_frames(gen, until_count_of="new-3", count=1), timeout=5.0
        )
    finally:
        producer.cancel()
        with contextlib_suppress():
            await producer
        await gen.aclose()

    frames = _parse_sse_frames(body)
    seqs = [
        int(f["id"].split(":", 1)[1])
        for f in frames
        if f.get("event") == "TextDelta"
    ]
    assert seqs.count(2) == 1, f"seq 2 duplicated: {seqs}"  # not replayed twice
    assert 3 in seqs


@pytest.mark.asyncio
async def test_no_duplicate_on_bare_resume_during_catch_up(
    state: AdminState,
    journal: AgentJournal,
    emitter: JournalBackedEmitter,
) -> None:
    """The replay/live overlap must dedup for a BARE resume too. Dedup is
    scoped to the turn catch-up actually replays (the resolved latest
    turn), so an event committed to that turn mid-replay — landing in both
    a journal page and the live queue — is delivered exactly once, even
    though the client sent only a bare sequence (no composite turn)."""
    tid = await journal.begin_turn("sess-1", "hello")
    assert tid is not None
    for seq in range(3):
        await journal.append_event(
            _make_envelope(turn_id=str(tid), sequence=seq, text=f"old-{seq}")
        )

    # Bare resume: sequence only, no turn — catch-up resolves the latest
    # turn (tid) and dedup must scope to it.
    gen = _sse_stream(state, "sess-1", catch_up_turn_id=None, catch_up_sequence=0)

    async def _producer() -> None:
        for _ in range(50):
            if emitter.subscriber_count("sess-1") > 0:
                break
            await asyncio.sleep(0.02)
        # seq 2 already lives in the journal (replay/live overlap); seq 3
        # is genuinely new — both on the resolved turn.
        await emitter.emit(_make_envelope(turn_id=str(tid), sequence=2, text="old-2"))
        await emitter.emit(_make_envelope(turn_id=str(tid), sequence=3, text="new-3"))

    producer = asyncio.create_task(_producer())
    try:
        body = await asyncio.wait_for(
            _drain_frames(gen, until_count_of="new-3", count=1), timeout=5.0
        )
    finally:
        producer.cancel()
        with contextlib_suppress():
            await producer
        await gen.aclose()

    frames = _parse_sse_frames(body)
    seqs = [
        int(f["id"].split(":", 1)[1])
        for f in frames
        if f.get("event") == "TextDelta"
    ]
    assert seqs.count(2) == 1, f"seq 2 duplicated on bare resume: {seqs}"
    assert 3 in seqs


@pytest.mark.asyncio
async def test_live_only_delta_in_journal_gap_survives(
    state: AdminState,
    journal: AgentJournal,
    emitter: JournalBackedEmitter,
) -> None:
    """A delta the emitter live-fanned but whose deferred durable write was
    dropped (a journal GAP) must still reach the client. Catch-up dedups by
    the EXACT replayed sequences, not a ``seq <= high-water`` range, so a
    live-only seq sitting below a later replayed seq is not mistaken for a
    replay duplicate and discarded."""
    tid = await journal.begin_turn("sess-1", "hello")
    assert tid is not None
    # Journal has a hole at seq 3 (its batched write "failed"); 4 and 5 did
    # persist, so a range high-water (5) would pass over the missing 3.
    for seq in (0, 1, 2, 4, 5):
        await journal.append_event(
            _make_envelope(turn_id=str(tid), sequence=seq, text=f"old-{seq}")
        )

    gen = _sse_stream(
        state, "sess-1", catch_up_turn_id=str(tid), catch_up_sequence=0
    )
    # Drain the catch-up frames (seqs 1,2,4,5) first so the replayed set is
    # fixed BEFORE any live event is emitted — and confirm the gap (3) was
    # never replayed.
    catch_up = b""
    for _ in range(4):
        catch_up += await asyncio.wait_for(gen.__anext__(), timeout=5.0)
    cu_seqs = [
        int(f["id"].split(":", 1)[1])
        for f in _parse_sse_frames(catch_up)
        if f.get("event") == "TextDelta"
    ]
    assert cu_seqs == [1, 2, 4, 5]  # the gap at 3 is absent from the replay

    # seq 3 was only ever live-fanned (its journal write was dropped); seq 6
    # is genuinely new. Neither is in the replayed set, so both must survive.
    await emitter.emit(_make_envelope(turn_id=str(tid), sequence=3, text="live-3"))
    await emitter.emit(_make_envelope(turn_id=str(tid), sequence=6, text="new-6"))
    live = b""
    try:
        for _ in range(2):
            live += await asyncio.wait_for(gen.__anext__(), timeout=5.0)
    finally:
        await gen.aclose()

    texts = [
        json.loads(f["data"])["payload"]["text"]
        for f in _parse_sse_frames(live)
        if f.get("event") == "TextDelta"
    ]
    assert "live-3" in texts, texts  # gap delta NOT suppressed by a range check
    assert "new-6" in texts


@pytest.mark.asyncio
async def test_bounded_dedup_still_suppresses_recent_overlap(
    state: AdminState,
    journal: AgentJournal,
    emitter: JournalBackedEmitter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dedup memory is bounded to the most-recent ``DEDUP_WINDOW`` replayed
    sequences, so a long turn's whole backlog never stays resident. The
    bound is safe because a replay/live duplicate must still be queued, so
    its sequence is always within the window — proven here by suppressing a
    recent overlap even when the window is far smaller than the backlog."""
    monkeypatch.setattr(sessions_events, "DEDUP_WINDOW", 2)
    tid = await journal.begin_turn("sess-1", "hello")
    assert tid is not None
    for seq in range(6):  # backlog 0..5 — far larger than the 2-wide window
        await journal.append_event(
            _make_envelope(turn_id=str(tid), sequence=seq, text=f"old-{seq}")
        )

    gen = _sse_stream(
        state, "sess-1", catch_up_turn_id=str(tid), catch_up_sequence=0
    )

    async def _producer() -> None:
        for _ in range(50):
            if emitter.subscriber_count("sess-1") > 0:
                break
            await asyncio.sleep(0.02)
        # seq 5 is the most-recent replayed row (inside the 2-wide window) —
        # the replay/live overlap; seq 6 is genuinely new.
        await emitter.emit(_make_envelope(turn_id=str(tid), sequence=5, text="old-5"))
        await emitter.emit(_make_envelope(turn_id=str(tid), sequence=6, text="new-6"))

    producer = asyncio.create_task(_producer())
    try:
        body = await asyncio.wait_for(
            _drain_frames(gen, until_count_of="new-6", count=1), timeout=5.0
        )
    finally:
        producer.cancel()
        with contextlib_suppress():
            await producer
        await gen.aclose()

    frames = _parse_sse_frames(body)
    seqs = [
        int(f["id"].split(":", 1)[1])
        for f in frames
        if f.get("event") == "TextDelta"
    ]
    assert seqs.count(5) == 1, f"recent overlap seq 5 duplicated: {seqs}"
    assert 6 in seqs


@pytest.mark.asyncio
async def test_bare_resume_does_not_dedup_a_new_turn(
    state: AdminState,
    journal: AgentJournal,
    emitter: JournalBackedEmitter,
) -> None:
    """A bare ``Last-Event-ID`` (sequence only, no turn) is not turn-scoped,
    so it must never seed live dedup. Even after catch-up raises the
    high-water mark, a fresh turn's early events — whose sequences restart
    from 0 and fall *below* that mark — must still be delivered. Dropping
    them was the bug where a bare resume wrongly suppressed a new turn.
    """
    tid = await journal.begin_turn("sess-1", "hello")
    assert tid is not None
    for seq in range(10):
        await journal.append_event(
            _make_envelope(turn_id=str(tid), sequence=seq, text=f"old-{seq}")
        )

    # Bare resume: no composite turn, sequence only. Catch-up resolves the
    # latest turn and replays seq 6..9 → the high-water mark becomes 9.
    gen = _sse_stream(state, "sess-1", catch_up_turn_id=None, catch_up_sequence=5)

    async def _producer() -> None:
        for _ in range(50):
            if emitter.subscriber_count("sess-1") > 0:
                break
            await asyncio.sleep(0.02)
        # A brand-new turn whose sequences restart at 0 — all below the
        # high-water mark left by catch-up.
        for seq in range(3):
            await emitter.emit(
                _make_envelope(turn_id="turn-new", sequence=seq, text=f"new-{seq}")
            )

    producer = asyncio.create_task(_producer())
    try:
        body = await asyncio.wait_for(
            _drain_frames(gen, until_count_of="new-2", count=1), timeout=5.0
        )
    finally:
        producer.cancel()
        with contextlib_suppress():
            await producer
        await gen.aclose()

    frames = _parse_sse_frames(body)
    texts = [
        json.loads(f["data"])["payload"]["text"]
        for f in frames
        if f.get("event") == "TextDelta"
    ]
    # Catch-up still delivered the old turn's tail …
    assert "old-9" in texts
    # … and the new turn's early events survived despite low sequences.
    assert "new-0" in texts
    assert "new-1" in texts
    assert "new-2" in texts


@pytest.mark.asyncio
async def test_aclose_mid_catch_up_is_bounded(
    state: AdminState,
    journal: AgentJournal,
) -> None:
    """Regression for the CI 6-hour hang: abandoning the stream right
    after the first catch-up frame must close promptly. The old code
    yielded from inside ``journal.iter_events`` — ``aclose()`` then tore
    down the aiosqlite cursor mid-iteration, which could deadlock the DB
    worker thread. With the buffered replay, ``aclose`` lands on a plain
    list iteration and finishes immediately.
    """
    tid = await journal.begin_turn("sess-1", "hello")
    assert tid is not None
    for seq in range(50):
        await journal.append_event(
            _make_envelope(turn_id=str(tid), sequence=seq, text=f"old-{seq}")
        )

    gen = _sse_stream(
        state, "sess-1", catch_up_turn_id=str(tid), catch_up_sequence=0
    )
    # Pull exactly one frame, then abandon the stream mid-replay.
    first = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
    assert b"TextDelta" in first
    await asyncio.wait_for(gen.aclose(), timeout=5.0)


@pytest.mark.asyncio
async def test_catch_up_pages_deliver_every_event(
    state: AdminState,
    journal: AgentJournal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch-up replays the WHOLE backlog in bounded pages — with the
    page size lowered well below the backlog, every missed event is still
    delivered in order (no silent gap past a cap)."""
    monkeypatch.setattr(sessions_events, "CATCH_UP_PAGE_SIZE", 3)
    tid = await journal.begin_turn("sess-1", "hello")
    assert tid is not None
    for seq in range(10):
        await journal.append_event(
            _make_envelope(turn_id=str(tid), sequence=seq, text=f"old-{seq}")
        )

    # start_sequence=0 → iter_events yields sequence > 0 (events 1..9).
    gen = _sse_stream(
        state, "sess-1", catch_up_turn_id=str(tid), catch_up_sequence=0
    )
    frames: list[bytes] = []
    try:
        for _ in range(9):  # spans 3 pages of 3
            frames.append(await asyncio.wait_for(gen.__anext__(), timeout=5.0))
    finally:
        await asyncio.wait_for(gen.aclose(), timeout=5.0)

    parsed = _parse_sse_frames(b"".join(frames))
    seqs = [
        int(f["id"].split(":", 1)[1])
        for f in parsed
        if f.get("event") == "TextDelta"
    ]
    assert seqs == list(range(1, 10))  # all delivered, in order, across pages


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_keeps_connection_alive(
    state: AdminState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silent session triggers the ``:keepalive`` comment frame."""
    monkeypatch.setattr(sessions_events, "SSE_HEARTBEAT_SECONDS", 0.1)
    gen = _sse_stream(state, "sess-quiet", catch_up_turn_id=None, catch_up_sequence=-1)
    try:
        # SSE comment frames are ``: keepalive\n\n`` (leading space after
        # the colon is intentional — matches the wire format).
        body = await asyncio.wait_for(
            _drain_frames(gen, until_count_of=": keepalive", count=1),
            timeout=5.0,
        )
    finally:
        await gen.aclose()

    assert b": keepalive" in body


# ---------------------------------------------------------------------------
# Last-Event-ID parser
# ---------------------------------------------------------------------------


def test_parse_last_event_id_handles_composite_and_bare() -> None:
    assert _parse_last_event_id(None) == (None, -1)
    assert _parse_last_event_id("") == (None, -1)
    assert _parse_last_event_id("turn-1:5") == ("turn-1", 5)
    assert _parse_last_event_id("7") == (None, 7)
    # Malformed → no catch-up
    assert _parse_last_event_id("bogus:abc") == (None, -1)
    assert _parse_last_event_id("abc") == (None, -1)


# ---------------------------------------------------------------------------
# JSON replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_returns_events_paginated(
    app: FastAPI,
    journal: AgentJournal,
) -> None:
    """Replay paginates by ``limit`` with a non-null ``next_cursor``."""
    tid = await journal.begin_turn("sess-page", "x")
    assert tid is not None
    for seq in range(10):
        await journal.append_event(
            _make_envelope(turn_id=str(tid), sequence=seq, text=f"t-{seq}")
        )

    client = TestClient(app)
    resp = client.get(f"/admin/sessions/sess-page/turns/{tid}/events?limit=4")
    assert resp.status_code == 200
    body = resp.json()
    assert body["turn_id"] == str(tid)
    assert body["session_key"] == "sess-page"
    assert len(body["events"]) == 4
    assert [e["sequence"] for e in body["events"]] == [0, 1, 2, 3]
    assert body["next_cursor"] == 3

    resp2 = client.get(
        f"/admin/sessions/sess-page/turns/{tid}/events"
        f"?limit=4&after_sequence={body['next_cursor']}"
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert [e["sequence"] for e in body2["events"]] == [4, 5, 6, 7]


@pytest.mark.asyncio
async def test_replay_empty_turn_returns_empty_list(
    app: FastAPI,
    journal: AgentJournal,
) -> None:
    """An unknown / empty turn returns an empty event list (not a 404)."""
    client = TestClient(app)
    resp = client.get("/admin/sessions/sess-x/turns/0/events")
    assert resp.status_code == 200
    body = resp.json()
    assert body["events"] == []
    assert body["next_cursor"] is None


@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_replay_pagination_never_abandons_a_cursor(
    app: FastAPI,
    journal: AgentJournal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the CI deadlock this file was infamous for
    (pytest-timeout at 180s; historically a 6h-cap hang): the JSON
    replay route must never abandon a live aiosqlite cursor by
    ``break``-ing out of ``journal.iter_events``.

    Mechanism pinned here: under the sync ``TestClient`` every request
    runs on a throwaway anyio portal event loop. The old route broke out
    of a live iterator at ``limit`` (plus a one-row probe), leaving
    suspended generators holding open cursors. Their deferred
    ``cursor.close()`` reached the aiosqlite worker queue with a future
    bound to that portal loop; when the worker serviced the close after
    the loop was gone, ``call_soon_threadsafe`` raised
    ``RuntimeError: Event loop is closed`` *inside the worker's
    exception handler* — killing the worker thread. Every later journal
    call then awaited a future nobody resolves, and the next request
    deadlocked in ``portal.call`` → ``waiter.acquire()`` (the exact CI
    traceback).

    We make the race deterministic instead of hoping for CI scheduling:
    every cursor close is prefixed with a blocking sleep item on the
    worker queue, guaranteeing any *abandoned* close is serviced only
    after its portal loop closed. With the break-safe iterator and the
    LIMIT-pushdown route this test stays green; with the old code the
    worker dies after request 1 and request 2 times out.
    """
    import time

    import aiosqlite

    tid = await journal.begin_turn("sess-page", "x")
    assert tid is not None
    for seq in range(10):
        await journal.append_event(
            _make_envelope(
                turn_id=str(tid),
                session_key="sess-page",
                sequence=seq,
                text=f"t-{seq}",
            )
        )

    # White-box: reach the aiosqlite worker thread so we can both delay
    # it and assert it survived.
    conn = journal.backend._conn  # noqa: SLF001 — deliberate white-box
    worker = conn._thread  # noqa: SLF001
    real_close = aiosqlite.Cursor.close

    async def slow_close(self: aiosqlite.Cursor) -> None:
        # A ``(future=None, fn)`` item is legal: the worker just runs it.
        self._conn._tx.put_nowait(  # noqa: SLF001
            (None, lambda: time.sleep(0.25))
        )
        await real_close(self)

    monkeypatch.setattr(aiosqlite.Cursor, "close", slow_close)

    client = TestClient(app)

    def _page(after: int | None) -> Any:
        url = f"/admin/sessions/sess-page/turns/{tid}/events?limit=4"
        if after is not None:
            url += f"&after_sequence={after}"
        return client.get(url)

    # Request 1 paginates (limit=4 < 10-event backlog) — the old code
    # abandoned two cursors here. ``to_thread`` + ``wait_for`` so a
    # regression FAILS in seconds instead of wedging the whole suite.
    resp = await asyncio.wait_for(asyncio.to_thread(_page, None), timeout=20.0)
    assert resp.status_code == 200
    assert [e["sequence"] for e in resp.json()["events"]] == [0, 1, 2, 3]

    # Let the (deliberately late) worker drain any deferred closes — with
    # the old code this is where it died on the closed portal loop.
    await asyncio.sleep(1.0)
    assert worker.is_alive(), (
        "aiosqlite worker thread died — a route abandoned a live cursor "
        "whose deferred close landed on a closed portal event loop"
    )

    # Request 2 is the one that deadlocked in CI (dead worker = futures
    # nobody resolves → portal.call blocks forever).
    resp2 = await asyncio.wait_for(asyncio.to_thread(_page, 3), timeout=20.0)
    assert resp2.status_code == 200
    assert [e["sequence"] for e in resp2.json()["events"]] == [4, 5, 6, 7]


# ---------------------------------------------------------------------------
# Disabled (no journal / emitter)
# ---------------------------------------------------------------------------


def test_503_when_observability_disabled(tmp_path: Path) -> None:
    """No journal / emitter on AdminState → 503 envelope on both routes."""
    s = AdminState(data_dir=tmp_path)
    set_admin_state(s)
    try:
        application = FastAPI()
        application.include_router(sessions_events.router())
        application.dependency_overrides[require_admin] = lambda: None
        client = TestClient(application)

        resp_live = client.get("/admin/sessions/sess/events/live")
        assert resp_live.status_code == 503
        assert resp_live.json()["error"] == "observability_disabled"

        resp_replay = client.get("/admin/sessions/sess/turns/123/events")
        assert resp_replay.status_code == 503
        assert resp_replay.json()["detail"]["error"] == "observability_disabled"
    finally:
        set_admin_state(None)


# ---------------------------------------------------------------------------
# Surfacing TurnComplete payloads (used by the cost-fallback path).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_returns_turn_complete_payload_intact(
    app: FastAPI,
    journal: AgentJournal,
) -> None:
    """``TurnComplete`` events round-trip with their full payload."""
    tid = await journal.begin_turn("sess-c", "y")
    assert tid is not None
    env = EventEnvelope(
        turn_id=str(tid),
        session_key="sess-c",
        sequence=0,
        timestamp_ms=1_700_000_000_000,
        event=TurnComplete(
            finish_reason="stop",
            usage={"input_tokens": 100, "output_tokens": 50},
            elapsed_ms=1234,
            estimated_cost_usd=0.01,
            cost_status="estimated",
        ),
    )
    await journal.append_event(env)

    client = TestClient(app)
    resp = client.get(f"/admin/sessions/sess-c/turns/{tid}/events")
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["finish_reason"] == "stop"
    assert payload["usage"]["input_tokens"] == 100
    assert payload["estimated_cost_usd"] == 0.01
