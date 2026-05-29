"""Background dispatch for ``subagent.spawn`` with ``run_in_background=true``.

W1.3 of ``docs/PLAN_MULTI_AGENT.md`` §2 Wave 1/W1.3.

The dispatcher is the bridge between the synchronous tool-call site
(``dispatch_subagent_spawn`` in the agent package) and the long-running
asyncio task that actually drives :func:`run_child`. Responsibilities:

1. Mint a ``request_id`` and register a row in :class:`SubagentTaskStore`.
2. Enforce ``max_concurrent_per_tenant`` (default 15, matches the
   :class:`Supervisor` policy).
3. Spawn an :func:`asyncio.create_task` that invokes
   :meth:`Supervisor.spawn_child_to_result` against the resolved agent
   card. The Supervisor wraps the child's events via :class:`BubbleEmitter`
   so they surface on the parent's SSE stream — we re-use that path
   unchanged.
4. Update the store on transitions (``queued`` → ``running`` →
   terminal). On terminal, inject a synthetic ``user``-role notification
   into the parent session's journal so the parent model observes the
   completion on its next turn.
5. Expose :meth:`kill` (operator kill switch) and :meth:`list_active`
   (admin UI list).

The dispatcher purposely does NOT plumb the agent's provider /
persona_store / parent_tools. The production caller (the tool wrapper)
already resolved those when it built ``run_child``'s arguments; the
dispatcher accepts a ``run_child_factory`` callable that closes over
those parameters and returns the awaitable the asyncio task awaits. This
keeps the dispatcher loose-typed against the agent package — pure
``Awaitable[TaskResult]`` in, store updates + journal append out — and
keeps the existing supervisor/runner contract untouched.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

import structlog

from corlinman_server.system.subagent.store import (
    SubagentRequest,
    SubagentStatus,
    SubagentTaskStore,
)

if TYPE_CHECKING:  # pragma: no cover — import-only typing
    from corlinman_server.system.audit import SystemAuditLog

logger = structlog.get_logger(__name__)


__all__ = [
    "AsyncSubagentDispatcher",
    "DispatchOutcome",
    "TenantQuotaExceeded",
]


# Hard cap that mirrors the Supervisor's per-tenant ceiling. Matched
# default keeps the dispatcher's surface refusal aligned with the
# supervisor's slot refusal so an operator never sees the dispatcher
# admit a request only to have the supervisor immediately refuse it.
DEFAULT_MAX_CONCURRENT_PER_TENANT: int = 15


# Truncation budgets — kept tight so a runaway child can't blow up the
# parent's prompt token spend via the synthetic notification.
_NOTIFICATION_BODY_MAX_CHARS: int = 3500
_SUMMARY_PERSIST_MAX_CHARS: int = 3500


class TenantQuotaExceeded(Exception):
    """Raised by :meth:`AsyncSubagentDispatcher.dispatch_async` when the
    per-tenant cap is already at the ceiling.

    The route layer (or tool-wrapper) maps this to the 503-equivalent
    rejection envelope so the LLM observes the cap rather than blocking
    on the dispatcher.
    """

    def __init__(
        self, *, active: int, ceiling: int, tenant_id: str = "default"
    ) -> None:
        super().__init__(
            f"subagent tenant quota exceeded for {tenant_id!r}: "
            f"{active}/{ceiling} active"
        )
        self.active = active
        self.ceiling = ceiling
        self.tenant_id = tenant_id


# ---------------------------------------------------------------------------
# Run-child factory typing
# ---------------------------------------------------------------------------

#: The dispatcher calls this once per request. It must return the
#: awaitable that drives the child (typically a call into the Supervisor
#: that wraps :func:`run_child`). The dispatcher catches any exception
#: raised from the awaitable and folds it into a ``failed`` row.
RunChildFactory = Callable[
    [SubagentRequest],
    Awaitable[Any],  # canonically a TaskResult, but loose-typed to keep
    # the dispatcher decoupled from corlinman_agent at import time.
]


# ---------------------------------------------------------------------------
# Outcome wire shape (for diagnostics / kill semantics)
# ---------------------------------------------------------------------------


class DispatchOutcome:
    """Free-form bag attached to the in-process task registry.

    Carries the asyncio.Task handle so :meth:`AsyncSubagentDispatcher.kill`
    can cancel it. Not persisted — a process restart wipes this and the
    matching row in the store is flagged ``stalled`` by the recovery
    sweep (deferred — for W1.3 we just leave the row as-is and operators
    see it via the audit log).
    """

    __slots__ = ("request_id", "task")

    def __init__(self, request_id: str, task: asyncio.Task[Any]) -> None:
        self.request_id = request_id
        self.task = task


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def _truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit] + "…"


class AsyncSubagentDispatcher:
    """Schedule + track background ``subagent.spawn`` requests.

    Construct one per gateway process; share by reference via
    ``AdminState.subagent_dispatcher``. The dispatcher owns the
    in-memory task registry (so :meth:`kill` can cancel the asyncio.Task)
    and serialises all store + registry mutations behind a single
    :class:`asyncio.Lock`.
    """

    __slots__ = (
        "_audit_log",
        "_journal",
        "_lock",
        "_max_concurrent_per_tenant",
        "_run_child_factory",
        "_store",
        "_tasks",
    )

    def __init__(
        self,
        *,
        store: SubagentTaskStore,
        run_child_factory: RunChildFactory,
        journal: Any | None = None,
        audit_log: "SystemAuditLog | None" = None,
        max_concurrent_per_tenant: int = DEFAULT_MAX_CONCURRENT_PER_TENANT,
    ) -> None:
        """Construct a dispatcher.

        Parameters
        ----------
        store
            Persistent + in-memory row tracker.
        run_child_factory
            Callable that, given the :class:`SubagentRequest`, returns
            the awaitable that drives the child. The production wire-up
            closes over the supervisor + agent registry + provider; tests
            pass a stub that returns a pre-built :class:`TaskResult` (or
            raises to exercise the failed path).
        journal
            Optional :class:`AgentJournal`-shaped sink for the synthetic
            user-role notification injected on terminal. ``None``
            short-circuits the notification — the store row is still
            written so the admin UI surfaces the result.
        max_concurrent_per_tenant
            Hard ceiling matching the supervisor's per-tenant cap.
            Default 15.
        """
        self._store = store
        self._run_child_factory = run_child_factory
        self._journal = journal
        self._audit_log = audit_log
        self._max_concurrent_per_tenant = max_concurrent_per_tenant
        self._lock = asyncio.Lock()
        self._tasks: dict[str, DispatchOutcome] = {}

    # ------------------------------------------------------------------
    # Audit log — best-effort, swallow failures so a write hiccup
    # (full disk, race during shutdown, …) never breaks the dispatcher.
    # The four call sites are `subagent.dispatched` (right after admit),
    # `subagent.completed` (terminal success), `subagent.failed`
    # (timeout / failure / factory raise) and `subagent.killed`
    # (operator pressed Kill). See `docs/PLAN_MULTI_AGENT.md` W3.1.
    # ------------------------------------------------------------------

    async def _audit(
        self,
        event: str,
        *,
        request: SubagentRequest,
        status: SubagentStatus | None = None,
        **details: Any,
    ) -> None:
        audit_log = self._audit_log
        if audit_log is None:
            return
        try:
            # Local imports — keep the module's import-time graph free of
            # the audit module (and its asyncio/json deps) on the happy
            # path where no audit_log is wired (tests, older deployments).
            from corlinman_server.system.audit import (
                AuditEntry,
                utcnow_iso,
            )

            tag = request.subagent_type
            actor = request.requested_by or "model"
            payload: dict[str, Any] = {
                "subagent_type": request.subagent_type,
                "parent_session_key": request.parent_session_key,
            }
            for key, value in details.items():
                if value is not None:
                    payload[key] = value
            await audit_log.append(
                AuditEntry(
                    ts=utcnow_iso(),
                    event=event,
                    request_id=request.request_id,
                    tag=tag,
                    actor=actor,
                    details=payload,
                )
            )
        except Exception:  # noqa: BLE001 — audit must never raise upward
            logger.exception(
                "subagent.audit_failed",
                request_id=request.request_id,
                event=event,
            )

    # ------------------------------------------------------------------
    # Inspection helpers
    # ------------------------------------------------------------------

    @property
    def store(self) -> SubagentTaskStore:
        return self._store

    @property
    def max_concurrent_per_tenant(self) -> int:
        return self._max_concurrent_per_tenant

    async def list_active(self) -> list[SubagentStatus]:
        return await self._store.list_active()

    async def list_all(self) -> list[SubagentStatus]:
        return await self._store.list_all()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def dispatch_async(
        self, req: SubagentRequest
    ) -> SubagentStatus:
        """Register ``req`` and schedule its background task.

        Returns the seeded ``running`` status immediately — the asyncio
        task itself drives the child to terminal in the background.

        Raises :class:`TenantQuotaExceeded` when the per-tenant ceiling
        is already full.
        """
        async with self._lock:
            # R3-004: per-tenant scope — the supervisor enforces the cap
            # per-tenant (see corlinman_subagent.supervisor.try_acquire)
            # and this dispatcher's surface refusal must agree, otherwise
            # one noisy tenant fills the cap and starves every other
            # tenant's dispatches with a misleading "tenant quota
            # exceeded" error that lies about which tenant owns the
            # active rows.
            active = await self._store.count_in_flight_for_tenant(req.tenant_id)
            if active >= self._max_concurrent_per_tenant:
                raise TenantQuotaExceeded(
                    active=active,
                    ceiling=self._max_concurrent_per_tenant,
                    tenant_id=req.tenant_id,
                )

            seeded = await self._store.begin(req)
            seeded = await self._store.update(
                req.request_id,
                state="running",
                started_at=_now_ms(),
            )
            task = asyncio.create_task(
                self._run(req),
                name=f"subagent.background:{req.request_id}",
            )
            self._tasks[req.request_id] = DispatchOutcome(req.request_id, task)
        # Audit outside the lock — write I/O shouldn't block dispatch
        # serialisation, and the entry is itself best-effort.
        await self._audit("subagent.dispatched", request=req)
        return seeded

    # ------------------------------------------------------------------
    # Kill
    # ------------------------------------------------------------------

    async def kill(
        self, request_id: str, *, by: str | None = None
    ) -> SubagentStatus | None:
        """Cancel the in-flight asyncio.Task + flip the store row.

        Returns the killed status snapshot, or ``None`` when the row is
        already terminal (caller maps that to 409).
        """
        async with self._lock:
            outcome = self._tasks.get(request_id)
        if outcome is not None and not outcome.task.done():
            outcome.task.cancel()
        # Independent of whether the asyncio.Task was still live, flip
        # the store row to killed; the task's finally block tolerates a
        # already-terminal row and skips the second update.
        killed = await self._store.set_killed(request_id, by=by)
        if killed is None:
            return None
        # Best-effort notification — same shape as the natural-terminal
        # path, with an explicit kill marker so the parent model sees
        # the operator action.
        await self._inject_notification(
            request_id=request_id,
            agent_name=killed.subagent_type,
            output_text=f"[killed by {by or 'operator'}]",
            terminal_state="killed",
        )
        # Audit the kill so /admin/system Audit card surfaces it.
        # `get_request` is best-effort; on a missing row we skip silently.
        killed_request = await self._store.get_request(request_id)
        if killed_request is not None:
            await self._audit(
                "subagent.killed",
                request=killed_request,
                status=killed,
                killed_by=by,
                finish_reason=killed.finish_reason,
            )
        return killed

    # ------------------------------------------------------------------
    # Internal — the asyncio task body
    # ------------------------------------------------------------------

    async def _run(self, req: SubagentRequest) -> None:
        """Drive one child to terminal and update the store.

        Folds every failure mode (cancellation, factory raise, child
        raise) into a typed row update so the asyncio task never raises
        out of this coroutine — surfacing an exception would log
        ``Task exception was never retrieved`` and is otherwise
        invisible to the admin UI.
        """
        request_id = req.request_id
        start_ns = time.monotonic_ns()
        try:
            result = await self._run_child_factory(req)
        except asyncio.CancelledError:
            # Operator pressed Kill while the child was still running.
            # The store already flipped to ``killed`` from :meth:`kill`;
            # do nothing else (re-raising would surface as ``Task was
            # destroyed``-style noise in logs).
            await self._cleanup_registry(request_id)
            raise
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = (time.monotonic_ns() - start_ns) // 1_000_000
            logger.warning(
                "subagent.dispatcher.run_failed",
                request_id=request_id,
                subagent_type=req.subagent_type,
                error=str(exc),
            )
            try:
                await self._store.update(
                    request_id,
                    state="failed",
                    finished_at=_now_ms(),
                    elapsed_ms=int(elapsed_ms),
                    error=str(exc),
                    finish_reason="error",
                )
            except KeyError:
                pass
            await self._inject_notification(
                request_id=request_id,
                agent_name=req.subagent_type,
                output_text=f"[error] {exc}",
                terminal_state="failed",
            )
            await self._audit(
                "subagent.failed",
                request=req,
                error=str(exc),
                finish_reason="error",
                elapsed_ms=int(elapsed_ms),
            )
            await self._cleanup_registry(request_id)
            return

        # Happy / timeout / supervisor-rejected paths all return a
        # :class:`TaskResult`-shaped value. We duck-type read the fields
        # so the dispatcher doesn't statically depend on
        # corlinman_agent.subagent.api here (the factory is what bridges).
        finish_reason = _read_finish_reason(result)
        terminal_state = _map_finish_to_state(finish_reason)
        output_text = _safe_str(getattr(result, "output_text", "") or "")
        child_session_key = _safe_str(
            getattr(result, "child_session_key", "") or ""
        )
        tool_calls = getattr(result, "tool_calls_made", None)
        tool_calls_count: int = (
            len(tool_calls)  # type: ignore[arg-type]
            if tool_calls is not None and hasattr(tool_calls, "__len__")
            else 0
        )
        elapsed_ms = int(
            getattr(result, "elapsed_ms", 0)
            or ((time.monotonic_ns() - start_ns) // 1_000_000)
        )
        error = getattr(result, "error", None)
        summary = _truncate(output_text, _SUMMARY_PERSIST_MAX_CHARS)

        try:
            await self._store.update(
                request_id,
                state=terminal_state,
                finished_at=_now_ms(),
                child_session_key=child_session_key or None,
                tool_calls_made=tool_calls_count,
                elapsed_ms=elapsed_ms,
                finish_reason=finish_reason,
                error=error,
                summary=summary,
            )
        except KeyError:
            # Row was kill-pruned mid-flight; nothing to record.
            await self._cleanup_registry(request_id)
            return

        await self._inject_notification(
            request_id=request_id,
            agent_name=req.subagent_type,
            output_text=output_text,
            terminal_state=terminal_state,
        )
        # Natural-terminal audit. `succeeded` becomes
        # ``subagent.completed``; everything else (timeout / failed /
        # rejected → mapped to `failed` state) becomes
        # ``subagent.failed`` so the /admin/system Audit card groups by
        # the same broad outcome the operator UI does.
        if terminal_state == "succeeded":
            await self._audit(
                "subagent.completed",
                request=req,
                finish_reason=finish_reason,
                elapsed_ms=elapsed_ms,
                tool_calls_made=tool_calls_count,
            )
        else:
            await self._audit(
                "subagent.failed",
                request=req,
                finish_reason=finish_reason,
                elapsed_ms=elapsed_ms,
                error=error,
                terminal_state=terminal_state,
            )
        await self._cleanup_registry(request_id)

    async def _cleanup_registry(self, request_id: str) -> None:
        async with self._lock:
            self._tasks.pop(request_id, None)

    # ------------------------------------------------------------------
    # Synthetic user-notification — Claude Code parity
    # ------------------------------------------------------------------

    async def _inject_notification(
        self,
        *,
        request_id: str,
        agent_name: str,
        output_text: str,
        terminal_state: str,
    ) -> None:
        """Append a synthetic ``user``-role message to the parent session.

        Format::

            [subagent.completed:<request_id>] <agent_name>

            <output_text trimmed to 3500 chars>

        The parent model sees this as its next inbound user turn. We
        mark the message with ``metadata`` (kind/request_id) so the UI
        can render it differently from genuine user input. ``None``
        journal short-circuits — the store row still has the summary.
        """
        journal = self._journal
        if journal is None:
            return
        req = await self._store.get_request(request_id)
        if req is None:
            return

        body = _truncate(output_text or "", _NOTIFICATION_BODY_MAX_CHARS)
        text = (
            f"[subagent.completed:{request_id}] {agent_name}\n\n{body}"
        )

        # The journal's :meth:`append_message` API takes a numeric
        # ``turn_id``. We don't have a parent-side turn id in scope
        # here, and creating a new turn from outside the chat path would
        # require duck-typing every backend variant. For W1.3 we
        # best-effort try a ``new_turn``/``start_turn`` shaped helper if
        # the journal exposes one — otherwise we skip the append and
        # leave the summary on the store row (the admin UI still
        # surfaces it via ``GET /admin/subagents``).
        new_turn = getattr(journal, "start_turn_for_subagent_notification", None)
        if new_turn is None:
            # No dedicated helper exists yet — defer the journal write
            # and just log it so the audit trail is intact. The parent
            # model's next chat turn will see this via the in-flight /
            # terminal subagent panel.
            logger.info(
                "subagent.dispatcher.notification_skipped",
                request_id=request_id,
                parent_session_key=req.parent_session_key,
                terminal_state=terminal_state,
                reason="journal_lacks_subagent_notification_helper",
            )
            return

        try:
            turn_id = await new_turn(
                session_key=req.parent_session_key,
                kind="subagent_notification",
                user_text=text,
                metadata={
                    "kind": "subagent_notification",
                    "request_id": request_id,
                    "subagent_type": agent_name,
                    "terminal_state": terminal_state,
                },
            )
            if isinstance(turn_id, int):
                await journal.append_message(
                    turn_id,
                    "user",
                    text,
                )
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "subagent.dispatcher.notification_failed",
                request_id=request_id,
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _snapshot(store: SubagentTaskStore) -> list[SubagentStatus]:
    """Read every status row without re-entering the store's lock.

    The dispatcher's outer lock guards ``begin`` / kill / cleanup at the
    dispatcher layer; the store has its own lock for serialised
    persistence. We snapshot once per dispatch decision so the
    quota check sees a coherent count.
    """
    return await store.list_all()


def _read_finish_reason(result: Any) -> str:
    """Extract the finish_reason value as a string, defensively.

    :class:`FinishReason` is a :class:`StrEnum` so ``.value`` / ``str``
    both work; we use the value attribute when present to keep the
    on-wire shape exactly the JSON-canonical lowercase token.
    """
    raw = getattr(result, "finish_reason", None)
    if raw is None:
        return "stop"
    value = getattr(raw, "value", None)
    if isinstance(value, str):
        return value
    return str(raw)


def _map_finish_to_state(finish_reason: str) -> str:
    """Translate a :class:`FinishReason` string to a SubagentState.

    * ``stop`` / ``length`` → ``succeeded`` (the child returned cleanly)
    * ``timeout`` → ``timeout``
    * everything else (``error`` / ``rejected`` / ``depth_capped``) →
      ``failed`` — the operator UI groups these under "did not complete".
    """
    if finish_reason in ("stop", "length"):
        return "succeeded"
    if finish_reason == "timeout":
        return "timeout"
    return "failed"


def _safe_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    return str(value) if value is not None else ""


# Re-export asdict for tests that round-trip statuses through JSON.
_asdict = asdict
