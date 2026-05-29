"""`HookBus` + `HookSubscription`.

Mirrors ``rust/crates/corlinman-hooks/src/bus.rs``.

Internally, the bus holds a list of per-subscriber :class:`asyncio.Queue`
instances, one set per priority tier. :meth:`HookBus.emit` publishes in
strict tier order (Critical -> Normal -> Low) so Critical subscribers
always observe an event before lower-priority ones. Each tier is
awaited via :func:`asyncio.sleep(0)` between tiers to give subscribers
a scheduling opportunity, matching the Rust ``tokio::task::yield_now``
semantics.

The Rust crate is built on ``tokio::sync::broadcast``: every active
subscriber receives every published event, and slow subscribers see a
``Lagged(n)`` error when they fall behind. We replicate that by giving
each subscriber its own bounded queue and dropping the oldest item +
incrementing a per-subscriber "missed" counter when the queue is full.
The next :meth:`HookSubscription.recv` call surfaces the counter as a
:class:`Lagged` exception, then resumes normal delivery.
"""

from __future__ import annotations

import asyncio
import inspect
import itertools
import logging
import weakref
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Union

from corlinman_hooks.error import Closed, HookCancelledError, Lagged
from corlinman_hooks.priority import CancelToken, HookPriority

if TYPE_CHECKING:
    from corlinman_hooks.event import _HookEventBase

__all__ = [
    "HookBus",
    "HookSubscription",
    "SubscriptionToken",
    "match_kind",
]

_log = logging.getLogger("corlinman.hooks.bus")

# Type aliases for the push-based callable-subscriber surface.
HookPredicate = Callable[["_HookEventBase"], bool]
HookSubscriber = Callable[["_HookEventBase"], Union[Awaitable[None], None]]


@dataclass(frozen=True)
class SubscriptionToken:
    """Opaque handle returned by :meth:`HookBus.subscribe` (push-based form).

    Pass it back to :meth:`HookBus.unsubscribe` to detach the subscriber.
    Holding the token does not keep the subscriber alive — the bus
    retains the subscriber directly; the token is only an id.
    """

    id: int


def match_kind(*kinds: str) -> HookPredicate:
    """Build a predicate matching ``HookEvent.kind()`` against ``kinds``.

    Convenience for the common case::

        bus.subscribe(match_kind("ToolCalled", "PreToolDispatch"), my_subscriber)

    ``kinds`` is matched against the snake_case discriminant returned by
    :meth:`HookEvent.kind` (e.g. ``"tool_called"``) *and* the PascalCase
    variant name (e.g. ``"ToolCalled"``) so callers can use either form.
    Empty ``kinds`` returns a predicate that matches everything — useful
    for a wildcard subscriber that wants every event on the bus.
    """
    if not kinds:
        return lambda _ev: True
    accepted = frozenset(kinds)

    def _predicate(ev: _HookEventBase) -> bool:
        if ev.kind() in accepted:
            return True
        variant = getattr(ev, "VARIANT_NAME", "")
        return bool(variant) and variant in accepted

    return _predicate


class HookSubscription:
    """A handle to one priority tier of the bus.

    Dropping it removes the slot from the bus's per-tier subscriber
    list (via a finaliser); other subscribers and the emitter are
    unaffected.

    The internal buffer is a bounded :class:`collections.deque` paired
    with an :class:`asyncio.Event` to signal availability. The deque is
    capped at the bus's ``capacity``; when the emitter publishes into
    a full buffer it discards the oldest event and increments
    :attr:`_lag`. The next :meth:`recv` call observes the non-zero lag
    and returns it as :class:`Lagged` before resuming normal delivery.
    """

    __slots__ = (
        "__weakref__",
        "_buffer",
        "_capacity",
        "_closed",
        "_event",
        "_lag",
        "_priority",
    )

    def __init__(
        self,
        priority: HookPriority,
        capacity: int,
        bus: HookBus,
    ) -> None:
        # The ``bus`` arg is accepted for symmetry with the Rust
        # constructor signature (subscription needs to know which bus
        # it belongs to in the Rust version because the broadcast
        # receiver is bound to a specific sender). The Python side does
        # the bookkeeping through the bus's own weak-ref list; we
        # don't need to retain a back-reference here.
        del bus
        self._priority = priority
        self._capacity = capacity
        self._buffer: deque[_HookEventBase] = deque()
        self._event = asyncio.Event()
        self._lag = 0
        self._closed = False

    @property
    def priority(self) -> HookPriority:
        return self._priority

    def _push(self, event: _HookEventBase) -> None:
        """Bus-internal: push an event into this subscriber's buffer.

        On overflow we drop the oldest item to match the
        ``broadcast::channel`` policy (slow subscribers see ``Lagged``
        rather than back-pressuring the emitter).
        """
        if self._closed:
            return
        if len(self._buffer) >= self._capacity:
            self._buffer.popleft()
            self._lag += 1
        self._buffer.append(event)
        self._event.set()

    def _close(self) -> None:
        """Bus-internal: mark this subscription closed (the bus is gone)."""
        self._closed = True
        self._event.set()

    async def recv(self) -> _HookEventBase:
        """Await the next event on this tier.

        Raises :class:`Lagged` if the subscriber fell behind since the
        last successful ``recv``. Raises :class:`Closed` if the bus
        has been garbage-collected and no events remain buffered.
        """
        if self._lag > 0:
            lag = self._lag
            self._lag = 0
            raise Lagged(lag)
        while not self._buffer:
            if self._closed:
                raise Closed()
            self._event.clear()
            # Re-check after clearing to avoid lost wakeups.
            if self._buffer or self._closed:
                continue
            await self._event.wait()
        return self._buffer.popleft()


class HookBus:
    """Cross-cutting event bus.

    Cheap to share: every subscriber holds its own bounded buffer, and
    the bus only retains weak references back to each subscription so
    dropped subscribers are GC'd automatically.

    Mirrors the Rust ``HookBus``. The Rust crate is ``Clone`` because
    the per-tier broadcast senders are themselves cloneable; in Python
    sharing the same instance by reference plays the same role.
    """

    # Default capacity when ``HookBus()`` is called with no args. Sized
    # generously so the typical "one subscriber per cross-cutting consumer"
    # workload (admin live feed, classifier, audit log) doesn't lag.
    DEFAULT_CAPACITY: int = 256

    __slots__ = (
        "_callable_subs",
        "_cancel",
        "_capacity",
        "_next_token_id",
        "_pending_tasks",
        "_subscribers",
    )

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        self._capacity = capacity
        # Per-tier list of weak refs to live subscriptions. Using weak
        # refs lets a subscriber's `__del__` implicitly remove it
        # without needing an explicit unsubscribe call.
        self._subscribers: dict[HookPriority, list[weakref.ReferenceType[HookSubscription]]] = {
            HookPriority.CRITICAL: [],
            HookPriority.NORMAL: [],
            HookPriority.LOW: [],
        }
        self._cancel = CancelToken()
        # Push-based callable subscribers: token id -> (predicate, subscriber).
        # Separate from the tokio-broadcast-style per-tier pull queues above
        # so the two delivery surfaces don't interfere. Both are fed by a
        # single :meth:`emit` call.
        self._callable_subs: dict[int, tuple[HookPredicate, HookSubscriber]] = {}
        self._next_token_id = itertools.count(1)
        # Strong-ref holder for fire-and-forget tasks scheduled from
        # :meth:`_fanout_callables_sync`. asyncio only weakly references
        # tasks (see
        # https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task);
        # without this, a busy loop can GC a scheduled async subscriber
        # mid-flight and silently drop the hook delivery. Same idiom
        # used in ``corlinman_channels.service`` and
        # ``native_upgrader`` (R2-003).
        self._pending_tasks: set[asyncio.Task[None]] = set()

    @property
    def capacity(self) -> int:
        return self._capacity

    def cancel_token(self) -> CancelToken:
        """Reference to the bus-wide cancel token.

        Flipping it stops future :meth:`emit` calls from publishing.
        Returned by reference (not a copy) so callers can either flip
        it themselves or hand the same instance to a shutdown signal.
        """
        return self._cancel

    def receiver_count(self, priority: HookPriority) -> int:
        """Number of live subscribers on ``priority``."""
        self._compact(priority)
        return len(self._subscribers[priority])

    def _compact(self, priority: HookPriority) -> None:
        """Drop dead weak refs from ``priority``'s subscriber list."""
        live = [ref for ref in self._subscribers[priority] if ref() is not None]
        self._subscribers[priority] = live

    def subscribe(
        self,
        priority_or_predicate: HookPriority | HookPredicate,
        subscriber: HookSubscriber | None = None,
    ) -> HookSubscription | SubscriptionToken:
        """Subscribe to the bus.

        Two call shapes are supported:

        * ``subscribe(priority)`` — legacy tokio-broadcast-style pull
          surface. Returns a :class:`HookSubscription` whose ``recv()``
          coroutine yields the next matching event. Subscribers see
          *every* event published on their priority tier.
        * ``subscribe(predicate, subscriber)`` — push surface. The
          ``predicate`` is invoked synchronously on every emitted event;
          when it returns truthy, ``subscriber`` is invoked with the
          event. ``subscriber`` may be sync (returns ``None``) or async
          (returns an :class:`Awaitable`). Returns a
          :class:`SubscriptionToken` to pass back to
          :meth:`unsubscribe`.

        The two paths are independent: events emitted via :meth:`emit`
        are fanned out to *both* the per-tier pull queues *and* every
        matching callable subscriber.
        """
        # Push-based: ``(predicate, subscriber)``.
        if callable(priority_or_predicate) and not isinstance(
            priority_or_predicate, HookPriority
        ):
            if subscriber is None:
                raise TypeError(
                    "subscribe(predicate, subscriber) requires a subscriber callable"
                )
            if not callable(subscriber):
                raise TypeError("subscriber must be callable")
            token_id = next(self._next_token_id)
            self._callable_subs[token_id] = (priority_or_predicate, subscriber)
            return SubscriptionToken(id=token_id)

        # Pull-based legacy path.
        if not isinstance(priority_or_predicate, HookPriority):
            raise TypeError(
                "subscribe expects either a HookPriority or (predicate, subscriber)"
            )
        if subscriber is not None:
            raise TypeError(
                "subscribe(priority) does not accept a second positional argument"
            )
        priority = priority_or_predicate
        sub = HookSubscription(priority, self._capacity, self)
        self._subscribers[priority].append(weakref.ref(sub))
        return sub

    def unsubscribe(self, token: SubscriptionToken) -> None:
        """Detach a push-based callable subscriber.

        Idempotent — calling with a token that was never registered (or
        already unsubscribed) is a no-op rather than an error so shutdown
        paths can ``unsubscribe`` unconditionally without bookkeeping.
        """
        if not isinstance(token, SubscriptionToken):
            return
        self._callable_subs.pop(token.id, None)

    def subscriber_count(self) -> int:
        """Number of live push-based callable subscribers.

        Useful for tests + admin introspection. The tier-pull subscriber
        count is reported per-tier by :meth:`receiver_count`.
        """
        return len(self._callable_subs)

    def _fanout_tier(self, priority: HookPriority, event: _HookEventBase) -> None:
        """Push ``event`` into every live subscriber on ``priority``.

        Dead weak refs are collected lazily — a single pass either
        resolves the ref to push or appends nothing, then we compact at
        the end. Matching the Rust ``broadcast::send`` semantics, a
        tier with no subscribers is a no-op.
        """
        survivors: list[weakref.ReferenceType[HookSubscription]] = []
        for ref in self._subscribers[priority]:
            sub = ref()
            if sub is None:
                continue
            sub._push(event)
            survivors.append(ref)
        self._subscribers[priority] = survivors

    async def emit(self, event: _HookEventBase) -> None:
        """Emit in strict priority order, then fan out to callable subscribers.

        Raises :class:`HookCancelledError` if the cancel token has been
        flipped by the time we start (or between any two tiers). Having
        no subscribers on a tier is not an error; the send is a no-op.

        Between tiers we ``await asyncio.sleep(0)`` so subscribers on
        the just-published tier can drain before we publish to the next
        tier. This is what enforces the ordering guarantee on a
        single-threaded asyncio runtime.

        After the tiered pull-fanout completes, every matching
        :meth:`subscribe` callable also observes the event. A subscriber
        that raises is logged and isolated — neither the producer nor
        the other subscribers see the failure.
        """
        if self._cancel.is_cancelled():
            raise HookCancelledError()
        for tier in HookPriority.ordered():
            if self._cancel.is_cancelled():
                raise HookCancelledError()
            self._fanout_tier(tier, event)
            # Yield so subscribers on this tier can drain before we
            # publish to the next tier.
            await asyncio.sleep(0)
        await self._fanout_callables(event)

    def emit_nonblocking(self, event: _HookEventBase) -> None:
        """Fire-and-forget variant. Never awaits.

        Useful from sync contexts (e.g. atexit, config-reload
        callbacks) where blocking on scheduler yields isn't possible.
        Skips the per-tier yield: all three tiers are fanned out
        immediately, so the strict per-tier observation ordering is
        not guaranteed from this entry point. Mirrors the Rust
        ``emit_nonblocking`` semantics.

        Push-based callable subscribers are also invoked, but async
        subscribers are scheduled as fire-and-forget tasks on the
        running loop (or skipped with a log line when no loop is
        available, since we can't ``await`` from a sync caller).
        """
        if self._cancel.is_cancelled():
            return
        for tier in HookPriority.ordered():
            self._fanout_tier(tier, event)
        self._fanout_callables_sync(event)

    # ------------------------------------------------------------------
    # Push-based callable-subscriber fan-out.
    # ------------------------------------------------------------------

    async def _fanout_callables(self, event: _HookEventBase) -> None:
        """Invoke every matching callable subscriber, awaiting async ones.

        Each subscriber is isolated: a raise (or a returned awaitable
        that raises) is logged and dropped — the next subscriber still
        runs. We snapshot the subscriber map up front so an
        ``unsubscribe`` from inside a callback doesn't perturb the
        in-flight iteration.
        """
        for predicate, subscriber in list(self._callable_subs.values()):
            try:
                if not predicate(event):
                    continue
            except Exception:  # noqa: BLE001 — never let a predicate kill emit
                _log.exception("hook subscriber predicate raised; dropping event for this sub")
                continue
            try:
                result = subscriber(event)
            except Exception:  # noqa: BLE001 — isolate sync subscriber failure
                _log.exception("hook subscriber raised; isolated")
                continue
            if result is None:
                continue
            if inspect.isawaitable(result):
                try:
                    await result
                except Exception:  # noqa: BLE001 — isolate async subscriber failure
                    _log.exception("hook subscriber coroutine raised; isolated")

    def _fanout_callables_sync(self, event: _HookEventBase) -> None:
        """Sync-context variant of :meth:`_fanout_callables`.

        Async subscribers are scheduled with :func:`asyncio.ensure_future`
        when a running loop is available; otherwise they are skipped
        with a warning. Sync subscribers run inline with the same
        per-subscriber exception isolation as the async path.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        for predicate, subscriber in list(self._callable_subs.values()):
            try:
                if not predicate(event):
                    continue
            except Exception:  # noqa: BLE001
                _log.exception("hook subscriber predicate raised; dropping event for this sub")
                continue
            try:
                result = subscriber(event)
            except Exception:  # noqa: BLE001
                _log.exception("hook subscriber raised; isolated")
                continue
            if result is None:
                continue
            if inspect.isawaitable(result):
                if loop is None:
                    _log.warning(
                        "emit_nonblocking: async subscriber returned awaitable but "
                        "no running event loop is available; dropping"
                    )
                    # Close the coroutine to avoid the "coroutine was never
                    # awaited" runtime warning.
                    close = getattr(result, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:  # noqa: BLE001
                            pass
                    continue
                t = loop.create_task(self._await_isolated(result))
                self._pending_tasks.add(t)
                t.add_done_callback(self._pending_tasks.discard)

    @staticmethod
    async def _await_isolated(awaitable: Awaitable[Any]) -> None:
        """Await ``awaitable`` and swallow any exception (logged)."""
        try:
            await awaitable
        except Exception:  # noqa: BLE001
            _log.exception("hook subscriber coroutine raised (scheduled); isolated")

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        counts = {p.value: self.receiver_count(p) for p in HookPriority.ordered()}
        return f"HookBus(capacity={self._capacity}, subscribers={counts})"


# Backwards-friendly alias for the registration vocabulary used in the
# port spec ("register_hook"). The bus's subscription model is the
# Python-native form of "register a hook listener"; this helper makes
# the call site read more like the Rust crate's documentation prose.
def register_hook(bus: HookBus, priority: HookPriority = HookPriority.NORMAL) -> HookSubscription:
    """Register a hook listener at ``priority`` and return its subscription.

    Equivalent to ``bus.subscribe(priority)``. Provided to satisfy the
    "register / unregister" vocabulary in the port spec; the unregister
    side is implicit — drop the returned :class:`HookSubscription` and
    the bus's weak-ref-based bookkeeping cleans up on the next
    :meth:`HookBus.emit` or :meth:`HookBus.receiver_count` call.
    """
    return bus.subscribe(priority)


__all__ = [
    "HookBus",
    "HookSubscription",
    "SubscriptionToken",
    "match_kind",
    "register_hook",
]
