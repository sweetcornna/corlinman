"""v0.7.1 warm pool for the agent runtime.

OpenClaw inspired the surface: ``acquire(key)`` returns a warm handle
when one is available; otherwise it cold-spawns via a per-key factory
and counts the miss. ``release(handle)`` returns the entity to the
pool, subject to per-key warm cap. Idle eviction fires when the active
total presses on ``max_active``.

What this pool is for, in v0.7.1:

corlinman's Rust gateway talks gRPC to a long-running Python servicer;
chat sessions don't spawn fresh OS processes per request. The cold-
start cost lives instead in **provider SDK first-call setup** (httpx
client, auth, model schema validation) and in the **agent-card +
context assembler** initialisation that lazy-runs on first chat.

The pool is the abstraction that lets us amortise those costs:

- **Pre-warm** at servicer boot: the operator's most-used
  ``(provider_alias, model)`` keys get one warm provider instance ready
  before the first user request.
- **Acquire / release** per chat: existing warm provider is reused;
  the SDK's HTTP/2 connection pool stays warm across requests.
- **Eviction**: idle entries past ``max_active`` are evicted oldest-
  first so memory doesn't grow unbounded under churn.

The pool is provider-agnostic: it stores ``Any`` and the factory
decides what gets cached. Initial caller in v0.7.1 is the provider
resolver in :mod:`corlinman_server.agent_servicer`; later releases
may pool reasoning loops or context assemblers.

Structured-log observability uses ``structlog`` (matches the rest of
the Python side); Prometheus counters are deliberately not added here
because the Rust gateway already exports per-chat latency that
operators monitor. Reach for prometheus_client only when an explicit
operator request lands.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING, Any, TypeVar

import structlog

if TYPE_CHECKING:
    # The agent package owns the typed ``EventEmitter`` protocol. Import
    # behind ``TYPE_CHECKING`` so the runner-pool module stays
    # importable even when ``corlinman-agent`` isn't installed (e.g.
    # during a stand-alone pool unit test).
    from corlinman_agent.events import EventEmitter

logger = structlog.get_logger(__name__)


# W3.1 — tool dispatch observability constants.
#
# Heartbeat cadence (seconds) for in-flight tools. opencode uses a 10s
# SSE heartbeat baseline; we match so the channel adapters can refresh
# their spinner with elapsed-time text on long-running shell / web /
# subagent calls. Tools that finish faster than this never spawn a
# heartbeat task — the after-emit path cancels it before the first
# tick fires.
TOOL_HEARTBEAT_INTERVAL_S: float = 10.0

#: Cap on the ``args_json`` field of :class:`ToolStateRunning`. The
#: reasoning loop already truncates large arg blocks, but a paranoid
#: dispatcher cap means a 2 MB blob from a misbehaving tool can't
#: blow the SSE / journal payload size. 64 KB matches the plan §1.1
#: spec for the live event stream.
_DISPATCH_ARGS_CAP: int = 64 * 1024

#: Cap on :class:`ToolStateCompleted.result_summary`. Mirrors the
#: reasoning-loop's own 4 KB cap so the wire shape stays uniform.
_DISPATCH_RESULT_CAP: int = 4_000


_TruncateMarker = "\n…[truncated]…\n"


def _truncate_for_event(value: str, cap: int) -> str:
    """Return ``value`` clamped to ``cap`` chars with a head+tail slice.

    Pure helper for :func:`dispatch_with_observability`. Strings at or
    below ``cap`` pass through unchanged. Larger inputs keep the leading
    ``cap // 2`` and trailing ``cap // 2`` chars with a sentinel marker
    in between so a viewer can see both the call shape and the failure
    tail.
    """
    if len(value) <= cap:
        return value
    half = cap // 2
    return f"{value[:half]}{_TruncateMarker}{value[-half:]}"


_T = TypeVar("_T")


PoolKey = tuple[str, str]
"""``(group, sub_key)`` — for the v0.7.1 provider case, that's
``(provider_alias, model)``. Generic enough to repurpose for later
pooled resources without redesigning the key shape."""


@dataclass
class PoolStats:
    """Per-pool counters + warm gauge. Read via :meth:`RunnerPool.stats`;
    mutate via the pool's internal hooks only. ``warm_age_seconds``
    surfaces the oldest warm entry's age so an operator can spot
    pool-stagnation regressions."""

    hits: int = 0
    misses: int = 0
    evictions: int = 0
    warm_count: int = 0
    warm_age_seconds: float = 0.0


@dataclass
class _Entry[T]:
    """One pool entry. ``last_idle_at`` is reset on each release so
    eviction can pick the oldest-idle. ``created_at`` is fixed at the
    cold-spawn moment for warm-age accounting."""

    value: T
    key: PoolKey
    created_at: float = field(default_factory=time.monotonic)
    last_idle_at: float = field(default_factory=time.monotonic)


@dataclass
class RunnerHandle[T]:
    """Drop-guard returned by :meth:`RunnerPool.acquire`. Hold while
    the caller is using the resource; call :meth:`release` (or use
    via the context-manager protocol) to return it to the pool.

    Re-using a released handle is a programmer error; the pool's
    internal accounting would double-release. ``_released`` short-
    circuits the path so explicit ``release()`` + scope-exit don't
    decrement twice.
    """

    key: PoolKey
    value: T
    _pool: RunnerPool[T]
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._pool._return(self.key, self.value)

    def __enter__(self) -> T:
        return self.value

    def __exit__(self, *_exc: object) -> None:
        self.release()


class RunnerPool[T]:
    """Bounded warm pool. Thread-safe under the internal lock; safe to
    share across asyncio coroutines because Python sqlite3-style
    coarse locking is enough for the rate the pool operates at (one
    acquire per chat request).

    Parameters
    ----------
    max_warm_per_key
        How many entries to keep warm under a single key. The pool
        never warms more than this; release-after-full evicts.
    max_active_total
        Cap on warm entries across *all* keys. When pressed, the
        oldest-idle entry (any key) is evicted.
    """

    def __init__(
        self,
        *,
        max_warm_per_key: int = 2,
        max_active_total: int = 8,
        event_emitter: "EventEmitter | None" = None,
    ) -> None:
        if max_warm_per_key < 1 or max_active_total < 1:
            raise ValueError("pool caps must be positive")
        if max_warm_per_key > max_active_total:
            raise ValueError(
                "max_warm_per_key cannot exceed max_active_total"
            )
        self._max_warm_per_key = max_warm_per_key
        self._max_active_total = max_active_total
        # Per-key LRU of warm entries. The outer dict is keyed by the
        # PoolKey; the inner OrderedDict carries one entry per warm
        # slot, ordered oldest-first so eviction is a popitem(last=False)
        # away. (We use the entry's id() as the inner key so the same
        # value never collides with itself.)
        self._warm: dict[PoolKey, OrderedDict[int, _Entry[T]]] = {}
        self._lock = Lock()
        self._stats = PoolStats()
        # W1.3 — typed observability emitter the gateway lifecycle
        # constructs once and shares with every pool / reasoning loop.
        # Stored here as a plain field so a future W3.1 patch can emit
        # ``ToolStateRunning`` / ``ToolStateHeartbeat`` /
        # ``ToolStateCompleted`` from within the pool without revisiting
        # the constructor (or the gateway-lifecycle wiring). ``None``
        # means "no observability sink wired" — every existing call
        # site keeps working unchanged.
        self._event_emitter: "EventEmitter | None" = event_emitter

    # ─── Public surface ────────────────────────────────────────────

    def acquire(self, key: PoolKey, factory: Callable[[], T]) -> RunnerHandle[T]:
        """Return a warm entry under ``key``, cold-spawning via
        ``factory`` if none is available. ``factory`` runs *outside*
        the lock so a slow-to-construct provider doesn't stall other
        coroutines acquiring different keys.
        """
        with self._lock:
            entries = self._warm.get(key)
            if entries:
                # LIFO pick — the most-recently released is also the
                # most-likely-to-have-warm-connections. Pop from end.
                _, entry = entries.popitem(last=True)
                if not entries:
                    del self._warm[key]
                self._stats.hits += 1
                logger.debug(
                    "runner_pool.hit",
                    key=key,
                    age_seconds=time.monotonic() - entry.created_at,
                )
                self._refresh_warm_count_unlocked()
                return RunnerHandle(key=key, value=entry.value, _pool=self)
            # Miss — cold-spawn outside the lock.
            self._stats.misses += 1
        value = factory()
        logger.info("runner_pool.miss_cold_spawn", key=key)
        return RunnerHandle(key=key, value=value, _pool=self)

    def prewarm(self, key: PoolKey, factory: Callable[[], T]) -> None:
        """Cold-spawn one entry under ``key`` and park it warm. Used
        at servicer boot for the operator's most-used aliases.

        If the per-key warm cap is already at the limit, the call is a
        no-op (idempotent). Honours ``max_active_total`` by evicting
        oldest-idle if necessary; the freshly-warmed entry always
        wins the slot.
        """
        value = factory()
        entry = _Entry(value=value, key=key)
        with self._lock:
            entries = self._warm.setdefault(key, OrderedDict())
            if len(entries) >= self._max_warm_per_key:
                return
            self._enforce_active_cap_unlocked()
            entries[id(entry)] = entry
            logger.info("runner_pool.prewarmed", key=key)
            self._refresh_warm_count_unlocked()

    def stats(self) -> PoolStats:
        """Snapshot of current pool counters. The values are copied so
        callers don't observe mid-mutation state."""
        with self._lock:
            self._refresh_warm_count_unlocked()
            return PoolStats(
                hits=self._stats.hits,
                misses=self._stats.misses,
                evictions=self._stats.evictions,
                warm_count=self._stats.warm_count,
                warm_age_seconds=self._stats.warm_age_seconds,
            )

    # ─── Internal: handle re-entry on release ──────────────────────

    def _return(self, key: PoolKey, value: T) -> None:
        """Called by :meth:`RunnerHandle.release`. If pool is full,
        the entry is dropped (caller's reference is the last one)."""
        entry = _Entry(value=value, key=key)
        with self._lock:
            entries = self._warm.setdefault(key, OrderedDict())
            if len(entries) >= self._max_warm_per_key:
                logger.debug("runner_pool.dropped_full_per_key", key=key)
                if not entries:
                    del self._warm[key]
                return
            self._enforce_active_cap_unlocked()
            entry.last_idle_at = time.monotonic()
            entries[id(entry)] = entry
            self._refresh_warm_count_unlocked()

    def _enforce_active_cap_unlocked(self) -> None:
        """If adding one more warm entry would breach ``max_active_total``,
        evict the oldest-idle entry (across all keys) to make room.
        Caller must hold ``self._lock``.
        """
        current = sum(len(v) for v in self._warm.values())
        if current < self._max_active_total:
            return
        oldest_key: PoolKey | None = None
        oldest_entry_id: int | None = None
        oldest_idle = float("inf")
        for k, entries in self._warm.items():
            for eid, entry in entries.items():
                if entry.last_idle_at < oldest_idle:
                    oldest_idle = entry.last_idle_at
                    oldest_key = k
                    oldest_entry_id = eid
        if oldest_key is not None and oldest_entry_id is not None:
            entries = self._warm[oldest_key]
            entries.pop(oldest_entry_id, None)
            if not entries:
                del self._warm[oldest_key]
            self._stats.evictions += 1
            logger.info("runner_pool.evicted_oldest_idle", key=oldest_key)

    def _refresh_warm_count_unlocked(self) -> None:
        total = 0
        oldest_age = 0.0
        now = time.monotonic()
        for entries in self._warm.values():
            for entry in entries.values():
                total += 1
                age = now - entry.created_at
                if age > oldest_age:
                    oldest_age = age
        self._stats.warm_count = total
        self._stats.warm_age_seconds = oldest_age


@dataclass(slots=True)
class DispatchContext:
    """Correlation data the tool dispatcher needs to emit observability.

    Plumbed through the dispatch entry points (``_dispatch_builtin`` in
    the agent servicer, ``executor.execute`` in chat_service) so the
    emitter can stamp the right ``turn_id`` / ``session_key`` on every
    :class:`ToolStateRunning` / :class:`ToolStateHeartbeat` /
    :class:`ToolStateCompleted` envelope.

    Carries the emitter directly (rather than reading it off
    :class:`RunnerPool`) so the helper is callable from any module
    without a circular import and so unit tests can hand in a
    :class:`MockEventEmitter` without constructing a full pool.

    Construction:

    * ``emitter`` — ``None`` collapses the helper into a thin pass-
      through (the wrapped coroutine still runs; no envelopes are
      emitted). Existing call sites that aren't wired into observability
      keep working unchanged.
    * ``turn_id`` / ``session_key`` — correlation pair the reasoning
      loop already stamps on its own envelopes. The dispatcher reuses
      both so the SSE / journal consumer sees one ordered stream per
      turn.
    """

    turn_id: str
    session_key: str
    emitter: "EventEmitter | None" = None


async def dispatch_with_observability(
    ctx: DispatchContext,
    *,
    tool_call_id: str,
    tool_name: str,
    args_json: str | bytes,
    invoke: Callable[[], Awaitable[_T]],
    summarise_result: Callable[[_T], tuple[str, bool]] = (
        lambda r: (str(r), False)
    ),
    heartbeat_interval_s: float = TOOL_HEARTBEAT_INTERVAL_S,
) -> _T:
    """Wrap a tool-dispatch coroutine with the W3.1 state-machine emits.

    Emits, in order:

    1. :class:`corlinman_agent.events.ToolStateRunning` just before
       ``invoke()`` is awaited (records wall-clock ``started_at_ms``);
    2. :class:`corlinman_agent.events.ToolStateHeartbeat` every
       ``heartbeat_interval_s`` seconds while ``invoke()`` is running —
       a background task that cancels itself on completion (so fast
       tools never see a heartbeat at all);
    3. :class:`corlinman_agent.events.ToolStateCompleted` after
       ``invoke()`` returns, with the elapsed ms and a truncated
       result summary derived from ``summarise_result(result)``.

    The wrapped coroutine's return value is forwarded verbatim; the
    helper is transparent on success.

    On exception inside ``invoke()`` the helper still emits a
    :class:`ToolStateCompleted` with ``is_error=True`` and the
    exception's stringified message as the summary, then re-raises so
    the caller's existing error-handling path is unchanged.

    Heartbeat lifecycle:
        The heartbeat task lives as long as the dispatch task. We hand
        it ``shield=True`` indirectly (via the ``try/finally`` block) so
        a cancellation of the outer task tears the heartbeat down
        cleanly. Cancellation of the heartbeat itself is swallowed —
        :class:`asyncio.CancelledError` is the expected shutdown
        signal.

    Why not a context manager:
        The dispatcher call sites need the helper to await a single
        coroutine and forward the return value; a contextmanager would
        force a re-entrant ``async with`` pattern at every call site.
        The function form keeps the diff at each call site to a
        one-line wrap.
    """
    emitter = ctx.emitter
    if emitter is None:
        # Observability not wired — degrade to a pass-through so the
        # existing dispatch flow doesn't change at all.
        return await invoke()

    started_at_ms = time.time_ns() // 1_000_000
    started_monotonic_ns = time.monotonic_ns()

    # Lazy-import to avoid a top-level corlinman-agent dependency. This
    # module is importable in isolation (the warm-pool unit tests don't
    # need corlinman-agent installed) and the dispatch helper is the
    # only consumer.
    from corlinman_agent.events import (
        ToolStateCompleted,
        ToolStateHeartbeat,
        ToolStateRunning,
    )

    args_repr = (
        args_json.decode("utf-8", errors="replace")
        if isinstance(args_json, (bytes, bytearray))
        else str(args_json)
    )
    args_repr = _truncate_for_event(args_repr, _DISPATCH_ARGS_CAP)

    await emitter.emit_event(
        ctx.turn_id,
        ctx.session_key,
        ToolStateRunning(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            args_json=args_repr,
            started_at_ms=started_at_ms,
        ),
    )

    async def _heartbeat_loop() -> None:
        try:
            while True:
                await asyncio.sleep(heartbeat_interval_s)
                elapsed_ms = (time.monotonic_ns() - started_monotonic_ns) // 1_000_000
                await emitter.emit_event(  # type: ignore[union-attr]
                    ctx.turn_id,
                    ctx.session_key,
                    ToolStateHeartbeat(
                        tool_call_id=tool_call_id,
                        elapsed_ms=elapsed_ms,
                        stdout_tail=None,
                    ),
                )
        except asyncio.CancelledError:
            # Expected — the dispatch task cancels us on completion.
            return

    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(),
        name=f"runner_pool.heartbeat.{tool_call_id}",
    )

    is_error = False
    # ``result_for_summary`` is assigned exactly once on the success path
    # (via ``await invoke()``) before any read; the error / cancel paths
    # re-raise without reading it. Typed via ``_T`` so the return is
    # transparent to callers.
    result_for_summary: _T
    summary_text: str = ""
    try:
        result_for_summary = await invoke()
    except asyncio.CancelledError:
        # Cancellation tears down the heartbeat task in the finally
        # below; we re-raise so the surrounding stream stops. We do
        # NOT emit ToolStateCompleted — Cancelling has already (or
        # will be) fired via the reasoning loop's cancel path.
        heartbeat_task.cancel()
        raise
    except Exception as exc:  # noqa: BLE001 — surface as is_error
        is_error = True
        summary_text = _truncate_for_event(str(exc), _DISPATCH_RESULT_CAP)
        # Emit completion before re-raising so the SSE / journal sees
        # the failure even if the caller logs and continues.
        elapsed_ms = (time.monotonic_ns() - started_monotonic_ns) // 1_000_000
        heartbeat_task.cancel()
        try:
            await emitter.emit_event(
                ctx.turn_id,
                ctx.session_key,
                ToolStateCompleted(
                    tool_call_id=tool_call_id,
                    result_summary=summary_text,
                    result_json_ref=None,
                    elapsed_ms=elapsed_ms,
                    is_error=is_error,
                ),
            )
        finally:
            # Drain the cancellation so it doesn't leak as "Task was
            # destroyed but it is pending!" on async loops.
            try:
                await heartbeat_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        raise
    else:
        # Success — derive a summary from the result and emit.
        try:
            raw_summary, is_error = summarise_result(result_for_summary)
        except Exception as exc:  # noqa: BLE001 — never crash the dispatch on a summariser bug
            logger.warning(
                "runner_pool.dispatch_summarise_failed",
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                error=str(exc),
            )
            raw_summary = ""
            is_error = False
        summary_text = _truncate_for_event(raw_summary or "", _DISPATCH_RESULT_CAP)
    finally:
        # Cancel the heartbeat task (no-op if it's already done) and
        # drain it so asyncio doesn't warn about pending tasks.
        if not heartbeat_task.done():
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    elapsed_ms = (time.monotonic_ns() - started_monotonic_ns) // 1_000_000
    await emitter.emit_event(
        ctx.turn_id,
        ctx.session_key,
        ToolStateCompleted(
            tool_call_id=tool_call_id,
            result_summary=summary_text,
            result_json_ref=None,
            elapsed_ms=elapsed_ms,
            is_error=is_error,
        ),
    )
    return result_for_summary


__all__ = [
    "DispatchContext",
    "PoolKey",
    "PoolStats",
    "RunnerHandle",
    "RunnerPool",
    "TOOL_HEARTBEAT_INTERVAL_S",
    "dispatch_with_observability",
]
