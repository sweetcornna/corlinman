"""gap-fill lane-session-prims — Session bundle + cancel.combine().

Covers gap ``loop-session-cancel-stubs``: the two formerly-stubbed
primitives in ``corlinman_agent.session`` and ``corlinman_agent.cancel``.
"""

from __future__ import annotations

import asyncio

import pytest
from corlinman_agent.cancel import combine, with_timeout
from corlinman_agent.session import Session

# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


def test_session_constructs_with_defaults() -> None:
    s = Session()
    assert s.session_key == ""
    assert s.trace is None
    assert s.messages == []
    assert s.pending_tools == []
    assert isinstance(s.cancel, asyncio.Event)
    assert s.is_cancelled() is False


def test_session_exposes_supplied_fields() -> None:
    ev = asyncio.Event()
    trace = object()
    msgs = [{"role": "user", "content": "hi"}]
    pending = [{"call_id": "c1", "tool": "shell"}]
    s = Session(
        session_key="sess-42",
        trace=trace,
        messages=msgs,
        pending_tools=pending,
        cancel=ev,
    )
    assert s.session_key == "sess-42"
    assert s.trace is trace
    assert s.messages is msgs
    assert s.pending_tools is pending
    assert s.cancel is ev


def test_session_is_cancelled_tracks_token() -> None:
    s = Session()
    assert s.is_cancelled() is False
    s.cancel.set()
    assert s.is_cancelled() is True


def test_session_default_events_are_independent() -> None:
    a = Session()
    b = Session()
    a.cancel.set()
    assert a.is_cancelled() is True
    assert b.is_cancelled() is False


# ---------------------------------------------------------------------------
# cancel.combine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_combine_fires_when_first_input_fires() -> None:
    e1 = asyncio.Event()
    e2 = asyncio.Event()
    combined = combine(e1, e2)
    assert combined.is_set() is False

    e1.set()
    await asyncio.wait_for(combined.wait(), timeout=1.0)
    assert combined.is_set() is True


@pytest.mark.asyncio
async def test_combine_fires_when_second_input_fires() -> None:
    e1 = asyncio.Event()
    e2 = asyncio.Event()
    combined = combine(e1, e2)
    assert combined.is_set() is False

    e2.set()
    await asyncio.wait_for(combined.wait(), timeout=1.0)
    assert combined.is_set() is True


@pytest.mark.asyncio
async def test_combine_prefires_when_input_already_set() -> None:
    e1 = asyncio.Event()
    e2 = asyncio.Event()
    e2.set()
    combined = combine(e1, e2)
    # No await needed — an already-fired input must short-circuit.
    assert combined.is_set() is True


@pytest.mark.asyncio
async def test_combine_with_no_inputs_never_fires() -> None:
    combined = combine()
    assert isinstance(combined, asyncio.Event)
    assert combined.is_set() is False
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(combined.wait(), timeout=0.05)


@pytest.mark.asyncio
async def test_combine_single_input_passthrough() -> None:
    e1 = asyncio.Event()
    combined = combine(e1)
    assert combined.is_set() is False
    e1.set()
    await asyncio.wait_for(combined.wait(), timeout=1.0)
    assert combined.is_set() is True


# ---------------------------------------------------------------------------
# with_timeout (regression — must stay intact)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_timeout_passes_through_fast_result() -> None:
    async def quick() -> int:
        return 7

    assert await with_timeout(quick(), seconds=1.0) == 7
