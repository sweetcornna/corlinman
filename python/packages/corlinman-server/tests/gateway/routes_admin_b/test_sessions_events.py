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
