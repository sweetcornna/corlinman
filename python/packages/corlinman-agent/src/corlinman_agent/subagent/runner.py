"""Happy-path child driver — :func:`run_child`.

Iter 4 landed the **happy path only**; iter 6 layered the cooperative
``max_wall_seconds`` enforcement on top; iter 7 (this revision) adds the
**tool-allowlist filter** and **privilege-escalation reject** documented
in design § "Tool exposure".

What :func:`run_child` now does end-to-end:

1. Resolve the child's effective tool list via :func:`_filter_tools_for_child`
   — intersection of ``task.tool_allowlist`` with the parent's
   ``tools_allowed``; ``None`` allowlist means "inherit parent's set".
2. Reject escalation outright: a request for any tool the parent doesn't
   already hold returns a synthetic
   :class:`TaskResult` with ``finish_reason=REJECTED`` and
   ``error="tool_allowlist_escalation"`` *before* the loop is driven.
3. Prune the spawn tools (``subagent_spawn`` / ``_many`` / ``_inline``)
   from EVERY spawned child's allowlist (``child_depth >= 1``) — single-
   level nesting (parent → child). The gateway's child executor refuses
   any recursive spawn anyway, so advertising it would only waste an LLM
   round-trip on a tool that is always rejected.
4. Project the resulting *names* back onto the parent's *tool schemas*
   (the OpenAI `tools=` array) so the child's :class:`ChatStart`
   carries usable tool definitions, not just names.

What is *still* deliberately NOT here:

* PyO3 entry point — the supervisor calls this function over the GIL
  via the iter-5 bridge; iter 8 wires the production caller.
* Hook-bus observability — `SubagentSpawned/Completed/...` lands in
  iter 9.

The split between this runner and the supervisor remains the same
split documented in the design § "Implementation surface — Rust supervisor
+ Python runner": the **isolation contract** lives where the LLM cannot
reach it (Rust); the **loop driver** has to call into Python because that's
where :class:`ReasoningLoop` and the providers live.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import structlog

from corlinman_agent.agents.card import AgentCard
from corlinman_agent.reasoning_loop import (
    ChatStart,
    DoneEvent,
    ErrorEvent,
    ReasoningLoop,
    TokenEvent,
    ToolCallEvent,
    ToolResult,
)
from corlinman_agent.tool_aliases import (
    canonicalize_tool_name,
    warn_alias_collisions,
)

#: Async callback the child loop uses to actually EXECUTE a tool call and
#: return its JSON result envelope (the same string the parent feeds back as
#: :attr:`ToolResult.content`). Supplied by the gateway (which binds it to the
#: parent's builtin dispatcher) and threaded down through
#: :func:`run_child`. ``None`` keeps the legacy "no tool execution" behaviour
#: for pure-LLM children / unit tests that don't wire one. v1.12.3 — without
#: this the child emitted tool calls that were never executed, so the model
#: never received results and returned an empty ``output_text``.
ChildToolExecutor = Callable[[ToolCallEvent], Awaitable[str]]
from corlinman_agent.subagent.api import (
    DEFAULT_MAX_DEPTH,
    FinishReason,
    ParentContext,
    TaskResult,
    TaskSpec,
    ToolCallSummary,
)

#: Reserved tool name the parent's reasoning loop emits when it wants
#: to delegate. Pruned from the child's allowlist at the deepest legal
#: depth (``child_ctx.depth >= max_depth - 1``) so a grandchild can't
#: spawn a great-grandchild that the supervisor would reject with
#: :attr:`FinishReason.DEPTH_CAPPED` anyway. Lifting the literal into a
#: module constant keeps the iter-8 tool-wrapper registration in one
#: place — registry code imports the same name.
#: NOTE: underscore, not dot. OpenAI-style providers require tool names to
#: match ``^[a-zA-Z0-9_-]+$`` — a ``.`` is rejected with a 400 once the tool
#: is advertised. Kept as a constant so the dispatch switch + schema + prune
#: all agree.
SUBAGENT_SPAWN_TOOL: str = "subagent_spawn"

#: Fan-out sibling of ``subagent_spawn`` — the orchestrator agent (v0.7)
#: emits this to dispatch N children concurrently under one parent. The
#: supervisor's per-parent concurrency cap
#: (``SupervisorPolicy.max_concurrent_per_parent``, default 10) still bounds
#: the live siblings; the dispatcher splits the task list and awaits all via
#: ``asyncio.gather``. Pruned from the child's allowlist by the same
#: depth rule that prunes ``subagent_spawn``.
SUBAGENT_SPAWN_MANY_TOOL: str = "subagent_spawn_many"

#: Ad-hoc / temporary sibling of ``subagent_spawn`` (Claude-Code's
#: "general-purpose with overrides" pattern). Where ``subagent_spawn``
#: resolves a *registered* card by name, this spawns a one-off child from
#: an INLINE ``system_prompt`` — an ephemeral :class:`AgentCard` built in
#: memory and never written to the registry. Pruned from the child's
#: allowlist by the same depth-1 rule so a deep child can't inline-spawn a
#: grandchild the supervisor would reject anyway.
SUBAGENT_SPAWN_INLINE_TOOL: str = "subagent_spawn_inline"

#: Sentinel error string surfaced on a privilege-escalation rejection.
#: Pinned in :attr:`TaskResult.error` so the LLM (and forensic queries)
#: can branch on the exact reason the child was refused.
TOOL_ALLOWLIST_ESCALATION_ERROR: str = "tool_allowlist_escalation"

#: Wildcard token honoured *only* on an :class:`AgentCard.tools_allowed`
#: entry. When the card's list contains exactly ``"*"`` the runner treats
#: it as "inherit the parent's full tool set" (no card-side narrowing).
#: Caller-supplied ``TaskSpec.tool_allowlist`` is **never** interpreted as
#: a wildcard — caller-side allowlists stay strict so a malicious /
#: confused parent LLM can't widen the child's reach beyond what its
#: card declared.
WILDCARD_TOOL: str = "*"

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    # Avoids forcing a runtime import of corlinman-persona for callers
    # who pass `persona_store=None`. The pyproject lists corlinman-persona
    # as a dep so it's *available* at runtime — but lazy import keeps
    # the cost out of the import-time path of corlinman_agent.subagent.
    from corlinman_persona.store import PersonaStore

logger = structlog.get_logger(__name__)


async def run_child(
    parent_ctx: ParentContext,
    agent_card: AgentCard,
    task: TaskSpec,
    *,
    provider: Any,
    child_seq: int = 0,
    persona_store: PersonaStore | None = None,
    tool_result_timeout: float = 0.05,
    parent_tools: Sequence[dict[str, Any]] | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    event_emitter: Any | None = None,
    model_override: str | None = None,
    parent_model: str | None = None,
    tool_executor: ChildToolExecutor | None = None,
) -> TaskResult:
    """Drive one child reasoning loop and return its :class:`TaskResult`.

    Parameters
    ----------
    parent_ctx
        Snapshot of the parent's identity. The runner *derives* the
        child context internally via :meth:`ParentContext.child_context`
        — the supervisor passes the **parent's** context, not the
        child's, so the depth-/agent-id-mangling logic stays in one
        place. The supervisor (iter 5+) is responsible for the
        depth-cap check before calling this.
    agent_card
        The child's agent card. ``agent_card.system_prompt`` becomes
        the child's system message and ``agent_card.name`` is mangled
        into the child's :attr:`ParentContext.parent_agent_id` (the
        spawned child's own ``agent_id`` from a persona-row perspective).
    task
        Wire-format request: ``goal`` is the child's only user-turn
        message, ``tool_allowlist`` is recorded but not yet filtered
        (iter 7), ``max_wall_seconds`` / ``max_tool_calls`` are
        recorded but not yet enforced (iter 6+).
    provider
        Anything matching the :class:`CorlinmanProvider` Protocol — same
        contract :class:`ReasoningLoop` itself takes. Using duck-typing
        rather than the imported Protocol means tests can pass the same
        ``_FakeProvider`` they use for the loop's own tests without
        importing the heavyweight provider module.
    child_seq
        Sequence number disambiguating siblings under the same parent.
        Default 0 is fine for a single child; concurrent fan-out
        callers (iter 8+) pass increasing values.
    persona_store
        If given, a fresh persona row is seeded for the child's mangled
        ``agent_id`` under the parent's ``tenant_id``. ``None`` skips
        seeding entirely — useful for unit tests that don't care about
        persona side effects. The seed is best-effort: a write failure
        logs a warning and the child still runs (it doesn't read
        persona state directly; the resolver does, on the next prompt
        render).
    tool_result_timeout
        Forwarded to :class:`ReasoningLoop`. Default 0.05s is the same
        as the loop's own default — for iter 4 (no tools wired) the
        loop short-circuits on the first round, so the value doesn't
        actually gate happy-path tests.
    parent_tools
        OpenAI-shaped tool schemas the *parent's* reasoning loop is
        configured with (each entry has at least ``{"function":
        {"name": "..."}}`` or a top-level ``"name"``). Iter 7 uses this
        list as both the *allowlist source-of-truth* (its names form the
        parent's ``tools_allowed``) and the *schema source* projected
        onto the child's :class:`ChatStart`. ``None`` is treated as the
        parent having no tools at all — child gets the empty list
        regardless of ``task.tool_allowlist`` (calling for tools the
        parent never had is itself escalation).
    max_depth
        The supervisor's ``[subagent].max_depth`` policy value. The
        runner reads it only for the ``subagent_spawn`` self-prune at
        ``child_ctx.depth == max_depth - 1`` — *not* for the depth-cap
        check itself, which still belongs to the supervisor (the runner
        is called by the supervisor *after* the cap admits the spawn).
        Defaults to :data:`api.DEFAULT_MAX_DEPTH` so unit tests don't
        need to thread the policy through; production callers (iter 8)
        pass the live policy value.
    model_override
        W1.1 — when set, overrides both ``agent_card.model`` and the
        empty placeholder in :class:`ChatStart.model` so the child's
        provider routing follows the parent's explicit choice. When
        ``None`` the card's own ``model`` binding takes effect; when
        the card has no model either, ``ChatStart.model`` stays
        ``""`` (the legacy placeholder — production callers replace
        this from the parent's resolved alias at the gateway layer).
        Precedence: ``model_override`` > ``agent_card.model`` >
        ``parent_model`` > ``""``.
    parent_model
        v1.12.2 — the parent's *resolved* model alias (e.g. the value of
        ``ChatStart.model`` the parent is running under). Used only as a
        fallback when neither ``model_override`` nor ``agent_card.model``
        is set: an ephemeral ``spawn_inline`` card has no model binding,
        and unlike top-level chats the gateway does not rewrite an empty
        child ``model``, so without this the spawn reaches the provider
        with ``model=""`` → 400 "model is required". Inheriting the
        parent's alias makes a model-less spawn run under the same model
        the parent uses.

    Returns
    -------
    TaskResult
        Always populated; on errors the runner catches the exception,
        logs, and returns a ``finish_reason=ERROR`` result with the
        exception's message in :attr:`TaskResult.error` rather than
        propagating. The Rust supervisor's ``finally`` releases the slot
        regardless, so a structured return path keeps the cap accounting
        deterministic.

    Notes on isolation guarantees verified by iter 4 tests:

    * The child's :class:`ChatStart.messages` contains only the
      ``role="system"`` prompt + the ``role="user"`` goal. Parent's
      chat history is **not** visible — iter 4 covers the
      ``include_parent_history=False`` default; the optional opt-in
      lands later (Open Question 1 in the design doc).
    * The child's session_key follows ``<parent_session>::child::<seq>``.
    * The child's ``agent_id`` follows ``<parent_agent>::<card>::<seq>``.
    * The persona row, when seeded, is keyed by the child's mangled
      ``agent_id`` under the parent's ``tenant_id`` — iter 5+ memory-host
      lookups will see it without colliding with the parent's row.
    """
    started_ms = _now_ms()
    child_ctx = parent_ctx.child_context(agent_card.name, child_seq)

    # Iter 7: tool-allowlist filter + escalation gate. Run *before*
    # persona seeding / loop construction so a rejected spawn produces
    # zero side effects (no orphaned persona row, no provider call).
    # ``parent_tool_names`` is the canonical source-of-truth for what
    # the parent is allowed to invoke; the child can never see anything
    # outside this set.
    parent_tool_names = _tool_names(parent_tools)
    try:
        child_tool_names = _filter_tools_for_child(
            parent_tool_names=parent_tool_names,
            card_tools_allowed=agent_card.tools_allowed,
            requested_allowlist=task.tool_allowlist,
            child_depth=child_ctx.depth,
            max_depth=max_depth,
        )
    except _ToolAllowlistEscalationError as exc:
        logger.info(
            "subagent.runner.tool_allowlist_escalation",
            child_session_key=child_ctx.parent_session_key,
            child_agent_id=child_ctx.parent_agent_id,
            offending_tools=sorted(exc.offending),
        )
        return TaskResult(
            output_text="",
            tool_calls_made=[],
            child_session_key=child_ctx.parent_session_key,
            child_agent_id=child_ctx.parent_agent_id,
            elapsed_ms=max(0, _now_ms() - started_ms),
            finish_reason=FinishReason.REJECTED,
            error=TOOL_ALLOWLIST_ESCALATION_ERROR,
        )

    # Project filtered names back onto schemas (the OpenAI-shaped dicts
    # the reasoning loop forwards to the provider). Empty allowlist →
    # empty schema list → loop runs as a pure LLM call.
    child_tools = _project_tool_schemas(parent_tools, child_tool_names)

    # Seed persona row before driving the loop. Best-effort: a failure
    # here would prevent the child from running which is heavier than
    # we want for an observability-only side effect.
    if persona_store is not None:
        try:
            await _seed_child_persona(persona_store, agent_card, child_ctx)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "subagent.runner.persona_seed_failed",
                child_agent_id=child_ctx.parent_agent_id,
                tenant_id=child_ctx.tenant_id,
                error=str(exc),
            )

    messages = _build_child_messages(agent_card, task)
    # W1.1 / v1.12.2: resolve the child's model with explicit precedence —
    # caller override > card binding > inherited parent model > legacy empty
    # placeholder. The third rung is the v1.12.2 fix: spawn_inline ephemeral
    # cards carry no model, and the gateway no longer rewrites an empty
    # ``model`` for *child* ChatStarts, so a bare spawn would reach the
    # provider with ``model=""`` and 400 ("model is required"). Inheriting
    # the parent's resolved alias makes a model-less spawn Just Work, while
    # the empty-string final rung stays byte-compat for callers that pass
    # neither (tests with fake providers that ignore the field).
    effective_model: str = (
        model_override
        or (agent_card.model or None)
        or (parent_model or None)
        or ""
    )
    chat_start = ChatStart(
        # ``model=""`` is a placeholder — real provider routing wires
        # this from the parent's resolved model alias in iter 8. Tests
        # supply a fake provider that ignores the model field.
        model=effective_model,
        messages=messages,
        # Iter 7: filtered+pruned schema list. Empty when the parent
        # had no tools, when ``task.tool_allowlist == []`` (the explicit
        # "pure LLM" mode), or when every parent tool was excluded by
        # the depth-prune (only ``subagent_spawn`` at the deepest legal
        # depth, in practice).
        tools=child_tools,
        session_key=child_ctx.parent_session_key,
    )

    # W3.2: child events bubble up to the parent's stream via the
    # ``event_emitter`` (typically a :class:`BubbleEmitter` constructed
    # by the dispatcher). ``None`` keeps the legacy no-observability
    # path so unit tests that don't wire an emitter still work.
    loop = ReasoningLoop(
        provider,
        tool_result_timeout=tool_result_timeout,
        event_emitter=event_emitter,
    )
    return await _drive_and_collect(
        loop,
        chat_start,
        child_ctx,
        started_ms,
        task,
        provider=provider,
        tool_executor=tool_executor,
        tool_result_timeout=tool_result_timeout,
        event_emitter=event_emitter,
        # D1: the child's *enforced* tool subset. Threaded down to the drain
        # so the execution boundary refuses any tool the model emits that is
        # not in this set — the advertised schema (built from the same names)
        # only HIDES out-of-allowlist tools; a model can still emit a hidden
        # name, and without this gate the gateway executor would run it with
        # the parent's authority. advertised-toolset == usable-toolset.
        allowed_tools=child_tool_names,
    )


#: Cooperative-shutdown grace period after a hard timeout fires. After
#: ``ReasoningLoop.cancel`` is signalled the runner waits up to this many
#: seconds for the loop's own cancel-aware paths to drain (yielding the
#: terminal :class:`ErrorEvent`) before force-dropping the loop task.
#: Matches design § "Timeout handling" — "wait 2s, drops the future".
_TIMEOUT_GRACE_SECONDS: float = 2.0


async def _drive_and_collect(
    loop: ReasoningLoop,
    chat_start: ChatStart,
    child_ctx: ParentContext,
    started_ms: int,
    task: TaskSpec,
    *,
    provider: Any,
    tool_executor: ChildToolExecutor | None = None,
    tool_result_timeout: float = 0.05,
    event_emitter: Any | None = None,
    allowed_tools: frozenset[str] | None = None,
) -> TaskResult:
    """Drain :meth:`ReasoningLoop.run` into a :class:`TaskResult`,
    enforcing :attr:`TaskSpec.max_wall_seconds` cooperatively.

    Iter 6 (this revision): the drain is wrapped in
    ``asyncio.wait_for(..., max_wall_seconds)``. On expiry the runner
    cooperates with the loop's existing cancel path
    (``ReasoningLoop.cancel("subagent_timeout")`` →
    ``ErrorEvent(reason="cancelled")``) for up to
    :data:`_TIMEOUT_GRACE_SECONDS`, then force-drops the task. Either
    way: the partial ``output_text`` collected so far is preserved
    verbatim and :attr:`FinishReason.TIMEOUT` lands on the result so
    the parent's LLM observes the wall-clock failure mode.

    The timeout is enforced from Python rather than from the Rust
    supervisor's ``tokio::time::timeout`` because the PyO3 bridge
    (iter 5) hands control to Python under a sync GIL acquisition;
    a parallel ``tokio::time::timeout`` cannot interrupt that. Putting
    the budget here keeps the contract self-consistent and lets unit
    tests exercise it without spinning Rust.
    """
    output_chunks: list[str] = []
    tool_calls: list[ToolCallSummary] = []
    executed_results: list[str] = []
    state: _DrainState = {
        "finish_reason": FinishReason.STOP,
        "error_msg": None,
    }

    drain_task: asyncio.Task[None] = asyncio.ensure_future(
        _drain_events(
            loop,
            chat_start,
            output_chunks,
            tool_calls,
            state,
            tool_executor=tool_executor,
            max_tool_calls=task.max_tool_calls,
            executed_results=executed_results,
            allowed_tools=allowed_tools,
        )
    )
    try:
        # ``asyncio.wait_for`` is the cooperative analogue the design
        # called for. ``task.max_wall_seconds`` is the hard ceiling; the
        # supervisor (iter 5) caps this from above via the policy
        # ``max_wall_seconds_ceiling`` (default 300 — see config block).
        await asyncio.wait_for(
            asyncio.shield(drain_task),
            timeout=float(task.max_wall_seconds),
        )
    except TimeoutError:
        # Cooperative cancel first: the loop's own cancel handler emits
        # an ErrorEvent and drains, which lets the drain coroutine exit
        # cleanly with the partial output already accumulated.
        loop.cancel("subagent_timeout")
        try:
            await asyncio.wait_for(
                asyncio.shield(drain_task),
                timeout=_TIMEOUT_GRACE_SECONDS,
            )
        except TimeoutError:
            # Cooperative grace exhausted — force-drop. ``cancel()`` on
            # the asyncio.Task throws CancelledError into the coroutine;
            # we suppress it because we've already captured whatever
            # the drain produced before the freeze.
            drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await drain_task
        state["finish_reason"] = FinishReason.TIMEOUT
        # Preserve any partial error_msg the loop set (e.g. cancelled
        # ErrorEvent). If none, leave error blank — TIMEOUT is itself
        # the failure indicator the parent's LLM branches on.
    except Exception as exc:  # pragma: no cover - belt and braces
        logger.warning(
            "subagent.runner.loop_uncaught",
            child_session_key=child_ctx.parent_session_key,
            error=str(exc),
        )
        state["error_msg"] = str(exc)
        state["finish_reason"] = FinishReason.ERROR
    finally:
        # D2 — never orphan the drain. ``asyncio.shield`` keeps ``drain_task``
        # alive when THIS coroutine is cancelled from the outside (e.g. a
        # ``spawn_many`` gather cancelling its siblings, or the parent turn
        # aborting): ``CancelledError`` is a ``BaseException``, so the
        # ``except Exception`` above does not catch it and ``wait_for`` leaves
        # the shielded task running detached — holding the live ReasoningLoop,
        # provider stream and tool_executor until GC. Cancel + drain it on
        # every exit path. The timeout branch already cancelled on grace-
        # exhaustion, so ``done()`` short-circuits there; the happy path has
        # ``done()`` True too, making this a no-op except under cancellation.
        if not drain_task.done():
            drain_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await drain_task

    output_text = "".join(output_chunks)

    # v1.12.3 — guaranteed-synthesis net. A child that executed tools but
    # whose model emitted no final answer text (a bare tool-call round) would
    # otherwise return ``output_text=""`` — the exact prod failure where
    # research subagents handed back only their search trajectory. When tools
    # ran and produced results but no answer surfaced, do ONE tools-disabled
    # round so the delegation always returns something usable. Skipped on
    # timeout/error (the loop already failed) and when no tools ran (a pure-LLM
    # child legitimately producing empty text is not this bug).
    if (
        not output_text.strip()
        and executed_results
        and tool_executor is not None
        and state["finish_reason"] not in (FinishReason.TIMEOUT, FinishReason.ERROR)
    ):
        # D10 — the forced-synthesis round must fit inside the child's
        # REMAINING wall-clock budget so ``max_wall_seconds`` stays a true
        # ceiling. The drain above already consumed part of the budget;
        # giving synthesis a fresh 30s on top let a "success-but-empty" child
        # overrun the advertised hard cap by up to 30s. Cap at the 30s
        # fallback ceiling AND at whatever budget is left; skip entirely when
        # the budget is already spent.
        synth_budget = min(
            _SYNTH_FALLBACK_TIMEOUT_SECONDS,
            float(task.max_wall_seconds) - (_now_ms() - started_ms) / 1000.0,
        )
        if synth_budget > 0:
            synth = await _synthesize_from_tool_results(
                provider=provider,
                base_messages=chat_start.messages,
                executed_results=executed_results,
                model=chat_start.model,
                session_key=chat_start.session_key,
                tool_result_timeout=tool_result_timeout,
                child_session_key=child_ctx.parent_session_key,
                timeout_seconds=synth_budget,
            )
            if synth.strip():
                output_text = synth
                # The model did finish — just on a second, forced turn. STOP
                # is the truthful terminal state for the parent's prompt.
                state["finish_reason"] = FinishReason.STOP

    elapsed_ms = max(0, _now_ms() - started_ms)
    return TaskResult(
        output_text=output_text,
        tool_calls_made=tool_calls,
        child_session_key=child_ctx.parent_session_key,
        child_agent_id=child_ctx.parent_agent_id,
        elapsed_ms=elapsed_ms,
        finish_reason=state["finish_reason"],
        error=state["error_msg"],
    )


#: Hard cap on the wall-clock for the forced final-synthesis round so the
#: belt-and-braces fallback can't itself hang the spawn. 30s is generous for
#: a single tools-disabled generation.
_SYNTH_FALLBACK_TIMEOUT_SECONDS: float = 30.0

#: Cap on how much tool-result text is fed into the synthesis fallback so a
#: handful of verbose ``web_search`` payloads don't blow the child's context.
_SYNTH_RESULT_DIGEST_CHARS: int = 12000


async def _synthesize_from_tool_results(
    *,
    provider: Any,
    base_messages: Sequence[dict[str, Any]],
    executed_results: list[str],
    model: str,
    session_key: str,
    tool_result_timeout: float,
    child_session_key: str,
    timeout_seconds: float = _SYNTH_FALLBACK_TIMEOUT_SECONDS,
) -> str:
    """Run ONE tools-disabled generation that turns the child's tool results
    into a final answer. The belt-and-braces half of the v1.12.3 fix.

    Builds ``base_messages + [user: "here are your tool results, write the
    final answer now, no more tools"]`` and drives a fresh
    :class:`ReasoningLoop` with ``tools=[]`` so the model physically cannot
    loop back into another tool round. Never raises — any failure logs and
    returns ``""`` (the caller keeps the empty output rather than crashing
    the spawn). Bounded by ``timeout_seconds`` — defaults to
    :data:`_SYNTH_FALLBACK_TIMEOUT_SECONDS`, but the caller passes the child's
    *remaining* wall-clock budget (D10) so this fallback can't push the spawn
    past its advertised ``max_wall_seconds`` ceiling.
    """
    if not executed_results:
        return ""
    digest = "\n\n".join(executed_results)
    if len(digest) > _SYNTH_RESULT_DIGEST_CHARS:
        digest = digest[:_SYNTH_RESULT_DIGEST_CHARS] + "\n…(truncated)"
    synth_messages: list[dict[str, Any]] = [
        *base_messages,
        {
            "role": "user",
            "content": (
                "你已经调用工具并获得以下结果：\n\n"
                + digest
                + "\n\n请基于以上结果，现在直接写出完整的最终答案，"
                "不要再调用任何工具。"
            ),
        },
    ]
    synth_start = ChatStart(
        model=model,
        messages=synth_messages,
        tools=[],
        session_key=session_key,
    )
    synth_loop = ReasoningLoop(provider, tool_result_timeout=tool_result_timeout)
    chunks: list[str] = []

    async def _pump() -> None:
        async for ev in synth_loop.run(synth_start):
            if isinstance(ev, TokenEvent):
                chunks.append(ev.text)

    try:
        await asyncio.wait_for(_pump(), timeout=timeout_seconds)
    except (TimeoutError, Exception) as exc:  # noqa: BLE001 — never crash the spawn
        logger.warning(
            "subagent.runner.synth_fallback_failed",
            child_session_key=child_session_key,
            error=str(exc),
        )
    return "".join(chunks)


# Drain-state contract: a tiny TypedDict-shaped dict the drain coroutine
# mutates so the cooperative-cancel path can observe partial output
# without racing the drain task's own return value. Plain `dict` keeps
# pyright happy without forcing a TypedDict import for two keys.
_DrainState = dict


async def _drain_events(
    loop: ReasoningLoop,
    chat_start: ChatStart,
    output_chunks: list[str],
    tool_calls: list[ToolCallSummary],
    state: _DrainState,
    *,
    tool_executor: ChildToolExecutor | None = None,
    max_tool_calls: int = 0,
    executed_results: list[str] | None = None,
    allowed_tools: frozenset[str] | None = None,
) -> None:
    """Pump the reasoning loop's event stream into shared collectors.

    Mutating shared lists (rather than returning a tuple) lets the
    timeout layer in :func:`_drive_and_collect` recover whatever was
    collected up to the moment the cancel fired. Without this contract
    the partial-output guarantee documented in design § "Timeout
    handling" wouldn't hold — a TaskCancelled would erase the
    intermediate state along with the task's local frame.

    v1.12.3 — when ``tool_executor`` is wired, the drain doesn't just
    *record* tool calls: it EXECUTES each one and feeds the result back via
    :meth:`ReasoningLoop.feed_tool_result`, exactly as the gateway's parent
    loop does. Without this the child's ``_collect_results`` timed out on a
    queue nothing fed, the loop exited after the tool round, and the model
    never produced a final answer (``output_text==""``). ``max_tool_calls``
    caps real tool execution (cost guard); past the cap we feed a
    "budget exhausted, wrap up" envelope instead of running the tool.
    """
    executed = 0
    try:
        async for event in loop.run(chat_start):
            if isinstance(event, TokenEvent):
                # ``is_reasoning`` tokens are the model's thinking trace —
                # we deliberately fold them into output_text so the
                # parent can still observe them; iter 8+ may decide to
                # split reasoning out into its own field.
                output_chunks.append(event.text)
            elif isinstance(event, ToolCallEvent):
                tool_calls.append(_summarise_tool_call(event))
                if tool_executor is None:
                    # Legacy / pure-LLM child: no executor wired. Record the
                    # call only (pre-v1.12.3 behaviour) — the loop will time
                    # out its result wait and end. Kept so no-tool children
                    # and unit tests that don't supply an executor still pass.
                    continue
                if allowed_tools is not None and event.tool not in allowed_tools:
                    # D1 — execution-boundary enforcement of the child's tool
                    # subset. ``_filter_tools_for_child`` only narrows the
                    # ADVERTISED schema; a model can still emit a tool name
                    # outside its allowlist, and the gateway executor would run
                    # it with the parent's full authority (privilege
                    # non-containment). Refuse here so advertised == usable.
                    # ``None`` keeps the legacy "no gate" path for callers /
                    # tests that drive the drain directly without an allowlist.
                    loop.feed_tool_result(
                        ToolResult(
                            call_id=event.call_id,
                            content=json.dumps(
                                {
                                    "error": "tool_not_in_allowlist",
                                    "tool": event.tool,
                                }
                            ),
                            is_error=True,
                        )
                    )
                    continue
                if max_tool_calls and executed >= max_tool_calls:
                    # Budget spent — stop running real tools but keep the
                    # conversation alive so the model writes its final answer.
                    loop.feed_tool_result(
                        ToolResult(
                            call_id=event.call_id,
                            content=json.dumps(
                                {
                                    "error": "tool_budget_exhausted",
                                    "note": (
                                        "工具调用次数已达上限，请基于已有结果"
                                        "直接给出最终答案，不要再调用工具。"
                                    ),
                                }
                            ),
                            is_error=True,
                        )
                    )
                    continue
                executed += 1
                result_json = await _execute_child_tool(tool_executor, event)
                if executed_results is not None:
                    executed_results.append(result_json)
                loop.feed_tool_result(
                    ToolResult(
                        call_id=event.call_id,
                        content=result_json,
                        is_error=_result_is_error(result_json),
                    )
                )
            elif isinstance(event, DoneEvent):
                state["finish_reason"] = _map_finish_reason(event.finish_reason)
            elif isinstance(event, ErrorEvent):
                state["error_msg"] = event.message
                state["finish_reason"] = FinishReason.ERROR
    except asyncio.CancelledError:
        # Re-raise so the wait_for sees cancellation. The shared lists
        # already carry whatever was drained before the cancel fired.
        raise


async def _execute_child_tool(
    executor: ChildToolExecutor,
    event: ToolCallEvent,
) -> str:
    """Run one child tool call through the executor, never raising.

    The executor (gateway-supplied) already folds its own failures into an
    ``{"error": ...}`` envelope, but we belt-and-brace here so a programmer
    error in the dispatch layer becomes a readable tool result the model can
    react to rather than tearing down the child's drain loop.
    """
    try:
        return await executor(event)
    except Exception as exc:  # noqa: BLE001 — tool exec must never crash the drain
        logger.warning(
            "subagent.runner.tool_exec_failed",
            tool=event.tool,
            call_id=event.call_id,
            error=str(exc),
        )
        return json.dumps({"error": f"tool_execution_failed: {exc}"})


def _result_is_error(result_json: str) -> bool:
    """Parse a tool-result envelope and report whether it signals an error.

    Mirrors the gateway's own check (agent_servicer ``_summarise``): an
    object carrying a truthy ``error`` or ``is_error`` key is an error.
    Malformed JSON counts as success (the model still reads the raw text).
    """
    try:
        parsed = json.loads(result_json or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return isinstance(parsed, dict) and bool(parsed.get("error") or parsed.get("is_error"))


class _ToolAllowlistEscalationError(Exception):
    """Internal signal raised by :func:`_filter_tools_for_child` when a
    request asks for tools the parent doesn't already hold.

    Caught in :func:`run_child` and translated into a rejected
    :class:`TaskResult` with ``error=tool_allowlist_escalation``. Not
    a public exception — callers see the rejection envelope, never
    this. Carries the offending tool names so the log line is
    actionable for operators.
    """

    def __init__(self, offending: set[str]) -> None:
        super().__init__(
            f"requested tools not in parent allowlist: {sorted(offending)!r}"
        )
        self.offending: frozenset[str] = frozenset(offending)


def _filter_tools_for_child(
    *,
    parent_tool_names: frozenset[str],
    card_tools_allowed: list[str] | None = None,
    requested_allowlist: list[str] | None,
    child_depth: int,
    max_depth: int,
) -> frozenset[str]:
    """Compute the child's effective tool-name set.

    Two narrowing layers stack on top of ``parent_tool_names`` — the
    *card's* declared ``tools_allowed`` (W1.1) and the *caller's*
    per-spawn ``requested_allowlist``. The result is the intersection
    of all three, then the depth-1 self-prune.

    Layer 1 — card narrowing (W1.1):

    * ``card_tools_allowed`` is ``None`` or ``[]`` (legacy / built-in
      defaults) → no card-side narrowing; child can see every tool
      the parent holds. Preserves the pre-W1.1 contract where the
      runner ignored the card's ``tools_allowed`` field entirely.
    * ``card_tools_allowed == ["*"]`` (W1.1 wildcard sentinel) →
      explicit "inherit parent's full set". Same effective behaviour
      as the legacy default, but the card *declares* the intent so an
      operator reading the YAML knows the agent is meant to inherit.
    * ``card_tools_allowed`` non-empty without the wildcard → narrow
      ``parent_tool_names`` to the intersection. A tool the card
      lists but the parent doesn't hold is silently dropped (the card
      is documenting "I'd like this tool if the parent has it");
      escalation is *only* a concern at layer 2.

    Layer 2 — caller's :class:`TaskSpec.tool_allowlist` (existing):

    * ``requested_allowlist is None`` (the default) → no further
      narrowing; child sees the layer-1 result.
    * ``requested_allowlist == []`` → empty set; pure LLM call.
      Distinct from ``None`` so the parent can opt the child out of
      all tools without the runner inferring "they meant inherit".
    * non-empty list → must be a subset of *layer 1's* result; any
      name outside raises :class:`_ToolAllowlistEscalationError`.
      The caller-side allowlist is **never** allowed to contain the
      wildcard token — a parent LLM can't widen the child's reach
      beyond what the card declared. Wildcard from the caller is
      treated as an unknown tool name and rejected via the standard
      escalation path.

    After resolution, prune ``subagent_spawn`` when the child is at the
    deepest depth that could still spawn a grandchild
    (``child_depth >= max_depth - 1``). The supervisor would refuse the
    grandchild's spawn anyway with :attr:`FinishReason.DEPTH_CAPPED`;
    we strip the tool entry so the LLM doesn't waste a round trying.

    Returns a :class:`frozenset` to make the result hash-eq comparable
    in tests and to telegraph immutability — the iter 9 hook event
    payload may capture this set verbatim, and we don't want callers
    accidentally mutating that record.
    """
    # The card's ``tools_allowed`` and the caller's ``requested_allowlist``
    # may use the dotted logical namespace (``web.search``, ``file.read``)
    # while ``parent_tool_names`` holds underscore wire names
    # (``web_search``, ``read_file``). Match in canonical space so a dotted
    # request isn't mis-flagged as an unknown tool (privilege escalation),
    # but keep the parent's REAL wire name in the effective set so the
    # child's advertised schema + execution-boundary check still use
    # dispatchable names. See ``corlinman_agent.tool_aliases``.
    #
    # Surface — but never reject — two real parent tools that canonicalize to
    # the same wire name: the fold below keys the last one to win, silently
    # dropping the other, so a single structured warning flags the ambiguity
    # for the operator. Behaviour is otherwise unchanged (#108 item 3).
    warn_alias_collisions(parent_tool_names, gate="subagent_allowlist")
    parent_by_canon = {canonicalize_tool_name(t): t for t in parent_tool_names}

    # ── Layer 1: card narrowing (W1.1). ──────────────────────────────
    if not card_tools_allowed:
        # Legacy / empty card list — inherit verbatim. Matches
        # pre-W1.1 behaviour so existing cards (which never had the
        # field consulted at runtime) keep working.
        card_effective: set[str] = set(parent_tool_names)
    elif WILDCARD_TOOL in card_tools_allowed:
        # Wildcard on the card — explicit inherit. Other entries
        # alongside the ``"*"`` are ignored (a card that says
        # ``["*", "foo"]`` already gets "foo" via the parent's set;
        # the wildcard alone is the canonical form).
        card_effective = set(parent_tool_names)
    else:
        # Explicit narrowing — intersect (in canonical space) with the
        # parent's set so a card advertising a tool the parent doesn't
        # hold is silently dropped (the parent's set is the hard ceiling).
        card_effective = {
            parent_by_canon[c]
            for t in card_tools_allowed
            if (c := canonicalize_tool_name(t)) in parent_by_canon
        }

    # ── Layer 2: caller's per-spawn allowlist (existing). ────────────
    if requested_allowlist is None:
        # Inherit layer-1: copy so the prune below doesn't mutate the
        # caller's view.
        effective = set(card_effective)
    else:
        card_by_canon = {canonicalize_tool_name(t): t for t in card_effective}
        # Escalation check in canonical space — empty list is a legal
        # subset of every set so it falls straight through to the prune.
        # ``offending`` keeps the caller's ORIGINAL names for the error.
        offending = {
            t for t in requested_allowlist
            if canonicalize_tool_name(t) not in card_by_canon
        }
        if offending:
            raise _ToolAllowlistEscalationError(offending)
        # Resolve each requested (possibly dotted) name to the matching
        # parent wire name so the child gets real, dispatchable tools.
        effective = {
            card_by_canon[canonicalize_tool_name(t)] for t in requested_allowlist
        }

    # NOTE: run_shell does NOT imply the task-control surface for a *child*.
    # A subagent is refused ``run_in_background=true`` (the child executor
    # rejects it — its bounded lifetime can't own a detached task), so a child
    # can never create a bg task of its own and has no legitimate use for
    # shell_task_output / shell_task_kill. Worse, child tool calls dispatch
    # under the PARENT's ``session_key``, so a child handed a parent task_id
    # would pass the registry's ownership gate and poll/kill the PARENT's job.
    # Implying the controls here would hand that cross-session reach to any
    # child granted plain run_shell, so the implication is intentionally
    # dropped for children (Codex #112 r7). A child only gets the control
    # tools if its card explicitly lists them.

    # D7 — single-level nesting (parent → child). EVERY spawned child
    # (``child_depth >= 1``) loses the spawn tools, regardless of the
    # configured ``max_depth``. This matches reality on two fronts: the
    # default policy is ``max_depth=1`` (a child may not spawn a grandchild),
    # AND the gateway's child tool-executor blanket-refuses every spawn tool
    # with ``subagent_no_recursive_spawn``. Advertising a spawn tool the
    # executor will always reject is the "advertise != usable" drift the audit
    # flagged — so we never advertise it to a child. (``max_depth`` is kept on
    # the signature for the supervisor cap contract / future deeper nesting;
    # the prune itself no longer keys off it.)
    if child_depth >= 1:
        effective.discard(SUBAGENT_SPAWN_TOOL)
        effective.discard(SUBAGENT_SPAWN_MANY_TOOL)
        effective.discard(SUBAGENT_SPAWN_INLINE_TOOL)

    return frozenset(effective)


def _tool_names(tools: Sequence[dict[str, Any]] | None) -> frozenset[str]:
    """Extract the OpenAI-shaped tool name set from a schema list.

    Recognises both the wrapped form (``{"type": "function", "function":
    {"name": "..."}}``) and the flat form (``{"name": "..."}``). The
    wrapped form is what the gateway forwards to providers; the flat
    form is what older tests and some adapters use. ``None`` / missing
    entries are skipped silently — a malformed entry isn't worth
    crashing the child over; it's just not visible to it.
    """
    if not tools:
        return frozenset()
    names: set[str] = set()
    for entry in tools:
        if not isinstance(entry, dict):
            continue
        # Wrapped form (the canonical OpenAI shape).
        function = entry.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            if isinstance(name, str) and name:
                names.add(name)
                continue
        # Flat form fallback.
        flat_name = entry.get("name")
        if isinstance(flat_name, str) and flat_name:
            names.add(flat_name)
    return frozenset(names)


def _project_tool_schemas(
    tools: Sequence[dict[str, Any]] | None,
    keep_names: frozenset[str],
) -> list[dict[str, Any]]:
    """Filter the parent's tool-schema list down to the names in
    ``keep_names``, preserving order.

    Order preservation matters for two reasons: (a) some providers
    deterministically prefer earlier-listed tools when the model is
    ambiguous; (b) golden-file tests on iter-8 wire payloads compare
    the JSON shape verbatim. Falls back to skipping malformed entries
    (same rationale as :func:`_tool_names`).
    """
    if not tools:
        return []
    out: list[dict[str, Any]] = []
    for entry in tools:
        if not isinstance(entry, dict):
            continue
        function = entry.get("function")
        name = function.get("name") if isinstance(function, dict) else entry.get("name")
        if isinstance(name, str) and name in keep_names:
            out.append(entry)
    return out


def _build_child_messages(
    agent_card: AgentCard, task: TaskSpec
) -> list[dict[str, Any]]:
    """Assemble the child's chat messages.

    Two-message minimum: ``system`` from the agent card + ``user``
    carrying the task goal. Parent history is **not** inherited —
    that's the whole point of subagent isolation. ``task.extra_context``
    is folded into the system prompt as ``[ctx.<key>]`` blocks; the
    keys are ``BTreeMap``-ordered on the Rust side so the rendered
    prompt is deterministic across processes.
    """
    system_parts: list[str] = []
    if agent_card.system_prompt:
        system_parts.append(agent_card.system_prompt)
    if task.extra_context:
        # Sort for determinism (matches Rust ``BTreeMap`` iteration).
        for key in sorted(task.extra_context.keys()):
            value = task.extra_context[key]
            system_parts.append(f"[ctx.{key}]\n{value}")

    messages: list[dict[str, Any]] = []
    if system_parts:
        messages.append({"role": "system", "content": "\n\n".join(system_parts)})
    messages.append({"role": "user", "content": task.goal})
    return messages


def _summarise_tool_call(event: ToolCallEvent) -> ToolCallSummary:
    """Compress a :class:`ToolCallEvent` into a :class:`ToolCallSummary`.

    The summary shape is fixed by the JSON wire envelope (see
    ``rust/crates/corlinman-subagent/src/types.rs::ToolCallSummary``).
    ``args_summary`` is a one-line synopsis — for iter 4 we just truncate
    the raw arguments JSON to 200 chars; iter 7 will let the tool plugin
    supply a custom summariser.
    """
    raw = event.args_json.decode("utf-8", errors="replace") if event.args_json else ""
    args_summary = raw[:200] + ("…" if len(raw) > 200 else "")
    return ToolCallSummary(
        name=event.tool or event.plugin or "unknown",
        args_summary=args_summary,
        # iter-4 has no per-call timing yet — iter 7 wires from
        # plugin executor latency. Zero is fine because the parent's
        # prompt is allowed to display it as "n/a".
        duration_ms=0,
    )


def _map_finish_reason(provider_reason: str) -> FinishReason:
    """Map :class:`DoneEvent.finish_reason` strings to :class:`FinishReason`.

    The reasoning loop emits the OpenAI-standard vocabulary
    (``"stop"`` / ``"length"`` / ``"tool_calls"`` / ``"content_filter"``).
    We promote ``"stop"`` and ``"length"`` to their direct counterparts.

    v1.12.3 — ``"tool_calls"`` now maps to :attr:`FinishReason.LENGTH`,
    not ``STOP``. The loop reports ``"tool_calls"`` when it ends ON a
    tool-call round without a following synthesis turn — i.e. the child
    was *truncated* mid-task, not cleanly done. Mapping it to ``STOP``
    made that failure silent (the parent saw a clean stop with empty
    text). ``LENGTH`` is the truthful "did not produce a final answer"
    signal. When tool execution + the synthesis fallback succeed, the
    terminal event is a genuine ``"stop"`` and this branch isn't hit.
    """
    match provider_reason:
        case "stop":
            return FinishReason.STOP
        case "length" | "tool_calls":
            return FinishReason.LENGTH
        case _:
            return FinishReason.STOP


async def _seed_child_persona(
    store: PersonaStore,
    agent_card: AgentCard,
    child_ctx: ParentContext,
) -> None:
    """Insert a default-shaped persona row for the child's mangled id.

    Mirrors :func:`corlinman_persona.seeder.seed_from_card` but bypasses
    the YAML round-trip: we already have the in-memory :class:`AgentCard`
    and the child's mangled ``agent_id`` is what we need to persist
    under the parent's ``tenant_id``. Skips the write if a row already
    exists (idempotent — re-runs of the same child during a test
    fixture replay don't double-seed).

    Lazy-imports :mod:`corlinman_persona.state` so callers that pass
    ``persona_store=None`` to :func:`run_child` don't pay the import
    cost — the dependency is declared in pyproject so this never fails
    in production but keeps the runtime graph minimal in tests that
    stub everything.
    """
    from corlinman_persona.state import PersonaState  # local import: see docstring

    existing = await store.get(
        child_ctx.parent_agent_id, tenant_id=child_ctx.tenant_id
    )
    if existing is not None:
        # Sibling re-runs / forensic replays: do NOT mutate. Matches
        # the seeder's "leave existing rows alone" stance.
        return

    state = PersonaState(
        agent_id=child_ctx.parent_agent_id,
        mood="neutral",
        fatigue=0.0,
        recent_topics=[],
        # ``upsert`` fills updated_at with "now" when we pass 0, which
        # is what the YAML seeder also relies on.
        updated_at_ms=0,
        state_json={},
    )
    await store.upsert(state, tenant_id=child_ctx.tenant_id)


def _now_ms() -> int:
    """Wall-clock milliseconds. Test fixtures monkey-patch this — keep
    the signature trivial."""
    return int(time.time() * 1000)


# Pylint quietener: ``replace`` re-export keeps the public surface tidy
# even though the runner doesn't itself dataclass-replace anything in
# iter 4. iter 6 will reach for it when overlaying timeout outcomes
# onto a partial TaskResult.
__all__ = [
    "SUBAGENT_SPAWN_INLINE_TOOL",
    "SUBAGENT_SPAWN_MANY_TOOL",
    "SUBAGENT_SPAWN_TOOL",
    "TOOL_ALLOWLIST_ESCALATION_ERROR",
    "WILDCARD_TOOL",
    "replace",
    "run_child",
]
