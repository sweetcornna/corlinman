"""Parent-loop integration for the ``subagent_spawn`` tool.

Iter 8 of the D3 plan in ``docs/design/phase4-w4-d3-design.md``. Iter 7
landed the runner-side filtering; this module provides the bits needed
to actually expose ``subagent_spawn`` to the parent's LLM:

1. :func:`subagent_spawn_tool_schema` — OpenAI-shaped tool descriptor.
   Drop this into the parent's ``ChatStart.tools`` list and the model
   will emit ``ToolCallEvent("subagent_spawn", {"agent": "...",
   "goal": "..."})`` calls.
2. :func:`dispatch_subagent_spawn` — async helper that consumes a tool
   call's ``args_json``, resolves the requested agent card, drives
   :func:`corlinman_agent.subagent.run_child`, and returns the JSON
   string the gateway dispatcher feeds back as
   :attr:`corlinman_agent.reasoning_loop.ToolResult.content`. The
   parent's loop then appends a ``role="tool"`` message and continues —
   the tool-call envelope is the result-merge format the design fixes
   in § "Result merging — tool-call envelope wins".

The Rust supervisor (``corlinman-subagent`` crate) is the canonical
owner of the depth / concurrency / tenant caps. This module's
:func:`dispatch_subagent_spawn` therefore takes a *callable* —
``supervisor_acquire`` — that the production caller binds to either the
real Rust ``Supervisor::try_acquire`` (via the iter-5 PyO3 bridge) or
to an in-process Python stub for tests. Keeping the supervisor
abstract here means we can unit-test the dispatch contract without
spinning a Rust interpreter.

Failure mapping (the parent's LLM must observe every kind of failure
deterministically so the evolution loop can learn from it):

* unknown agent name → :attr:`FinishReason.REJECTED`,
  ``error="agent_not_found"``;
* malformed ``args_json`` (not JSON, missing ``agent`` / ``goal``,
  wrong types) → :attr:`FinishReason.REJECTED`, ``error`` carries the
  parse / validation message;
* supervisor refused the spawn (cap / depth) →
  :attr:`FinishReason.REJECTED` or :attr:`FinishReason.DEPTH_CAPPED`
  via :meth:`TaskResult.rejected`;
* uncaught exception in :func:`run_child` →
  :attr:`FinishReason.ERROR`. The runner already catches its own
  exceptions, so this branch is the belt-and-braces case for
  programmer error in the dispatch layer.

The whole module is pure Python — no PyO3, no gateway — so the unit
tests in ``test_subagent_tool_wrapper.py`` exercise the full
LLM↔runner round-trip without needing the Rust crate built.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from corlinman_agent.agents.card import AgentCard, build_ephemeral_card
from corlinman_agent.subagent.api import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_MAX_WALL_SECONDS,
    FinishReason,
    ParentContext,
    TaskResult,
    TaskSpec,
)
from corlinman_agent.subagent.runner import (
    SUBAGENT_SPAWN_INLINE_TOOL,
    SUBAGENT_SPAWN_MANY_TOOL,
    SUBAGENT_SPAWN_TOOL,
    ChildToolExecutor,
    run_child,
)

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from corlinman_persona.store import PersonaStore

    from corlinman_agent.agents.registry import AgentCardRegistry

logger = structlog.get_logger(__name__)


#: Sentinel error returned when ``args.agent`` doesn't resolve through
#: the registry. Pinned as a constant so the parent's prompt branches
#: on a stable string and the iter-9 hook event payload can carry it
#: verbatim.
AGENT_NOT_FOUND_ERROR: str = "agent_not_found"

#: Sentinel error returned when ``args.subagent_type`` (W1.1) refers to
#: an unknown card *and* the registry has no ``general-purpose``
#: fallback wired. Distinct from :data:`AGENT_NOT_FOUND_ERROR` so the
#: parent's prompt can tell apart "the legacy ``agent`` field referred
#: to a missing card" from "the Claude-Code-style ``subagent_type``
#: referred to a missing card without a fallback".
UNKNOWN_SUBAGENT_TYPE_ERROR: str = "unknown_subagent_type"

#: Sentinel error returned when the LLM sets ``run_in_background=true``
#: before the W1.3 background-dispatch path lands. The schema accepts
#: the field so the model's grammar matches Claude Code's; the backend
#: rejects with this error until the async store / completion polling
#: surface is in place.
BACKGROUND_NOT_IMPLEMENTED_ERROR: str = "run_in_background_not_implemented"

#: Sentinel error returned when the JSON args fail validation. The
#: details (which field, what type) ride in the error message verbatim.
ARGS_INVALID_ERROR: str = "args_invalid"


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------


def subagent_spawn_tool_schema(
    *,
    default_max_wall_seconds: int = DEFAULT_MAX_WALL_SECONDS,
    default_max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
) -> dict[str, Any]:
    """Return the OpenAI-shaped tool descriptor for ``subagent_spawn``.

    The descriptor is what the parent's reasoning loop hands the
    provider so the LLM can emit a ``ToolCallEvent`` for
    ``subagent_spawn``. Field naming matches the design's
    :class:`TaskSpec` exactly so a one-to-one
    ``json.loads(args_json) → TaskSpec(**...)`` works in
    :func:`dispatch_subagent_spawn`.

    The ``default_*`` parameters are surfaced in the schema's
    ``description`` strings (not as JSON-Schema defaults — providers'
    treatment of those varies) so the LLM has a reasonable expectation
    of what'll happen when it omits the budget knobs. The runner /
    supervisor enforce the actual numbers; this is documentation for
    the model.
    """
    return {
        "type": "function",
        "function": {
            "name": SUBAGENT_SPAWN_TOOL,
            "description": (
                "Delegate a self-contained subtask to a child agent and "
                "block until it returns. The child runs in a fresh "
                "context (fresh persona, fresh session) with read-only "
                "access to the parent's memory federation. Use for "
                "research-and-summarise fan-out, multi-source queries, "
                "or fan-out evaluation where context isolation matters. "
                "Pass ``subagent_type`` to pick a pre-configured card "
                "from the registry; omit for the built-in "
                "``general-purpose`` agent."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": (
                            "User-turn prompt the child will receive "
                            "as its only message. Should be self-"
                            "contained — the child cannot see the "
                            "parent's chat history."
                        ),
                    },
                    "subagent_type": {
                        "type": "string",
                        "description": (
                            "Optional. Registry key of a pre-configured "
                            "agent card. Omit (or pass empty) for the "
                            "built-in ``general-purpose`` fallback. An "
                            "explicit value that does not resolve in "
                            "the registry rejects the spawn with "
                            "error=unknown_subagent_type."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "Optional 3-5 word task label, surfaced "
                            "in the activity panel / observability UI. "
                            "Has no effect on the child's reasoning "
                            "loop — it's a human-readable handle the "
                            "frontend renders alongside the spawn "
                            "event."
                        ),
                    },
                    # D4 — ``run_in_background`` is intentionally NOT advertised.
                    # The end-to-end background path is not wired (the gateway
                    # never threads a dispatcher into the spawn call and the
                    # published factory raises), so advertising the field only
                    # invited the model to request a mode that always rejects.
                    # Don't advertise what the wiring can't deliver. The
                    # defensive reject branch below still handles a hand-crafted
                    # ``run_in_background=true`` arg, so older callers fail
                    # cleanly rather than silently running in the foreground.
                    "model": {
                        "type": "string",
                        "description": (
                            "Optional model alias override. When set, "
                            "the child uses this model instead of the "
                            "card's bound model (or the parent's "
                            "default if the card has no binding). "
                            "Useful for fan-out where one sibling "
                            "wants a cheaper model than the others."
                        ),
                    },
                    "agent": {
                        "type": "string",
                        "description": (
                            "DEPRECATED — legacy alias for "
                            "``subagent_type``. Prefer "
                            "``subagent_type`` for new callers. When "
                            "both are passed, ``subagent_type`` wins."
                        ),
                    },
                    "tool_allowlist": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional subset of the parent's tool set "
                            "the child is allowed to call. Omit to "
                            "inherit; pass [] to forbid all tools "
                            "(pure LLM call). Asking for a tool the "
                            "parent doesn't hold rejects the spawn."
                        ),
                    },
                    "max_wall_seconds": {
                        "type": "integer",
                        "description": (
                            f"Hard wall-clock budget for the child. "
                            f"Default {default_max_wall_seconds}s; "
                            f"capped from above by the supervisor's "
                            f"max_wall_seconds_ceiling policy."
                        ),
                        "minimum": 1,
                    },
                    "max_tool_calls": {
                        "type": "integer",
                        "description": (
                            f"Cap on the child's reasoning rounds. "
                            f"Default {default_max_tool_calls}."
                        ),
                        "minimum": 1,
                    },
                    "extra_context": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": (
                            "Optional {ctx.<key>: <text>} blobs "
                            "spliced into the child's system prompt."
                        ),
                    },
                },
                "required": ["goal"],
                "additionalProperties": False,
            },
        },
    }


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

#: Type of the supervisor-acquire callable. Returns either a context
#: manager (the slot drop-guard) on success, or a string describing
#: the rejection reason. Strings ``"depth_capped"`` / anything else are
#: mapped to :attr:`FinishReason.DEPTH_CAPPED` / :attr:`FinishReason.REJECTED`
#: in :func:`dispatch_subagent_spawn`.
#:
#: We use a callable + sentinel rather than raising because the
#: production binding (PyO3 → ``Supervisor::try_acquire``) returns a
#: ``Result``, not a Python exception, and we want the dispatch layer
#: to be agnostic to which side of the FFI it sits on.
SupervisorAcquire = Callable[[ParentContext], Any]


async def dispatch_subagent_spawn(
    *,
    args_json: bytes | str,
    parent_ctx: ParentContext,
    agent_registry: AgentCardRegistry,
    provider: Any,
    parent_tools: Sequence[dict[str, Any]] | None = None,
    persona_store: PersonaStore | None = None,
    supervisor_acquire: SupervisorAcquire | None = None,
    child_seq: int = 0,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_wall_seconds_ceiling: int | None = None,
    parent_model: str | None = None,
    tool_executor: ChildToolExecutor | None = None,
    event_emitter: Any | None = None,
    parent_turn_id: str | None = None,
    parent_session_key: str | None = None,
    subagent_dispatcher: Any | None = None,
) -> str:
    """Translate one ``subagent_spawn`` tool call into a JSON
    :class:`TaskResult` envelope.

    Parameters
    ----------
    args_json
        Raw ``ToolCallEvent.args_json`` bytes (or already-decoded
        string). Parsed as JSON; failure → :attr:`FinishReason.REJECTED`
        with ``error=args_invalid``.
    parent_ctx
        Parent's :class:`ParentContext`. The runner derives the
        child's own context from this; the supervisor uses the depth
        to gate recursion.
    agent_registry
        Source of :class:`AgentCard` lookups. The dispatcher resolves
        ``args.agent`` here; an unknown name short-circuits with
        :attr:`FinishReason.REJECTED` and ``error=agent_not_found``.
    provider
        Provider the *child* will use. The production caller (gateway
        / agent_servicer, iter 8 wiring) typically passes the same
        provider the parent is using; tests pass a fake.
    parent_tools
        OpenAI-shaped tool list the parent is configured with. Forwarded
        to :func:`run_child` as the allowlist source-of-truth (iter 7).
        ``None`` is treated as "parent has no tools", which means the
        child is restricted to a pure LLM call regardless of the
        request's ``tool_allowlist``.
    persona_store
        Forwarded to :func:`run_child` for fresh-row seeding under the
        child's mangled ``agent_id``. ``None`` skips seeding.
    supervisor_acquire
        Callable that reserves a slot in the Rust supervisor. ``None``
        runs without slot enforcement (test mode). On rejection the
        callable returns either ``"depth_capped"`` or any other string
        identifying the cap that fired; the dispatcher maps these to
        the appropriate :class:`FinishReason`.
    child_seq
        Sibling-disambiguation sequence number. The production caller
        keeps a per-parent counter (``parent_session_key`` →
        :class:`AtomicUsize` on the Rust side); tests pass 0.
    max_depth
        Threaded into :func:`run_child` so its self-prune at
        ``child_depth >= max_depth - 1`` matches the live policy.
    max_wall_seconds_ceiling
        Optional ceiling on the request's ``max_wall_seconds``. The
        design's ``[subagent].max_wall_seconds_ceiling`` (default 300)
        is enforced from above — if the LLM asks for more, we clamp.
    parent_model
        v1.12.2 — the parent's resolved model alias. Threaded into
        :func:`run_child` as the *fallback* model: when the LLM omits
        ``model`` and the resolved card has no ``model`` binding, the
        child inherits the parent's model instead of reaching the
        provider with ``model=""`` (which 400s "model is required").

    Returns
    -------
    str
        JSON-serialised :class:`TaskResult`. The caller feeds this
        verbatim into :class:`ToolResult.content`. Always returns;
        never raises (the parent's loop must keep going).
    """
    # ── 1. Parse + validate the LLM's args. ──────────────────────────
    try:
        parsed = _parse_args(args_json)
    except _ArgsInvalidError as exc:
        logger.warning(
            "subagent.dispatch.args_invalid",
            session=parent_ctx.parent_session_key,
            error=exc.message,
        )
        return _result_json(
            _rejected_result(
                parent_ctx=parent_ctx,
                reason=FinishReason.REJECTED,
                error=f"{ARGS_INVALID_ERROR}: {exc.message}",
            )
        )
    spec = parsed.spec

    # ── 1b. Background dispatch (W1.3). ──────────────────────────────
    # When the LLM asks for ``run_in_background=true`` and a dispatcher
    # is wired by the caller (gateway publishes one onto AdminState; the
    # tool-call path threads it through here), we register the request
    # in the background store and return an ``async_launched``-shaped
    # envelope immediately so the parent's reasoning loop keeps going.
    # The actual child runs under an asyncio.Task and surfaces a
    # synthetic user-notification on terminal — Claude Code parity.
    # Falls back to the legacy ``run_in_background_not_implemented``
    # rejection when the dispatcher isn't wired (degraded boot / test
    # path that hasn't installed one).
    if parsed.run_in_background:
        if subagent_dispatcher is None:
            logger.info(
                "subagent.dispatch.background_dispatcher_unavailable",
                session=parent_ctx.parent_session_key,
                subagent_type=parsed.subagent_type,
            )
            return _result_json(
                _rejected_result(
                    parent_ctx=parent_ctx,
                    reason=FinishReason.REJECTED,
                    error=BACKGROUND_NOT_IMPLEMENTED_ERROR,
                )
            )
        return await _dispatch_via_background(
            parsed=parsed,
            parent_ctx=parent_ctx,
            agent_registry=agent_registry,
            subagent_dispatcher=subagent_dispatcher,
            child_seq=child_seq,
        )

    # ── 2. Resolve the agent card. W1.1 / v1.12.2 ────────────────────
    # ``get_or_builtin_default`` returns the explicit card when the LLM
    # passed a known ``subagent_type`` / ``agent`` value, an IN-CODE
    # ``general-purpose`` fallback when the field is empty/absent (the
    # v1.12.2 fix: this no longer requires a ``general-purpose.md`` on
    # disk, so a fresh VPS with an empty ``agents/`` dir still resolves
    # the default), or ``None`` when an *explicit* value didn't resolve
    # (the dispatcher then emits ``unknown_subagent_type`` so the LLM can
    # correct the typo rather than silently getting the default card).
    card = agent_registry.get_or_builtin_default(parsed.subagent_type)
    if card is None:
        if parsed.subagent_type:
            # Explicit but unknown. Sentinel choice tracks which field
            # the LLM used so callers built against either schema get
            # the error string they branch on:
            #   * legacy ``agent`` field  → ``agent_not_found``
            #   * W1.1 ``subagent_type`` → ``unknown_subagent_type``
            if parsed.used_legacy_agent_field:
                error = f"{AGENT_NOT_FOUND_ERROR}: {parsed.subagent_type!r}"
            else:
                error = f"{UNKNOWN_SUBAGENT_TYPE_ERROR}: {parsed.subagent_type!r}"
            requested_for_log = parsed.subagent_type
        else:
            # Caller omitted the type AND the fallback card isn't
            # registered. Surfaces as the legacy ``agent_not_found``
            # so dashboards / operators see a single error class for
            # "missing card" rather than splitting the signal.
            error = f"{AGENT_NOT_FOUND_ERROR}: 'general-purpose'"
            requested_for_log = "<default>"
        logger.info(
            "subagent.dispatch.agent_not_found",
            session=parent_ctx.parent_session_key,
            requested=requested_for_log,
        )
        return _result_json(
            _rejected_result(
                parent_ctx=parent_ctx,
                reason=FinishReason.REJECTED,
                error=error,
            )
        )
    # Hand off to the shared post-resolution driver (clamp → acquire slot
    # → emit → run_child → emit → envelope). ``subagent_spawn_inline``
    # reuses the SAME driver — its only divergence from this path is that
    # ``card`` is an ephemeral in-memory card instead of a registry lookup.
    return await _run_child_under_slot(
        card=card,
        spec=spec,
        parent_ctx=parent_ctx,
        provider=provider,
        parent_tools=parent_tools,
        persona_store=persona_store,
        supervisor_acquire=supervisor_acquire,
        child_seq=child_seq,
        max_depth=max_depth,
        max_wall_seconds_ceiling=max_wall_seconds_ceiling,
        # W1.1: caller's ``model`` arg (when present) wins over the
        # card's binding; ``None`` lets the runner fall back to
        # ``agent_card.model`` and then ``parent_model``.
        model_override=parsed.model,
        parent_model=parent_model,
        tool_executor=tool_executor,
        event_emitter=event_emitter,
        parent_turn_id=parent_turn_id,
        parent_session_key=parent_session_key,
    )


async def _run_child_under_slot(
    *,
    card: AgentCard,
    spec: TaskSpec,
    parent_ctx: ParentContext,
    provider: Any,
    parent_tools: Sequence[dict[str, Any]] | None,
    persona_store: PersonaStore | None,
    supervisor_acquire: SupervisorAcquire | None,
    child_seq: int,
    max_depth: int,
    max_wall_seconds_ceiling: int | None,
    model_override: str | None,
    event_emitter: Any | None,
    parent_turn_id: str | None,
    parent_session_key: str | None,
    parent_model: str | None = None,
    tool_executor: ChildToolExecutor | None = None,
) -> str:
    """Shared post-resolution driver for ``subagent_spawn`` /
    ``subagent_spawn_inline``.

    Both the named-card path and the inline-card path resolve an
    :class:`AgentCard` (registry lookup vs ephemeral construction) and
    then funnel through here: clamp budgets to the policy ceiling, acquire
    a supervisor slot (or run unguarded in test mode), emit the
    ``SubagentSpawned`` / ``SubagentCompleted`` envelopes, drive
    :func:`run_child` under the slot, and fold every outcome into the JSON
    :class:`TaskResult` envelope. Never raises.
    """
    # The card's name threads into the child's ``parent_agent_id``
    # mangling (``::<card>::<seq>``) so a fallback / ephemeral label is
    # observable in the child's id.
    agent_name = card.name

    # ── Clamp request-side budgets to the policy ceiling. ────────────
    if (
        max_wall_seconds_ceiling is not None
        and spec.max_wall_seconds > max_wall_seconds_ceiling
    ):
        # Frozen dataclass — rebuild rather than mutate.
        from dataclasses import replace as _dc_replace

        spec = _dc_replace(spec, max_wall_seconds=max_wall_seconds_ceiling)

    # ── Acquire a supervisor slot (real or stubbed). ─────────────────
    slot_cm: Any
    if supervisor_acquire is None:
        slot_cm = nullcontext()
    else:
        outcome = supervisor_acquire(parent_ctx)
        if isinstance(outcome, str):
            # Rejection — map the string to a finish reason. The
            # supervisor serialises ``AcquireReject::DepthCapped`` as
            # ``"depth_capped"``; anything else is a per-parent /
            # tenant cap rejection.
            reason = (
                FinishReason.DEPTH_CAPPED
                if outcome == "depth_capped"
                else FinishReason.REJECTED
            )
            return _result_json(
                _rejected_result(
                    parent_ctx=parent_ctx,
                    reason=reason,
                    error=f"supervisor: {outcome}",
                )
            )
        slot_cm = outcome  # context-manager-shaped slot drop-guard

    # ── Drive the child runner under the slot. ───────────────────────
    # D5 — enter the slot guard BEFORE the first post-acquire await. The
    # supervisor incremented the per-parent + per-tenant counters
    # synchronously inside ``supervisor_acquire`` above. If this coroutine is
    # cancelled at the ``_emit_subagent_spawned`` await below (CancelledError
    # is a BaseException, so the emitter's own ``except Exception`` does not
    # swallow it), the slot's ``release()`` would never run and the counters
    # would leak until a non-deterministic ``Slot.__del__``. Wrapping every
    # post-acquire await in the ``with`` releases the slot on cancellation
    # too. ``nullcontext`` (the supervisor-less test path) is re-entrant so
    # this is safe.
    child_ctx_preview = parent_ctx.child_context(agent_name, child_seq)
    try:
        with slot_cm:
            await _emit_subagent_spawned(
                emitter=event_emitter,
                parent_turn_id=parent_turn_id,
                parent_session_key=parent_session_key,
                parent_ctx=parent_ctx,
                child_ctx=child_ctx_preview,
                prompt_preview=spec.goal,
            )

            _child_emitter = _make_bubble_emitter(
                parent=event_emitter,
                parent_turn_id=parent_turn_id,
                parent_session_key=parent_session_key,
                child_session_key=child_ctx_preview.parent_session_key,
            )

            result = await run_child(
                parent_ctx,
                card,
                spec,
                provider=provider,
                child_seq=child_seq,
                persona_store=persona_store,
                parent_tools=parent_tools,
                max_depth=max_depth,
                event_emitter=_child_emitter,
                model_override=model_override,
                parent_model=parent_model,
                tool_executor=tool_executor,
            )
    except Exception as exc:
        logger.exception(
            "subagent.dispatch.runner_uncaught",
            session=parent_ctx.parent_session_key,
        )
        result = TaskResult(
            output_text="",
            tool_calls_made=[],
            child_session_key=f"{parent_ctx.parent_session_key}::child::{child_seq}",
            child_agent_id=f"{parent_ctx.parent_agent_id}::{agent_name}::{child_seq}",
            elapsed_ms=0,
            finish_reason=FinishReason.ERROR,
            error=str(exc),
        )

    await _emit_subagent_completed(
        emitter=event_emitter,
        parent_turn_id=parent_turn_id,
        parent_session_key=parent_session_key,
        result=result,
    )

    return _result_json(result)


def _make_bubble_emitter(
    *,
    parent: Any | None,
    parent_turn_id: str | None,
    parent_session_key: str | None,
    child_session_key: str,
) -> Any | None:
    """Construct a :class:`BubbleEmitter` for the child agent.

    Lazy-imports ``corlinman_server.gateway.observability`` so the
    subagent package retains zero compile-time dependency on the
    server package — callers that don't wire observability (tests,
    smoke runs) never trigger the import. Returns ``None`` when no
    parent emitter / correlation pair is provided.
    """
    if parent is None or not parent_turn_id or not parent_session_key:
        return None
    try:
        from corlinman_server.gateway.observability.emitter import (  # noqa: PLC0415
            BubbleEmitter,
        )
    except ImportError:
        # The server package isn't importable from this test/runtime —
        # fall through and skip child bubbling. The parent-side spawn
        # / completed envelopes still fire from the dispatcher itself.
        return None
    return BubbleEmitter(
        parent=parent,
        parent_turn_id=parent_turn_id,
        parent_session_key=parent_session_key,
        child_session_key=child_session_key,
    )


def _truncate(text: str, limit: int) -> str:
    """Return ``text`` capped at ``limit`` chars with an ellipsis."""
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit] + "…"


async def _emit_subagent_spawned(
    *,
    emitter: Any | None,
    parent_turn_id: str | None,
    parent_session_key: str | None,
    parent_ctx: ParentContext,
    child_ctx: ParentContext,
    prompt_preview: str,
) -> None:
    """Best-effort emit of :class:`SubagentSpawned` on the parent stream.

    No-op when any of (emitter, parent_turn_id, parent_session_key) is
    ``None`` — the dispatcher is callable from test paths that don't
    wire observability. Errors inside the emitter are swallowed: the
    spawn must still happen.
    """
    if not emitter or not parent_turn_id or not parent_session_key:
        return
    from corlinman_agent.events import SubagentSpawned  # noqa: PLC0415

    try:
        await emitter.emit_event(
            parent_turn_id,
            parent_session_key,
            SubagentSpawned(
                parent_session_key=parent_ctx.parent_session_key,
                child_session_key=child_ctx.parent_session_key,
                child_agent_id=child_ctx.parent_agent_id,
                depth=child_ctx.depth,
                prompt_preview=_truncate(prompt_preview, 200),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "subagent.dispatch.spawned_emit_failed",
            session=parent_ctx.parent_session_key,
            error=str(exc),
        )


async def _emit_subagent_completed(
    *,
    emitter: Any | None,
    parent_turn_id: str | None,
    parent_session_key: str | None,
    result: TaskResult,
) -> None:
    """Best-effort emit of :class:`SubagentCompleted` on the parent stream."""
    if not emitter or not parent_turn_id or not parent_session_key:
        return
    from corlinman_agent.events import SubagentCompleted  # noqa: PLC0415

    try:
        await emitter.emit_event(
            parent_turn_id,
            parent_session_key,
            SubagentCompleted(
                child_session_key=result.child_session_key,
                finish_reason=result.finish_reason.value,
                tool_calls_made=len(result.tool_calls_made),
                elapsed_ms=result.elapsed_ms,
                summary=_truncate(result.output_text, 1024),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "subagent.dispatch.completed_emit_failed",
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


class _ArgsInvalidError(Exception):
    """Raised by :func:`_parse_args` when the LLM's arguments are
    unparseable or fail shape validation. Caught in
    :func:`dispatch_subagent_spawn` and folded into a rejected result.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(slots=True, frozen=True)
class _ParsedSpawnArgs:
    """Internal record for the parsed ``subagent_spawn`` tool-call args.

    W1.1 — extending the original ``(spec, agent_name)`` tuple to carry
    the four Claude-Code-style fields without churning every call site.
    The dispatcher destructures this; tests can construct one directly
    if needed.
    """

    spec: TaskSpec
    #: Resolved subagent registry key. ``None`` → caller omitted both
    #: ``subagent_type`` and the legacy ``agent`` field; the dispatcher
    #: falls back to the registry's ``general-purpose`` card.
    subagent_type: str | None
    description: str | None
    run_in_background: bool
    model: str | None
    #: ``True`` when the LLM used the deprecated ``agent`` field instead
    #: of (or in addition to) ``subagent_type``. The dispatcher uses
    #: this to choose between the legacy ``agent_not_found`` error and
    #: the W1.1 ``unknown_subagent_type`` error so callers built
    #: against either schema get the error sentinel they branch on.
    used_legacy_agent_field: bool


def _decode_args_dict(args_json: bytes | str) -> dict[str, Any]:
    """Decode ``args_json`` to a JSON object dict.

    Raises :class:`_ArgsInvalidError` on non-utf8 / non-JSON / non-object
    payloads. Shared by :func:`_parse_inline_args` so it can read fields
    (``system_prompt`` / ``name``) that :func:`_parse_args` doesn't surface
    without duplicating the decode-and-validate ladder.
    """
    if isinstance(args_json, (bytes, bytearray)):
        try:
            decoded = args_json.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _ArgsInvalidError(f"args_json not utf-8: {exc}") from exc
    else:
        decoded = args_json
    try:
        raw = json.loads(decoded) if decoded else {}
    except json.JSONDecodeError as exc:
        raise _ArgsInvalidError(f"args_json not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise _ArgsInvalidError(
            f"args_json must be a JSON object, got {type(raw).__name__}"
        )
    return raw


def _parse_args(args_json: bytes | str) -> _ParsedSpawnArgs:
    """Parse + validate the raw ``args_json`` from the tool call.

    Returns a :class:`_ParsedSpawnArgs` record. Field resolution:

    * ``subagent_type`` is the new canonical handle (Claude-Code shape).
    * ``agent`` is the legacy alias from the pre-W1.1 schema. When both
      are supplied, ``subagent_type`` wins; the legacy field is kept
      so existing trained-model prompts don't have to re-emit.
    * ``goal`` is the only hard requirement — every other field is
      optional with a documented default.
    """
    if isinstance(args_json, (bytes, bytearray)):
        try:
            decoded = args_json.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _ArgsInvalidError(f"args_json not utf-8: {exc}") from exc
    else:
        decoded = args_json

    try:
        raw = json.loads(decoded) if decoded else {}
    except json.JSONDecodeError as exc:
        raise _ArgsInvalidError(f"args_json not JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise _ArgsInvalidError(
            f"args_json must be a JSON object, got {type(raw).__name__}"
        )

    goal = raw.get("goal")
    if not isinstance(goal, str) or not goal:
        raise _ArgsInvalidError("missing or empty 'goal' field")

    # W1.1: ``subagent_type`` is the new canonical handle; ``agent`` is
    # the legacy alias. Either may be omitted — the dispatcher falls
    # back to the registry's ``general-purpose`` card when both are
    # absent.
    subagent_type_raw = raw.get("subagent_type")
    legacy_agent = raw.get("agent")
    if subagent_type_raw is not None and not isinstance(subagent_type_raw, str):
        raise _ArgsInvalidError("'subagent_type' must be a string when provided")
    if legacy_agent is not None and not isinstance(legacy_agent, str):
        raise _ArgsInvalidError("'agent' must be a string when provided")
    # Prefer ``subagent_type`` when both are passed. Empty strings are
    # treated as omitted so the LLM can send ``{"subagent_type": ""}``
    # to explicitly request the default card without resorting to
    # JSON-null gymnastics.
    used_legacy_agent_field = False
    if isinstance(subagent_type_raw, str) and subagent_type_raw:
        subagent_type: str | None = subagent_type_raw
    elif isinstance(legacy_agent, str) and legacy_agent:
        subagent_type = legacy_agent
        used_legacy_agent_field = True
    else:
        subagent_type = None

    description_raw = raw.get("description")
    if description_raw is not None and not isinstance(description_raw, str):
        raise _ArgsInvalidError("'description' must be a string when provided")
    description: str | None = description_raw if description_raw else None

    run_in_background_raw = raw.get("run_in_background", False)
    if not isinstance(run_in_background_raw, bool):
        raise _ArgsInvalidError("'run_in_background' must be a boolean when provided")
    run_in_background: bool = run_in_background_raw

    model_raw = raw.get("model")
    if model_raw is not None and not isinstance(model_raw, str):
        raise _ArgsInvalidError("'model' must be a string when provided")
    model: str | None = model_raw if model_raw else None

    # Optional fields — validate type, fall through to defaults.
    tool_allowlist = raw.get("tool_allowlist")
    if tool_allowlist is not None and (
        not isinstance(tool_allowlist, list)
        or not all(isinstance(t, str) for t in tool_allowlist)
    ):
        raise _ArgsInvalidError("'tool_allowlist' must be a list of strings")

    max_wall_seconds = raw.get("max_wall_seconds", DEFAULT_MAX_WALL_SECONDS)
    if not isinstance(max_wall_seconds, int) or max_wall_seconds <= 0:
        raise _ArgsInvalidError("'max_wall_seconds' must be a positive integer")

    max_tool_calls = raw.get("max_tool_calls", DEFAULT_MAX_TOOL_CALLS)
    if not isinstance(max_tool_calls, int) or max_tool_calls <= 0:
        raise _ArgsInvalidError("'max_tool_calls' must be a positive integer")

    extra_context = raw.get("extra_context", {})
    if not isinstance(extra_context, dict) or not all(
        isinstance(k, str) and isinstance(v, str)
        for k, v in extra_context.items()
    ):
        raise _ArgsInvalidError("'extra_context' must be a dict[str, str]")

    spec = TaskSpec(
        goal=goal,
        tool_allowlist=list(tool_allowlist) if tool_allowlist is not None else None,
        max_wall_seconds=max_wall_seconds,
        max_tool_calls=max_tool_calls,
        extra_context=dict(extra_context),
    )
    return _ParsedSpawnArgs(
        spec=spec,
        subagent_type=subagent_type,
        description=description,
        run_in_background=run_in_background,
        model=model,
        used_legacy_agent_field=used_legacy_agent_field,
    )


def _rejected_result(
    *,
    parent_ctx: ParentContext,
    reason: FinishReason,
    error: str,
) -> TaskResult:
    """Construct the synthetic envelope for a pre-spawn rejection.

    Mirrors :meth:`TaskResult.rejected` for the supervisor's own
    rejection path but accepts a free-form error string so the
    args-invalid / agent-not-found cases can carry their specific
    messages. The ``::child::-`` session-key convention marks the
    refused slot for operator UIs.
    """
    return TaskResult(
        output_text="",
        tool_calls_made=[],
        child_session_key=f"{parent_ctx.parent_session_key}::child::-",
        child_agent_id="",
        elapsed_ms=0,
        finish_reason=reason,
        error=error,
    )


async def _dispatch_via_background(
    *,
    parsed: _ParsedSpawnArgs,
    parent_ctx: ParentContext,
    agent_registry: AgentCardRegistry,
    subagent_dispatcher: Any,
    child_seq: int,
) -> str:
    """Register the spawn with the background dispatcher and return the
    ``async_launched`` envelope immediately.

    W1.3 — Claude Code parity. The parent model sees an immediate
    response carrying the ``request_id``, can keep going on its current
    turn, and observes the child's eventual completion via the synthetic
    user-role notification the dispatcher injects into the parent
    session's journal on terminal state.

    Resolution rules:

    * ``subagent_type`` resolves through the registry just like the
      foreground path. An unknown type rejects with the same
      ``unknown_subagent_type`` / ``agent_not_found`` sentinel so the
      LLM's branching prompt is shape-stable across foreground vs
      background calls.
    * Tenant-quota refusal surfaces as
      ``finish_reason=REJECTED, error="supervisor: tenant_quota_exceeded"``
      so the LLM sees the same sentinel string the foreground path uses.
    """
    import uuid

    # Lazy import — corlinman-agent intentionally has no compile-time
    # dependency on corlinman-server (same rationale as
    # :func:`_make_bubble_emitter`). When the dispatcher *was* wired,
    # the corlinman_server.system.subagent module is necessarily on
    # the import path; a degraded boot without it would have left
    # ``subagent_dispatcher=None`` and we'd never get here.
    try:
        from corlinman_server.system.subagent import (  # noqa: PLC0415
            SubagentRequest,
            TenantQuotaExceeded,
        )
    except ImportError:
        return _result_json(
            _rejected_result(
                parent_ctx=parent_ctx,
                reason=FinishReason.REJECTED,
                error=BACKGROUND_NOT_IMPLEMENTED_ERROR,
            )
        )

    # Card resolution — mirrors the foreground path. We don't need the
    # full card object here (the dispatcher's run_child_factory does the
    # second lookup at execution time); we only need to know whether the
    # name is valid so the rejection envelope can carry the right
    # sentinel.
    card = agent_registry.get_or_builtin_default(parsed.subagent_type)
    if card is None:
        sentinel = (
            AGENT_NOT_FOUND_ERROR
            if parsed.used_legacy_agent_field
            else UNKNOWN_SUBAGENT_TYPE_ERROR
        )
        return _result_json(
            _rejected_result(
                parent_ctx=parent_ctx,
                reason=FinishReason.REJECTED,
                error=f"{sentinel}: {parsed.subagent_type!r}",
            )
        )

    request_id = str(uuid.uuid4())
    import time as _time

    req = SubagentRequest(
        request_id=request_id,
        parent_session_key=parent_ctx.parent_session_key,
        parent_agent_id=parent_ctx.parent_agent_id,
        subagent_type=card.name,
        goal=parsed.spec.goal,
        description=parsed.description,
        requested_at=int(_time.time() * 1000),
        requested_by=None,
        tenant_id=parent_ctx.tenant_id,
    )

    try:
        status = await subagent_dispatcher.dispatch_async(req)
    except TenantQuotaExceeded as exc:
        return _result_json(
            _rejected_result(
                parent_ctx=parent_ctx,
                reason=FinishReason.REJECTED,
                error=f"supervisor: tenant_quota_exceeded ({exc.active}/{exc.ceiling})",
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "subagent.dispatch.background_dispatch_failed",
            session=parent_ctx.parent_session_key,
            subagent_type=card.name,
        )
        return _result_json(
            _rejected_result(
                parent_ctx=parent_ctx,
                reason=FinishReason.REJECTED,
                error=f"background_dispatch_failed: {exc}",
            )
        )

    # ``async_launched`` envelope — matches Claude Code's shape. The
    # parent model is trained to look for ``status`` here.
    payload = {
        "status": "async_launched",
        "request_id": status.request_id,
        "subagent_type": card.name,
        "description": parsed.description,
        "child_session_key": status.child_session_key,
        "state": status.state,
    }
    # We tuck the JSON into the ``output_text`` field of a synthetic
    # TaskResult so the parent's reasoning loop folds it into a
    # ``role="tool"`` message the same way as the foreground path. The
    # parent's prompt branches on the ``status`` field inside the JSON.
    placeholder = TaskResult(
        output_text=json.dumps(payload),
        tool_calls_made=[],
        child_session_key=status.child_session_key or "",
        child_agent_id=card.name,
        elapsed_ms=0,
        finish_reason=FinishReason.STOP,
        error=None,
    )
    _ = child_seq  # silenced — kept for future per-child registry seq use
    return _result_json(placeholder)


def _result_json(result: TaskResult) -> str:
    """JSON-serialise a :class:`TaskResult` for the wire envelope.

    :class:`FinishReason` inherits from ``str`` so its ``.value`` lands
    naturally; :class:`ToolCallSummary` is a dataclass we hand-flatten
    to keep the JSON shape Rust-compatible (Rust expects an object,
    not a tuple). ``error`` is included only when populated to keep
    the parent's prompt token-spend low on the happy path — matches
    the Rust ``#[serde(skip_serializing_if = "Option::is_none")]``
    behaviour byte-for-byte.
    """
    payload: dict[str, Any] = {
        "output_text": result.output_text,
        "tool_calls_made": [
            {
                "name": call.name,
                "args_summary": call.args_summary,
                "duration_ms": call.duration_ms,
            }
            for call in result.tool_calls_made
        ],
        "child_session_key": result.child_session_key,
        "child_agent_id": result.child_agent_id,
        "elapsed_ms": result.elapsed_ms,
        "finish_reason": result.finish_reason.value,
    }
    if result.error is not None:
        payload["error"] = result.error
    return json.dumps(payload)


#: Hard cap on the number of siblings one ``subagent_spawn_many`` call
#: can dispatch. Matches the supervisor's per-parent concurrency ceiling
#: so the cap surfaces as an args-invalid rejection (a clear, actionable
#: signal to the LLM) instead of N-10 silent ``parent_concurrency_exceeded``
#: rejections inside the gather. Raise this only if the supervisor's
#: ``SupervisorPolicy::max_concurrent_per_parent`` is raised in lock-step.
#: v1.12.2 — raised 3 → 10 to match Claude Code's max-fanout (the Task
#: tool lets the orchestrator dispatch up to 10 parallel subagents) and
#: ``SupervisorPolicy.max_concurrent_per_parent`` was bumped in lock-step.
SUBAGENT_SPAWN_MANY_MAX_TASKS: int = 10


def subagent_spawn_many_tool_schema(
    *,
    default_max_wall_seconds: int = DEFAULT_MAX_WALL_SECONDS,
    default_max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    max_tasks: int = SUBAGENT_SPAWN_MANY_MAX_TASKS,
) -> dict[str, Any]:
    """Return the OpenAI-shaped tool descriptor for ``subagent_spawn_many``.

    The orchestrator persona is the primary consumer. The descriptor
    accepts a list of per-child specs (each shaped like a
    :func:`subagent_spawn_tool_schema` body) and the dispatcher fans
    them out concurrently under one parent context. The siblings run in
    parallel, bounded by the supervisor's per-parent concurrency cap
    (which is why ``max_tasks`` defaults to that cap).

    The schema deliberately does NOT carry a ``blackboard_key`` field —
    coordination between siblings is a *content* concern handled by
    putting the same key into each child's ``extra_context``. Keeping
    the fan-out tool ignorant of the blackboard means the same fan-out
    primitive serves shared-state and no-shared-state patterns.
    """
    per_task = {
        "type": "object",
        "properties": {
            "agent": {
                "type": "string",
                "description": (
                    "Optional. Registry key of the agent card to spawn "
                    "for this sibling. Omit (or pass empty) for the "
                    "built-in ``general-purpose`` fallback — same default "
                    "as single ``subagent_spawn``."
                ),
            },
            "goal": {
                "type": "string",
                "description": (
                    "User-turn prompt this sibling will receive as its "
                    "only message."
                ),
            },
            "tool_allowlist": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional subset of the parent's tool set this "
                    "sibling is allowed to call. Inherit if omitted."
                ),
            },
            "max_wall_seconds": {
                "type": "integer",
                "description": (
                    f"Hard wall-clock budget for this sibling. Default "
                    f"{default_max_wall_seconds}s."
                ),
                "minimum": 1,
            },
            "max_tool_calls": {
                "type": "integer",
                "description": (
                    f"Cap on this sibling's reasoning rounds. Default "
                    f"{default_max_tool_calls}."
                ),
                "minimum": 1,
            },
            "extra_context": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": (
                    "Optional {ctx.<key>: <text>} blobs spliced into "
                    "this sibling's system prompt. Use this to pass a "
                    "shared 'blackboard_key' to coordinating siblings."
                ),
            },
        },
        # D11 — only ``goal`` is required, matching the dispatcher (which
        # defaults a missing ``agent`` to ``general-purpose``) and single
        # ``subagent_spawn`` (``required: ["goal"]``). Previously the schema
        # required ``agent`` too, so strict providers 400'd an agent-less
        # task that the dispatcher would have happily run as general-purpose.
        "required": ["goal"],
        "additionalProperties": False,
    }
    return {
        "type": "function",
        "function": {
            "name": SUBAGENT_SPAWN_MANY_TOOL,
            "description": (
                "Dispatch up to "
                f"{max_tasks} sibling child agents concurrently and "
                "block until all return. Use for true fan-out: "
                "research + edit, query multiple sources, compare "
                "approaches. Each sibling runs in its own fresh "
                "context; pass a shared key via extra_context if they "
                "need to coordinate through the blackboard tools. "
                "Returns {\"tasks\": [TaskResult, ...]} in input order."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": per_task,
                        "minItems": 1,
                        "maxItems": max_tasks,
                        "description": (
                            f"1..{max_tasks} per-sibling task specs. "
                            "Each sibling is dispatched concurrently."
                        ),
                    },
                },
                "required": ["tasks"],
                "additionalProperties": False,
            },
        },
    }


async def dispatch_subagent_spawn_many(
    *,
    args_json: bytes | str,
    parent_ctx: ParentContext,
    agent_registry: AgentCardRegistry,
    provider: Any,
    parent_tools: Sequence[dict[str, Any]] | None = None,
    persona_store: PersonaStore | None = None,
    supervisor_acquire: SupervisorAcquire | None = None,
    base_child_seq: int = 0,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_wall_seconds_ceiling: int | None = None,
    max_tasks: int = SUBAGENT_SPAWN_MANY_MAX_TASKS,
    parent_model: str | None = None,
    tool_executor: ChildToolExecutor | None = None,
    event_emitter: Any | None = None,
    parent_turn_id: str | None = None,
    parent_session_key: str | None = None,
) -> str:
    """Translate one ``subagent_spawn_many`` tool call into a JSON
    envelope of :class:`TaskResult` siblings, run concurrently.

    The dispatcher splits the LLM's ``tasks`` list, builds an isolated
    ``args_json`` for each, and awaits :func:`dispatch_subagent_spawn`
    on all of them in parallel via ``asyncio.gather``. The supervisor's
    per-parent concurrency cap (``SupervisorPolicy.max_concurrent_per_parent``,
    default 10) is the hard limit on live siblings; this dispatcher also
    rejects ``len(tasks) > max_tasks`` up-front so the LLM sees a clean
    args-invalid envelope instead of N-10 silent slot rejections.

    Children are disambiguated by ``child_seq = base_child_seq + i`` so
    their ``ParentContext.child_context`` derivations don't collide.
    Failures in one sibling are isolated: ``asyncio.gather`` is called
    with ``return_exceptions=True`` and any exception is folded into a
    synthetic ERROR envelope for that index, keeping the wire shape
    ``{"tasks": [TaskResult, ...]}`` intact.

    Returns
    -------
    str
        JSON object ``{"tasks": [TaskResult, ...]}`` in input order.
        ``error`` lives on individual siblings; the outer envelope is
        always shaped the same.
    """
    # ── 1. Parse + validate the LLM's args. ──────────────────────────
    try:
        task_specs = _parse_spawn_many_args(args_json, max_tasks=max_tasks)
    except _ArgsInvalidError as exc:
        logger.warning(
            "subagent.dispatch_many.args_invalid",
            session=parent_ctx.parent_session_key,
            error=exc.message,
        )
        # Fan-out's args-invalid surfaces as a top-level error
        # envelope (the LLM sees no per-sibling results) so it can't
        # confuse a parse failure with a sibling's runtime failure.
        return json.dumps(
            {
                "tasks": [],
                "error": f"{ARGS_INVALID_ERROR}: {exc.message}",
            }
        )

    # ── 2. Fan out. asyncio.gather over per-sibling dispatch_spawn. ──
    coros = [
        dispatch_subagent_spawn(
            args_json=task_args,
            parent_ctx=parent_ctx,
            agent_registry=agent_registry,
            provider=provider,
            parent_tools=parent_tools,
            persona_store=persona_store,
            supervisor_acquire=supervisor_acquire,
            child_seq=base_child_seq + i,
            max_depth=max_depth,
            max_wall_seconds_ceiling=max_wall_seconds_ceiling,
            parent_model=parent_model,
            tool_executor=tool_executor,
            event_emitter=event_emitter,
            parent_turn_id=parent_turn_id,
            parent_session_key=parent_session_key,
        )
        for i, task_args in enumerate(task_specs)
    ]
    raw = await asyncio.gather(*coros, return_exceptions=True)

    # ── 3. Normalise each result. dispatch_subagent_spawn already
    #      returns a JSON string; if a coro raised (programmer error,
    #      not a sibling-level failure), synthesise the ERROR envelope
    #      so the wire shape is always ``{"tasks": [TaskResult, ...]}``.
    siblings: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if isinstance(item, BaseException):
            logger.exception(
                "subagent.dispatch_many.gather_uncaught",
                session=parent_ctx.parent_session_key,
                child_index=i,
                exc_info=item,
            )
            siblings.append(
                {
                    "output_text": "",
                    "tool_calls_made": [],
                    "child_session_key": (
                        f"{parent_ctx.parent_session_key}"
                        f"::child::{base_child_seq + i}"
                    ),
                    "child_agent_id": "",
                    "elapsed_ms": 0,
                    "finish_reason": FinishReason.ERROR.value,
                    "error": str(item),
                }
            )
        else:
            siblings.append(json.loads(item))

    return json.dumps({"tasks": siblings})


def _parse_spawn_many_args(
    args_json: bytes | str,
    *,
    max_tasks: int,
) -> list[str]:
    """Validate ``{"tasks": [...]}`` and return per-sibling args_json.

    Each returned string is a ready-to-feed argument to
    :func:`dispatch_subagent_spawn` — pre-shaped so the per-sibling
    dispatch reuses the same validation in
    :func:`_parse_args` rather than duplicating the field-by-field
    type checks here. The fan-out wrapper does only the *envelope*
    shape (list size, list-of-objects).
    """
    if isinstance(args_json, (bytes, bytearray)):
        try:
            decoded = args_json.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _ArgsInvalidError(f"args_json not utf-8: {exc}") from exc
    else:
        decoded = args_json
    try:
        raw = json.loads(decoded) if decoded else {}
    except json.JSONDecodeError as exc:
        raise _ArgsInvalidError(f"args_json not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise _ArgsInvalidError(
            f"args_json must be a JSON object, got {type(raw).__name__}"
        )
    tasks = raw.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise _ArgsInvalidError("'tasks' must be a non-empty list of objects")
    if len(tasks) > max_tasks:
        raise _ArgsInvalidError(
            f"'tasks' length {len(tasks)} exceeds the per-fanout cap of {max_tasks}"
        )
    out: list[str] = []
    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise _ArgsInvalidError(
                f"tasks[{i}] must be a JSON object, got {type(task).__name__}"
            )
        # Re-serialise each per-sibling spec so the per-sibling
        # dispatcher's own field validation runs identically to the
        # single-spawn path. Cheap and keeps validation in one place.
        out.append(json.dumps(task))
    return out


# ---------------------------------------------------------------------------
# subagent_spawn_inline — ad-hoc / temporary purpose-built agent
# ---------------------------------------------------------------------------


def subagent_spawn_inline_tool_schema(
    *,
    default_max_wall_seconds: int = DEFAULT_MAX_WALL_SECONDS,
    default_max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
) -> dict[str, Any]:
    """OpenAI-shaped descriptor for ``subagent_spawn_inline``.

    The ad-hoc counterpart to :func:`subagent_spawn_tool_schema`: instead of
    naming a pre-registered card, the parent supplies an inline
    ``system_prompt`` and the dispatcher spins up a one-off, ephemeral
    child (never written to the registry). Mirrors Claude Code's
    general-purpose-with-overrides agent.
    """
    return {
        "type": "function",
        "function": {
            "name": SUBAGENT_SPAWN_INLINE_TOOL,
            "description": (
                "Create a TEMPORARY, purpose-built child agent on the fly "
                "and block until it returns. Unlike subagent_spawn (which "
                "runs a pre-registered agent card by name), this builds a "
                "one-off agent from the system_prompt you write here — use "
                "it when no existing agent fits the subtask. The child runs "
                "in a fresh, isolated context (it cannot see this chat) and "
                "is bounded by your tools — pass a narrow tool_allowlist to "
                "constrain it. The agent is ephemeral: it is NOT saved to "
                "the registry."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": (
                            "The task / user-turn the temporary agent "
                            "receives as its only message. Self-contained "
                            "— the child cannot see this conversation."
                        ),
                    },
                    "system_prompt": {
                        "type": "string",
                        "description": (
                            "The temporary agent's instructions / persona "
                            "— who it is and how to do the job. This is "
                            "what makes it purpose-built."
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": (
                            "Optional short label (a-z0-9-), shown in the "
                            "activity panel. Default 'inline'."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional 3-5 word task label for the UI.",
                    },
                    "tool_allowlist": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional subset of YOUR tools the child may "
                            "call. Omit to inherit your full set; pass [] "
                            "for a pure-LLM child. Asking for a tool you "
                            "don't hold rejects the spawn."
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional model alias override for the child.",
                    },
                    "max_wall_seconds": {
                        "type": "integer",
                        "description": (
                            f"Hard wall-clock budget. Default "
                            f"{default_max_wall_seconds}s; capped by the "
                            f"supervisor ceiling."
                        ),
                        "minimum": 1,
                    },
                    "max_tool_calls": {
                        "type": "integer",
                        "description": (
                            f"Cap on the child's reasoning rounds. Default "
                            f"{default_max_tool_calls}."
                        ),
                        "minimum": 1,
                    },
                    "extra_context": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": (
                            "Optional {ctx.<key>: <text>} blobs spliced "
                            "into the child's system prompt."
                        ),
                    },
                },
                "required": ["goal", "system_prompt"],
                "additionalProperties": False,
            },
        },
    }


@dataclass(slots=True, frozen=True)
class _ParsedInlineSpawnArgs:
    """Parsed args for ``subagent_spawn_inline`` — the shared spawn fields
    (via :func:`_parse_args`) plus the inline-only ``system_prompt`` +
    ``name``."""

    spec: TaskSpec
    system_prompt: str
    name: str | None
    description: str | None
    run_in_background: bool
    model: str | None


def _parse_inline_args(args_json: bytes | str) -> _ParsedInlineSpawnArgs:
    """Validate the raw args for ``subagent_spawn_inline``.

    Reuses :func:`_parse_args` for every shared field (``goal``,
    ``tool_allowlist``, budgets, ``extra_context``, ``model``,
    ``description``, ``run_in_background``) so validation stays in one
    place, then layers on the inline-only ``system_prompt`` (required) and
    ``name`` (optional). Any ``subagent_type`` / ``agent`` field is
    ignored — an inline spawn never resolves a registry card.
    """
    base = _parse_args(args_json)
    raw = _decode_args_dict(args_json)
    system_prompt = raw.get("system_prompt")
    if not isinstance(system_prompt, str) or not system_prompt.strip():
        raise _ArgsInvalidError("missing or empty 'system_prompt' field")
    name_raw = raw.get("name")
    if name_raw is not None and not isinstance(name_raw, str):
        raise _ArgsInvalidError("'name' must be a string when provided")
    return _ParsedInlineSpawnArgs(
        spec=base.spec,
        system_prompt=system_prompt,
        name=name_raw or None if isinstance(name_raw, str) else None,
        description=base.description,
        run_in_background=base.run_in_background,
        model=base.model,
    )


async def dispatch_subagent_spawn_inline(
    *,
    args_json: bytes | str,
    parent_ctx: ParentContext,
    provider: Any,
    parent_tools: Sequence[dict[str, Any]] | None = None,
    supervisor_acquire: SupervisorAcquire | None = None,
    child_seq: int = 0,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_wall_seconds_ceiling: int | None = None,
    parent_model: str | None = None,
    tool_executor: ChildToolExecutor | None = None,
    event_emitter: Any | None = None,
    parent_turn_id: str | None = None,
    parent_session_key: str | None = None,
) -> str:
    """Translate one ``subagent_spawn_inline`` tool call into a JSON
    :class:`TaskResult` envelope.

    Mirrors :func:`dispatch_subagent_spawn` but builds an EPHEMERAL
    :class:`AgentCard` from the inline ``system_prompt`` instead of
    resolving a registry card — no ``agent_registry`` is needed and the
    card is never persisted. The card carries ``tools_allowed=["*"]`` so
    the child inherits the parent's tools (bounded by ``parent_tools`` +
    the caller's ``tool_allowlist``; escalation is rejected by the runner).
    Never raises.
    """
    try:
        parsed = _parse_inline_args(args_json)
    except _ArgsInvalidError as exc:
        logger.warning(
            "subagent.dispatch_inline.args_invalid",
            session=parent_ctx.parent_session_key,
            error=exc.message,
        )
        return _result_json(
            _rejected_result(
                parent_ctx=parent_ctx,
                reason=FinishReason.REJECTED,
                error=f"{ARGS_INVALID_ERROR}: {exc.message}",
            )
        )

    # Inline + background isn't wired yet (the ephemeral child would need
    # the same async store path as a named background spawn). Reject
    # cleanly so the model retries foreground.
    if parsed.run_in_background:
        return _result_json(
            _rejected_result(
                parent_ctx=parent_ctx,
                reason=FinishReason.REJECTED,
                error=BACKGROUND_NOT_IMPLEMENTED_ERROR,
            )
        )

    # Build the ephemeral card in memory — never touches the registry.
    card = build_ephemeral_card(
        name=parsed.name,
        system_prompt=parsed.system_prompt,
        description=parsed.description,
        model=parsed.model,
    )

    return await _run_child_under_slot(
        card=card,
        spec=parsed.spec,
        parent_ctx=parent_ctx,
        provider=provider,
        parent_tools=parent_tools,
        persona_store=None,  # ephemeral child seeds no persona row
        supervisor_acquire=supervisor_acquire,
        child_seq=child_seq,
        max_depth=max_depth,
        max_wall_seconds_ceiling=max_wall_seconds_ceiling,
        model_override=None,  # card.model already carries parsed.model
        parent_model=parent_model,  # v1.12.2: inline card has no model → inherit parent's
        tool_executor=tool_executor,  # v1.12.3: child can actually run tools
        event_emitter=event_emitter,
        parent_turn_id=parent_turn_id,
        parent_session_key=parent_session_key,
    )


# ---------------------------------------------------------------------------
# Coordinator messaging — send_message / recv_message
# ---------------------------------------------------------------------------

#: Tool name for the send direction (registered in the parent's tool set).
AGENT_SEND_MESSAGE_TOOL: str = "agent_send_message"

#: Tool name for the receive direction.
AGENT_RECV_MESSAGE_TOOL: str = "agent_recv_message"


def agent_send_message_tool_schema() -> dict[str, Any]:
    """Return the OpenAI-shaped tool descriptor for ``agent_send_message``.

    Allows one running agent to push a message into another agent's
    in-process mailbox.  The target is identified by its ``agent_id``
    (the ``ParentContext.parent_agent_id`` string).  The call is
    non-blocking — it enqueues and returns immediately with a
    ``{"sent": true, "to": "<agent_id>", "queued_at": <float>}``
    envelope so the sender can continue its own reasoning loop.
    """
    return {
        "type": "function",
        "function": {
            "name": AGENT_SEND_MESSAGE_TOOL,
            "description": (
                "Send a text message to another running agent by its "
                "agent_id. The message is placed in the target's "
                "mailbox immediately; the target can retrieve it with "
                "agent_recv_message. Returns a confirmation envelope "
                "with a monotonic queued_at timestamp."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to_agent_id": {
                        "type": "string",
                        "description": (
                            "The recipient agent's agent_id — the "
                            "mangled ParentContext.parent_agent_id "
                            "string (e.g. "
                            "'root::researcher::0')."
                        ),
                    },
                    "message": {
                        "type": "string",
                        "description": "The text payload to deliver.",
                    },
                    "reply_to_turn": {
                        "type": "integer",
                        "description": (
                            "Optional: the parent-loop turn number this "
                            "message replies to, so the receiver can "
                            "correlate context.  Omit if not applicable."
                        ),
                    },
                },
                "required": ["to_agent_id", "message"],
                "additionalProperties": False,
            },
        },
    }


def agent_recv_message_tool_schema() -> dict[str, Any]:
    """Return the OpenAI-shaped tool descriptor for ``agent_recv_message``.

    Allows an agent to dequeue the next message from its own mailbox.
    If the mailbox is empty and ``timeout_secs`` is 0 (or omitted) the
    call returns immediately with ``{"message": null}``.  Pass a
    positive ``timeout_secs`` to wait for a message.
    """
    return {
        "type": "function",
        "function": {
            "name": AGENT_RECV_MESSAGE_TOOL,
            "description": (
                "Retrieve the next message from your own mailbox. "
                "Returns the message payload or null if the mailbox is "
                "empty (or no message arrives before timeout_secs "
                "elapses). Use agent_send_message from another agent to "
                "populate this mailbox."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "timeout_secs": {
                        "type": "number",
                        "description": (
                            "How long to wait for a message (seconds). "
                            "0 or omitted = non-blocking poll. Positive "
                            "float = block up to that many seconds."
                        ),
                        "minimum": 0,
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    }


async def dispatch_send_message(
    *,
    args_json: bytes | str,
    parent_ctx: ParentContext,
) -> str:
    """Dispatch one ``agent_send_message`` tool call.

    Parses ``args_json`` (keys: ``to_agent_id``, ``message``,
    optionally ``reply_to_turn``), enqueues the message in the target's
    in-process mailbox, and returns a JSON confirmation envelope:

    .. code-block:: json

        {"sent": true, "to": "<agent_id>", "queued_at": 1234.56}

    On parse failure returns an error envelope:

    .. code-block:: json

        {"sent": false, "error": "args_invalid: <details>"}

    Never raises.
    """
    from corlinman_agent.subagent.mailbox import send_to_agent  # local import: keep light

    try:
        raw = json.loads(args_json)
        if not isinstance(raw, dict):
            raise ValueError("expected JSON object")
        to_agent_id: str = raw["to_agent_id"]
        message: str = raw["message"]
        if not isinstance(to_agent_id, str) or not to_agent_id:
            raise ValueError("to_agent_id must be a non-empty string")
        if not isinstance(message, str):
            raise ValueError("message must be a string")
        reply_to_turn: int | None = raw.get("reply_to_turn")
        if reply_to_turn is not None and not isinstance(reply_to_turn, int):
            raise ValueError("reply_to_turn must be an integer or null")
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        logger.warning(
            "subagent.dispatch_send_message.args_invalid",
            session=parent_ctx.parent_session_key,
            error=str(exc),
        )
        return json.dumps(
            {"sent": False, "error": f"{ARGS_INVALID_ERROR}: {exc}"}
        )

    queued_at = await send_to_agent(
        from_agent_id=parent_ctx.parent_agent_id,
        to_agent_id=to_agent_id,
        message=message,
        reply_to_turn=reply_to_turn,
    )
    logger.debug(
        "subagent.send_message.enqueued",
        from_agent=parent_ctx.parent_agent_id,
        to_agent=to_agent_id,
        queued_at=queued_at,
    )
    return json.dumps({"sent": True, "to": to_agent_id, "queued_at": queued_at})


async def dispatch_recv_message(
    *,
    args_json: bytes | str,
    parent_ctx: ParentContext,
) -> str:
    """Dispatch one ``agent_recv_message`` tool call.

    Parses ``args_json`` (optional key: ``timeout_secs``), dequeues the
    next message from the calling agent's mailbox, and returns a JSON
    envelope:

    With a message:

    .. code-block:: json

        {
          "message": "hello",
          "from_agent_id": "root::coordinator::0",
          "reply_to_turn": null,
          "queued_at": 1234.56
        }

    Empty mailbox / timeout:

    .. code-block:: json

        {"message": null}

    Never raises.
    """
    from corlinman_agent.subagent.mailbox import recv_from_mailbox  # local import

    timeout_secs: float | None = None
    try:
        raw = json.loads(args_json)
        if not isinstance(raw, dict):
            raise ValueError("expected JSON object")
        ts = raw.get("timeout_secs")
        if ts is not None:
            timeout_secs = float(ts)
            if timeout_secs < 0:
                raise ValueError("timeout_secs must be >= 0")
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning(
            "subagent.dispatch_recv_message.args_invalid",
            session=parent_ctx.parent_session_key,
            error=str(exc),
        )
        return json.dumps(
            {"message": None, "error": f"{ARGS_INVALID_ERROR}: {exc}"}
        )

    msg = await recv_from_mailbox(
        parent_ctx.parent_agent_id, timeout_secs=timeout_secs
    )
    if msg is None:
        return json.dumps({"message": None})

    logger.debug(
        "subagent.recv_message.dequeued",
        agent=parent_ctx.parent_agent_id,
        from_agent=msg["from_agent_id"],
    )
    return json.dumps(
        {
            "message": msg["message"],
            "from_agent_id": msg["from_agent_id"],
            "reply_to_turn": msg["reply_to_turn"],
            "queued_at": msg["queued_at"],
        }
    )


__all__ = [
    "AGENT_NOT_FOUND_ERROR",
    "AGENT_RECV_MESSAGE_TOOL",
    "AGENT_SEND_MESSAGE_TOOL",
    "ARGS_INVALID_ERROR",
    "BACKGROUND_NOT_IMPLEMENTED_ERROR",
    "SUBAGENT_SPAWN_MANY_MAX_TASKS",
    "UNKNOWN_SUBAGENT_TYPE_ERROR",
    "SupervisorAcquire",
    "agent_recv_message_tool_schema",
    "agent_send_message_tool_schema",
    "dispatch_recv_message",
    "dispatch_send_message",
    "dispatch_subagent_spawn",
    "dispatch_subagent_spawn_inline",
    "dispatch_subagent_spawn_many",
    "subagent_spawn_inline_tool_schema",
    "subagent_spawn_many_tool_schema",
    "subagent_spawn_tool_schema",
]
