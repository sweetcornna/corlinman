"""Cancellation helpers — ``asyncio.timeout`` + ``CancelledError`` wrappers.

Mirrors ``corlinman-core::cancel::{combine, with_timeout}`` on the Rust side
(plan §8 A2). Used by the reasoning loop, provider adapters, and the
embedding router so a single cancel signal collapses every outstanding I/O.

:func:`combine` merges several :class:`asyncio.Event` sources into one
derived event that fires the moment *any* input fires (a fan-in of cancel
scopes); :func:`with_timeout` runs an awaitable under ``asyncio.timeout`` and
raises :class:`corlinman_providers.TimeoutError` on expiry.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable

import structlog
from corlinman_providers import TimeoutError as CorlinmanTimeoutError

logger = structlog.get_logger(__name__)

_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


def combine(*events: asyncio.Event) -> asyncio.Event:
    """Merge several cancel signals into one event firing on *any* input.

    Returns a fresh :class:`asyncio.Event` that is ``set()`` as soon as any
    of ``events`` becomes set. If one of the inputs is *already* set the
    combined event is returned pre-fired. With no inputs the result is an
    inert (never-set) event, so callers can treat "no cancel scopes" as
    "never cancelled" without a special case.

    Mirrors ``corlinman-core::cancel::combine``: a single derived signal that
    collapses every outstanding scope so one ``await combined.wait()`` covers
    them all.
    """
    combined = asyncio.Event()
    sources: tuple[asyncio.Event, ...] = tuple(events)

    # Fast path: if any source is already fired, surface it immediately and
    # skip spawning watcher tasks entirely.
    if any(ev.is_set() for ev in sources):
        combined.set()
        return combined

    if not sources:
        return combined

    async def _watch(source: asyncio.Event) -> None:
        try:
            await source.wait()
        except asyncio.CancelledError:
            raise
        else:
            combined.set()

    watchers: list[asyncio.Task[None]] = [
        asyncio.ensure_future(_watch(ev)) for ev in sources
    ]

    async def _reap() -> None:
        """Once the combined event fires, cancel any still-waiting watchers."""
        await combined.wait()
        for task in watchers:
            if not task.done():
                task.cancel()
        for task in watchers:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    reap_task = asyncio.ensure_future(_reap())
    _BACKGROUND_TASKS.add(reap_task)
    reap_task.add_done_callback(_BACKGROUND_TASKS.discard)
    return combined


async def with_timeout[T](awaitable: Awaitable[T], *, seconds: float) -> T:
    """Run ``awaitable`` under ``asyncio.timeout`` and translate timeouts.

    On expiry raises :class:`corlinman_providers.TimeoutError` so the
    agent-client can classify it as ``FailoverReason::Timeout``.
    """
    try:
        async with asyncio.timeout(seconds):
            return await awaitable
    except TimeoutError as exc:  # builtins.TimeoutError — the one asyncio.timeout raises
        raise CorlinmanTimeoutError(
            f"operation exceeded {seconds:.1f}s",
        ) from exc
