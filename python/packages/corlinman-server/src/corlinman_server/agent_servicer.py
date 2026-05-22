"""``corlinman.v1.Agent`` gRPC servicer.

Implements the bidirectional streaming ``Chat`` RPC:

1. read the first :class:`ClientFrame`, expect ``ClientFrame.start``;
2. resolve a provider via :func:`corlinman_providers.registry.resolve`;
3. drive :class:`corlinman_agent.reasoning_loop.ReasoningLoop` and translate
   each yielded event into the matching :class:`ServerFrame` variant;
4. return — the client always closes the request half by dropping its
   ``mpsc::Sender<ClientFrame>``.

M1/M2 scope: ``ToolCall`` frames are emitted but we don't wait for a matching
``ToolResult`` — the gateway echoes an ``awaiting_plugin_runtime`` placeholder
and we advance to ``Done`` so the E2E pipeline completes. M3 flips this to a
full wait-for-ToolResult loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import grpc
import structlog
from corlinman_agent.agents import AgentCard, AgentCardRegistry, AgentExpander
from corlinman_agent.context_assembler import ContextAssembler, PlaceholderError
from corlinman_agent.hooks import LoggingHookEmitter
from corlinman_agent.placeholder_client import PlaceholderClient
from corlinman_agent.reasoning_loop import (
    Attachment as AgentAttachment,
)
from corlinman_agent.reasoning_loop import (
    ChatStart as AgentChatStart,
)
from corlinman_agent.reasoning_loop import (
    DoneEvent,
    ErrorEvent,
    ReasoningLoop,
    TokenEvent,
    ToolCallEvent,
    ToolResult,
)
from corlinman_agent.skills import SkillRegistry
from corlinman_agent.subagent import (
    SUBAGENT_SPAWN_MANY_TOOL,
    SUBAGENT_SPAWN_TOOL,
    ParentContext,
    dispatch_subagent_spawn,
    dispatch_subagent_spawn_many,
)
from corlinman_agent.subagent.blackboard import (
    BLACKBOARD_READ_TOOL,
    BLACKBOARD_WRITE_TOOL,
    BlackboardStore,
    dispatch_blackboard_read,
    dispatch_blackboard_write,
)
from corlinman_agent.coding import (
    CODING_TOOLS,
    EDIT_FILE_TOOL,
    LIST_FILES_TOOL,
    READ_FILE_TOOL,
    RUN_SHELL_TOOL,
    SEARCH_FILES_TOOL,
    WRITE_FILE_TOOL,
    coding_tool_schemas,
    dispatch_edit_file,
    dispatch_list_files,
    dispatch_read_file,
    dispatch_run_shell,
    dispatch_search_files,
    dispatch_write_file,
)
from corlinman_agent.variables import VariableCascade
from corlinman_agent.web import (
    CALCULATOR_TOOL,
    WEB_FETCH_TOOL,
    WEB_SEARCH_TOOL,
    calculator_tool_schema,
    dispatch_calculator,
    dispatch_web_fetch,
    dispatch_web_search,
    web_fetch_tool_schema,
    web_search_tool_schema,
)
from corlinman_grpc import agent_pb2, agent_pb2_grpc, common_pb2
from corlinman_providers import registry as provider_registry
from corlinman_providers.base import CorlinmanProvider, ProviderChunk
from corlinman_providers.specs import AliasEntry

from corlinman_server.runner_pool import PoolStats, RunnerPool

logger = structlog.get_logger(__name__)

#: Tool names dispatched in-process by the servicer rather than routed
#: through the Rust plugin registry. These cover the v0.7 multi-agent
#: surface (subagent fan-out + shared blackboard) plus the v0.8 web
#: tools (web_fetch / web_search) and a self-contained calculator;
#: adding to this set is the way to expose a new builtin tool that
#: doesn't fit the plugin model.
BUILTIN_TOOLS: frozenset[str] = frozenset(
    {
        SUBAGENT_SPAWN_TOOL,
        SUBAGENT_SPAWN_MANY_TOOL,
        BLACKBOARD_READ_TOOL,
        BLACKBOARD_WRITE_TOOL,
        WEB_FETCH_TOOL,
        WEB_SEARCH_TOOL,
        CALCULATOR_TOOL,
    }
) | CODING_TOOLS

#: Builtin tools advertised to the model on every chat turn so it can
#: actually *call* them. ``BUILTIN_TOOLS`` (above) is the dispatch gate;
#: this is the *discovery* surface. Kept to the keyless, low-risk tools
#: (calculator + web) — subagent fan-out / blackboard stay dispatch-only
#: until a deployment opts into them. Each entry is an OpenAI-shaped
#: ``{"type": "function", "function": {...}}`` descriptor.
def _builtin_tool_schemas() -> list[dict[str, Any]]:
    """Return the OpenAI tool descriptors for the advertised builtins.

    The web/calculator tools plus the coding surface (file ops, search,
    shell) — the agent's "operate a codebase" capability.
    """
    return [
        calculator_tool_schema(),
        web_search_tool_schema(),
        web_fetch_tool_schema(),
        *coding_tool_schemas(),
    ]


def _inject_builtin_tools(start: AgentChatStart) -> None:
    """Merge the advertised builtin tool schemas into ``start.tools``.

    Gateway-supplied tools (plugins / MCP, carried in ``tools_json``)
    win on a name clash — the builtin is only added when no tool of the
    same name is already present. Mutates ``start`` in place.
    """
    existing = start.tools or []
    have: set[str] = set()
    for t in existing:
        if isinstance(t, dict):
            fn = t.get("function")
            name = fn.get("name") if isinstance(fn, dict) else t.get("name")
            if name:
                have.add(str(name))
    merged = list(existing)
    for schema in _builtin_tool_schemas():
        name = schema.get("function", {}).get("name")
        if name and name not in have:
            merged.append(schema)
            have.add(str(name))
    start.tools = merged


class _MockProvider:
    """Offline provider used by the E2E smoke script.

    Activated by setting ``CORLINMAN_TEST_MOCK_PROVIDER`` in the environment —
    the value is streamed back verbatim as a single ``token`` chunk so the
    Rust gateway / Python loop can be exercised without network access.
    """

    def __init__(self, text: str) -> None:
        self._text = text

    async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        yield ProviderChunk(kind="token", text=self._text)
        yield ProviderChunk(kind="done", finish_reason="stop")


def _mock_resolver(_model: str) -> Any:
    text = os.environ.get("CORLINMAN_TEST_MOCK_PROVIDER", "")
    return _MockProvider(text)


# Feature C: the spec-driven resolver signature returns the merged params
# too. The injection surface still accepts the legacy 1-arg callable
# ``(model) -> provider`` used by every existing test; ``_call_resolver``
# normalises both shapes to the new triple.
_ResolvedTriple = tuple[CorlinmanProvider, str, dict[str, Any]]
_ResolverCallable = Callable[..., Any]


class CorlinmanAgentServicer(agent_pb2_grpc.AgentServicer):
    """Concrete implementation — replaces the default UNIMPLEMENTED stub."""

    def __init__(
        self,
        provider_resolver: _ResolverCallable | None = None,
        *,
        aliases: Mapping[str, AliasEntry] | None = None,
        context_assembler: Any | None = None,
    ) -> None:
        """Construct the servicer.

        ``provider_resolver`` defaults to :mod:`corlinman_providers.registry`.
        The indirection exists so tests can inject a fake provider without
        touching the global registry. If the caller doesn't supply one and
        ``CORLINMAN_TEST_MOCK_PROVIDER`` is set, a mock resolver is used —
        this drives the E2E smoke script without hitting the real network.

        ``aliases`` — the ``[models.aliases.<name>]`` map; forwarded to the
        registry's spec-driven ``resolve()``. Passing ``None`` leaves the
        resolver to fall through to the legacy prefix table for raw model
        ids (preserves M2 behaviour for existing deployments).
        """
        if provider_resolver is not None:
            self._resolve = provider_resolver
        elif os.environ.get("CORLINMAN_TEST_MOCK_PROVIDER") is not None:
            self._resolve = _mock_resolver
        else:
            self._resolve = provider_registry.resolve
        self._aliases: dict[str, AliasEntry] = dict(aliases or {})
        self._context_assembler = context_assembler
        # Builtin-tool runtime state. The agent registry is reused from
        # the context assembler when one is configured; the blackboard
        # store is created lazily on the first builtin dispatch so an
        # operator that never registers the orchestrator agent never
        # pays for an empty sqlite file.
        self._builtin_agents: AgentCardRegistry | None = None
        self._blackboard_store: BlackboardStore | None = None
        # v0.7.1 warm pool. Operators can call ``prewarm_providers`` at
        # boot to resolve known aliases before the first user request;
        # the SDK auth handshake then happens off the hot path. The
        # per-chat path itself still delegates to the provider
        # registry's existing memoisation — the pool is the lever for
        # per-tenant / sandboxed providers in v0.8+.
        self._provider_pool: RunnerPool[CorlinmanProvider] = RunnerPool(
            max_warm_per_key=int(os.environ.get("CORLINMAN_RUNNER_POOL_WARM", "2")),
            max_active_total=int(os.environ.get("CORLINMAN_RUNNER_POOL_MAX", "8")),
        )
        # Automatic conversation memory. Lazily opened on first chat turn;
        # ``False`` once an init failure has been logged so we don't retry
        # every request. Backed by LocalSqliteHost (FTS5 BM25 — no
        # embedding model needed).
        self._memory_host: Any = None
        self._memory_init_done = False

    async def Chat(  # noqa: N802 — gRPC method name
        self,
        request_iterator: AsyncIterator[agent_pb2.ClientFrame],
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[agent_pb2.ServerFrame]:
        start_frame = await _expect_start(request_iterator)
        if start_frame is None:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "first frame must be ClientFrame.start",
            )
            return

        start = _to_agent_start(start_frame.start)
        logger.info("agent.chat.start", model=start.model, session=start.session_key)

        # W-D1: per-agent model binding. Peek the messages for an agent
        # reference; if found, and the card declares ``model:`` while the
        # request itself omitted a model, the card's model wins. The
        # card's optional ``provider:`` is always passed as a resolver
        # hint regardless of who supplied the model id.
        bound_card = self._peek_agent_binding(start)
        if bound_card is not None and not start.model and bound_card.model:
            logger.info(
                "agent.chat.model_bound_from_card",
                agent=bound_card.name,
                model=bound_card.model,
            )
            start.model = bound_card.model
        provider_hint = bound_card.provider if bound_card is not None else None

        try:
            provider, upstream_model, merged_params = _call_resolver(
                self._resolve,
                start.model,
                self._aliases,
                provider_hint=provider_hint,
            )
        except KeyError as exc:
            yield _error_frame("model_not_found", str(exc))
            return

        # Feature C: thread merged params into the reasoning loop.
        # ``temperature`` and ``max_tokens`` have dedicated slots on
        # ``ChatStart``; everything else flows through as ``extra`` so the
        # provider adapter forwards it to the SDK call body.
        start.model = upstream_model
        _apply_merged_params(start, merged_params)
        start = await self._assemble_context(start)

        # Advertise the builtin tools to the model so it can call them.
        # Without this the loop only ever sees gateway-supplied tools
        # (plugins / MCP) — the calculator + web tools would be
        # dispatchable but invisible.
        _inject_builtin_tools(start)

        # Automatic conversation memory: recall before answering. The
        # user's text is captured first — both for the post-turn store
        # and so it reflects the user's words, not the recall note we
        # are about to inject.
        user_text = _last_user_text(start.messages)
        await self._recall_memory(start)

        # Bump the tool-result timeout above the M2 default (0.05s) so the
        # loop actually waits long enough for the gateway to round-trip a
        # ToolResult frame back. The servicer is now the real feedback
        # channel — the ``awaiting_plugin_runtime`` placeholder short-circuit
        # still protects us against runaway loops.
        loop = ReasoningLoop(provider, tool_result_timeout=30.0)

        inbound_task = asyncio.create_task(
            _pump_inbound(request_iterator, loop),
            name="agent.chat.pump_inbound",
        )

        seq = 0
        reply_parts: list[str] = []
        try:
            async for event in loop.run(start):
                if isinstance(event, TokenEvent):
                    if not event.is_reasoning:
                        reply_parts.append(event.text)
                    yield agent_pb2.ServerFrame(
                        token=agent_pb2.TokenDelta(
                            text=event.text,
                            is_reasoning=event.is_reasoning,
                            seq=seq,
                        )
                    )
                    seq += 1
                elif isinstance(event, ToolCallEvent):
                    if event.tool in BUILTIN_TOOLS:
                        # Builtin tools (subagent.spawn{,_many}, blackboard.*)
                        # are dispatched in-process. We never emit a ToolCall
                        # proto frame for them; the loop is fed the result
                        # directly. Failure here is folded into the loop's
                        # ToolResult envelope so the model can keep going.
                        logger.info(
                            "agent.tool.dispatch",
                            tool=event.tool,
                            call_id=event.call_id,
                            args=event.args_json.decode("utf-8", "replace")[:200],
                        )
                        result_json = await self._dispatch_builtin(
                            event, start, provider
                        )
                        logger.info(
                            "agent.tool.result",
                            tool=event.tool,
                            call_id=event.call_id,
                            result=result_json[:200],
                        )
                        loop.feed_tool_result(
                            ToolResult(
                                call_id=event.call_id,
                                content=result_json,
                                is_error=False,
                            )
                        )
                        continue
                    yield agent_pb2.ServerFrame(
                        tool_call=agent_pb2.ToolCall(
                            call_id=event.call_id,
                            plugin=event.plugin,
                            tool=event.tool,
                            args_json=event.args_json,
                            seq=seq,
                        )
                    )
                    seq += 1
                elif isinstance(event, ErrorEvent):
                    yield _error_frame(event.reason, event.message)
                    return
                elif isinstance(event, DoneEvent):
                    # Store the completed turn so a later conversation can
                    # recall it. Best-effort — never blocks the Done frame.
                    await self._store_memory(
                        start.session_key, user_text, "".join(reply_parts)
                    )
                    yield agent_pb2.ServerFrame(
                        done=agent_pb2.Done(finish_reason=event.finish_reason)
                    )
                    return
        except Exception as exc:
            logger.exception("agent.chat.fatal", error=str(exc))
            yield _error_frame("unknown", str(exc))
        finally:
            inbound_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await inbound_task

    # ─── v0.7.1 warm pool surface ─────────────────────────────────────

    def prewarm_providers(self, model_names: list[str] | tuple[str, ...]) -> None:
        """Resolve the configured provider for each model name at boot
        and park it warm in the pool. Operators wire this in
        ``main.py`` immediately after constructing the servicer so the
        first user request doesn't pay the SDK init cost.

        Resolution errors (missing alias, bad config) are logged and
        skipped — pre-warming is best-effort. The servicer keeps
        running with the cold path intact.
        """
        for name in model_names:
            try:
                provider, upstream_model, _ = _call_resolver(
                    self._resolve, name, self._aliases
                )
            except Exception as exc:
                logger.warning(
                    "agent.chat.prewarm_failed",
                    model=name,
                    error=str(exc),
                )
                continue
            key = (name, upstream_model)
            self._provider_pool.prewarm(key, lambda p=provider: p)
            logger.info(
                "agent.chat.prewarm_succeeded",
                model=name,
                upstream_model=upstream_model,
            )

    def pool_stats(self) -> PoolStats:
        """Snapshot of the provider pool counters. Surfaced for
        operator tooling (admin UI, ``corlinman doctor``)."""
        return self._provider_pool.stats()

    async def _dispatch_builtin(
        self,
        event: ToolCallEvent,
        start: AgentChatStart,
        provider: CorlinmanProvider,
    ) -> str:
        """Route an in-process builtin tool to its handler.

        Returns the JSON-encoded result string that the loop feeds back
        as ``ToolResult.content``. Never raises — any exception is
        folded into an ``{"error": "..."}`` envelope so the model's
        next reasoning round still has something to read.

        The parent context is derived from the chat ``start`` frame:
        ``session_key`` doubles as ``trace_id`` (the gateway carries a
        separate W3C ``traceparent`` for cross-service spans, but the
        evolution observer joins on ``session_key`` regardless) and the
        tenant_id falls back to a literal sentinel for single-tenant
        deployments.
        """
        tenant_id = start.session_key.split("::")[0] if start.session_key else "default"
        parent_ctx = ParentContext(
            tenant_id=tenant_id or "default",
            parent_agent_id=start.model or "agent",
            parent_session_key=start.session_key or "session",
            depth=0,
            trace_id=start.session_key or "",
        )
        try:
            if event.tool == SUBAGENT_SPAWN_TOOL:
                registry = self._get_agent_registry()
                if registry is None:
                    return json.dumps(
                        {"error": "agent_registry_unavailable"}
                    )
                return await dispatch_subagent_spawn(
                    args_json=event.args_json,
                    parent_ctx=parent_ctx,
                    agent_registry=registry,
                    provider=provider,
                    parent_tools=list(start.tools or []),
                )
            if event.tool == SUBAGENT_SPAWN_MANY_TOOL:
                registry = self._get_agent_registry()
                if registry is None:
                    return json.dumps(
                        {"tasks": [], "error": "agent_registry_unavailable"}
                    )
                return await dispatch_subagent_spawn_many(
                    args_json=event.args_json,
                    parent_ctx=parent_ctx,
                    agent_registry=registry,
                    provider=provider,
                    parent_tools=list(start.tools or []),
                )
            if event.tool == BLACKBOARD_READ_TOOL:
                return dispatch_blackboard_read(
                    args_json=event.args_json,
                    store=self._get_blackboard_store(),
                    trace_id=parent_ctx.trace_id,
                )
            if event.tool == BLACKBOARD_WRITE_TOOL:
                return dispatch_blackboard_write(
                    args_json=event.args_json,
                    store=self._get_blackboard_store(),
                    trace_id=parent_ctx.trace_id,
                    written_by=parent_ctx.parent_agent_id,
                )
            if event.tool == WEB_FETCH_TOOL:
                return await dispatch_web_fetch(args_json=event.args_json)
            if event.tool == WEB_SEARCH_TOOL:
                return await dispatch_web_search(args_json=event.args_json)
            if event.tool == CALCULATOR_TOOL:
                return dispatch_calculator(args_json=event.args_json)
            # Coding tools — workspace-confined file ops + shell.
            if event.tool == READ_FILE_TOOL:
                return dispatch_read_file(args_json=event.args_json)
            if event.tool == WRITE_FILE_TOOL:
                return dispatch_write_file(args_json=event.args_json)
            if event.tool == EDIT_FILE_TOOL:
                return dispatch_edit_file(args_json=event.args_json)
            if event.tool == LIST_FILES_TOOL:
                return dispatch_list_files(args_json=event.args_json)
            if event.tool == SEARCH_FILES_TOOL:
                return dispatch_search_files(args_json=event.args_json)
            if event.tool == RUN_SHELL_TOOL:
                return await dispatch_run_shell(args_json=event.args_json)
        except Exception as exc:
            logger.exception(
                "agent.chat.builtin_tool_failed",
                tool=event.tool,
                call_id=event.call_id,
            )
            return json.dumps({"error": f"builtin_tool_failed: {exc}"})
        # Unreachable in practice — BUILTIN_TOOLS is the gate above the
        # dispatch — but return a clean envelope rather than implicit None.
        return json.dumps({"error": f"unknown_builtin_tool: {event.tool}"})

    def _get_agent_registry(self) -> AgentCardRegistry | None:
        """Resolve the agent registry from the context assembler or
        lazy-load from the data dir. Returns ``None`` if no agents/ dir
        is configured; callers fall back to an error envelope."""
        if self._builtin_agents is not None:
            return self._builtin_agents
        assembler = self._get_context_assembler()
        if assembler is not None and getattr(assembler, "agents", None) is not None:
            self._builtin_agents = assembler.agents
            return self._builtin_agents
        try:
            data_dir = _resolve_data_dir()
            self._builtin_agents = AgentCardRegistry.load_from_dir(
                data_dir / "agents"
            )
            return self._builtin_agents
        except Exception as exc:
            logger.warning("agent.chat.agent_registry_load_failed", error=str(exc))
            return None

    def _get_blackboard_store(self) -> BlackboardStore:
        """Lazy-init the blackboard store. Single sqlite file under the
        data dir; created on first use."""
        if self._blackboard_store is not None:
            return self._blackboard_store
        data_dir = _resolve_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        self._blackboard_store = BlackboardStore(data_dir / "blackboard.sqlite")
        return self._blackboard_store

    def _peek_agent_binding(self, start: AgentChatStart) -> AgentCard | None:
        """W-D1: detect which agent the request references so we can apply
        its model / provider binding before the resolver runs.

        Returns the bound :class:`AgentCard` if the messages reference a
        registered agent, otherwise ``None``. The full assembler will
        re-run the same expansion later; running it twice is cheap (pure
        in-memory string scan) and lets us keep this binding logic
        completely separate from the placeholder / cascade pipeline.
        """
        registry = self._get_agent_registry()
        if registry is None or len(registry) == 0:
            return None
        try:
            expander = AgentExpander(registry, single_agent_gate=True)
            expansion = expander.expand(list(start.messages))
        except Exception as exc:
            # Never fail the dispatch over a peek — fall back to
            # request-body-driven routing exactly as pre-W-D1.
            logger.warning("agent.chat.binding_peek_failed", error=str(exc))
            return None
        if expansion.expanded_agent is None:
            return None
        return registry.get(expansion.expanded_agent)

    async def _assemble_context(self, start: AgentChatStart) -> AgentChatStart:
        assembler = self._get_context_assembler()
        if assembler is None:
            return start

        try:
            assembled = await asyncio.wait_for(
                assembler.assemble(
                    list(start.messages),
                    session_key=start.session_key,
                    model_name=start.model,
                    metadata=_context_metadata(start),
                ),
                timeout=_context_timeout_secs(),
            )
        except PlaceholderError as exc:
            logger.warning(
                "agent.chat.context_assembly_placeholder_failed",
                session=start.session_key,
                model=start.model,
                error=str(exc),
            )
            return start
        except Exception as exc:
            logger.warning(
                "agent.chat.context_assembly_failed",
                session=start.session_key,
                model=start.model,
                error=str(exc),
            )
            return start

        start.messages = assembled.messages
        return start

    def _get_context_assembler(self) -> Any | None:
        if self._context_assembler is None:
            self._context_assembler = _build_default_context_assembler()
        return self._context_assembler

    # ------------------------------------------------------------------
    # Automatic conversation memory
    # ------------------------------------------------------------------

    async def _get_memory_host(self) -> Any | None:
        """Lazily open the LocalSqlite memory host (FTS5 BM25, no
        embeddings). Returns ``None`` if the host cannot be opened — the
        chat path then runs memory-free."""
        if self._memory_init_done:
            return self._memory_host
        self._memory_init_done = True
        try:
            from corlinman_memory_host import LocalSqliteHost

            path = _resolve_data_dir() / "memory.sqlite"
            self._memory_host = await LocalSqliteHost.open("local", str(path))
            logger.info("agent.memory.opened", path=str(path))
        except Exception as exc:  # noqa: BLE001 — degrade, never crash chat
            logger.warning("agent.memory.init_failed", error=str(exc))
            self._memory_host = None
        return self._memory_host

    async def _recall_memory(self, start: AgentChatStart) -> None:
        """Recall recent conversation memory for this session and fold it
        into the system prompt.

        Conversational memory wants *recency*, not keyword relevance — the
        agent should see the recent history with this user, so we pull the
        most recent stored turns for the ``session_key`` namespace rather
        than running a BM25 match. No-op without a session key (one-shot
        HTTP callers) or a usable host.
        """
        if not start.session_key:
            return
        host = await self._get_memory_host()
        if host is None:
            return
        recent_fn = getattr(host, "recent", None)
        if recent_fn is None:
            return
        try:
            hits = await recent_fn(start.session_key, 8)
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent.memory.recall_failed", error=str(exc))
            return
        if not hits:
            return
        # ``recent`` returns newest-first; present oldest-first so the
        # injected block reads chronologically.
        recalled = "\n".join(f"- {h.content}" for h in reversed(hits))
        note = (
            "## Memory from earlier conversations with this user\n"
            f"{recalled}\n"
            "Use this context when relevant. Do not mention that you are "
            "recalling stored memory."
        )
        start.messages = _inject_memory_note(list(start.messages), note)
        logger.info(
            "agent.memory.recalled", session=start.session_key, hits=len(hits)
        )

    async def _store_memory(
        self, session_key: str, user_text: str, reply_text: str
    ) -> None:
        """Persist the completed turn so a later conversation can recall
        it. Best-effort — a failure is logged and swallowed."""
        if not session_key or not user_text.strip():
            return
        host = await self._get_memory_host()
        if host is None:
            return
        try:
            from corlinman_memory_host import MemoryDoc

            content = (
                f"User said: {user_text.strip()[:1000]}\n"
                f"Assistant replied: {reply_text.strip()[:1000]}"
            )
            await host.upsert(
                MemoryDoc(content=content, namespace=session_key)
            )
            logger.info("agent.memory.stored", session=session_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent.memory.store_failed", error=str(exc))


def _build_default_context_assembler() -> ContextAssembler | None:
    try:
        data_dir = _resolve_data_dir()
        return ContextAssembler(
            agents=AgentCardRegistry.load_from_dir(_resolve_skill_dir(data_dir, "agents")),
            variables=VariableCascade(
                data_dir / "TVStxt" / "tar",
                data_dir / "TVStxt" / "var",
                data_dir / "TVStxt" / "sar",
                data_dir / "TVStxt" / "fixed",
                hot_reload=False,
            ),
            skills=SkillRegistry.load_from_dir(_resolve_skill_dir(data_dir, "skills")),
            placeholder_client=PlaceholderClient(),
            hook_emitter=LoggingHookEmitter(),
            config_lookup=lambda key: os.environ.get(key),
        )
    except Exception as exc:
        logger.warning("agent.chat.context_assembler_init_failed", error=str(exc))
        return None


def _resolve_data_dir() -> Path:
    raw = os.environ.get("CORLINMAN_DATA_DIR")
    if raw:
        return Path(raw)
    return Path.home() / ".corlinman"


def _last_user_text(messages: Sequence[Any]) -> str:
    """Extract the trailing user turn's text from a message list.

    Handles both plain-string content and the OpenAI multimodal
    content-parts list. Returns ``""`` when there is no user turn.
    """
    for msg in reversed(list(messages)):
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
        if role != "user":
            continue
        content = (
            msg.get("content") if isinstance(msg, dict)
            else getattr(msg, "content", None)
        )
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                str(p.get("text", ""))
                for p in content
                if isinstance(p, dict) and p.get("type") in ("text", "input_text")
            ]
            return " ".join(parts).strip()
        return ""
    return ""


def _inject_memory_note(messages: list[Any], note: str) -> list[dict[str, Any]]:
    """Fold a memory recall ``note`` into the system prompt.

    Appends to the leading system message when present; otherwise
    prepends a fresh system message. Returns a new list (the input is
    not mutated). Non-dict messages are coerced through ``role`` /
    ``content`` attribute reads so object-shaped messages still work.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, dict):
            out.append(dict(m))
        else:
            out.append({
                "role": getattr(m, "role", ""),
                "content": getattr(m, "content", ""),
            })
    if out and out[0].get("role") == "system" and isinstance(out[0].get("content"), str):
        out[0]["content"] = f"{out[0]['content']}\n\n{note}"
    else:
        out.insert(0, {"role": "system", "content": note})
    return out


def _resolve_skill_dir(data_dir: Path, name: str) -> Path:
    """Resolve a context-asset dir (``skills`` / ``agents``).

    Bundled skills are seeded by the gateway into
    ``<data_dir>/profiles/default/<name>/`` (see
    :mod:`corlinman_server.gateway.lifecycle.starter_skills`). The bare
    ``<data_dir>/<name>/`` form is the legacy/test layout. Prefer the
    profile dir when it exists so the agent picks up the 16 starter
    skills; fall back to the flat dir otherwise.
    """
    profile_dir = data_dir / "profiles" / "default" / name
    if profile_dir.is_dir():
        return profile_dir
    return data_dir / name


def _context_metadata(start: AgentChatStart) -> dict[str, str]:
    md: dict[str, str] = {}
    if start.session_key:
        md["session_key"] = start.session_key
    return md


def _context_timeout_secs() -> float:
    raw = os.environ.get("CORLINMAN_CONTEXT_ASSEMBLY_TIMEOUT_S")
    if not raw:
        return 2.0
    try:
        return max(0.1, float(raw))
    except ValueError:
        return 2.0


async def _expect_start(
    iterator: AsyncIterator[agent_pb2.ClientFrame],
) -> agent_pb2.ClientFrame | None:
    """Drain the iterator until the first frame; return it if it carries a
    ``ChatStart``, else ``None``."""
    async for frame in iterator:
        if frame.WhichOneof("kind") == "start":
            return frame
        return None
    return None


async def _pump_inbound(
    iterator: AsyncIterator[agent_pb2.ClientFrame],
    loop: ReasoningLoop,
) -> None:
    """Forward post-ChatStart :class:`ClientFrame` messages to the loop.

    * ``tool_result`` → :meth:`ReasoningLoop.feed_tool_result`
    * ``cancel`` → :meth:`ReasoningLoop.cancel` and return
    * ``approval`` → logged only (S5 wires this into an approval gate)
    * duplicate ``start`` / unknown kinds → ignored
    """
    async for frame in iterator:
        kind = frame.WhichOneof("kind")
        if kind == "tool_result":
            tr = frame.tool_result
            content = tr.result_json.decode("utf-8", errors="replace")
            loop.feed_tool_result(
                ToolResult(
                    call_id=tr.call_id,
                    content=content,
                    is_error=tr.is_error,
                )
            )
            logger.debug(
                "agent.chat.tool_result_in",
                call_id=tr.call_id,
                is_error=tr.is_error,
                duration_ms=tr.duration_ms,
            )
        elif kind == "cancel":
            reason = frame.cancel.reason or "client_cancel"
            logger.info("agent.chat.cancel_in", reason=reason)
            loop.cancel(reason=reason)
            return
        elif kind == "approval":
            # S5 will wire this into an approval gate; today we just log.
            logger.debug(
                "agent.chat.approval_received_but_not_wired",
                call_id=frame.approval.call_id,
                approved=frame.approval.approved,
            )
        elif kind == "start":
            logger.warning("agent.chat.duplicate_start_ignored")
        # Unknown kinds silently ignored — protobuf forward compatibility.


def _to_agent_start(pb_start: agent_pb2.ChatStart) -> AgentChatStart:
    """Convert a protobuf ``ChatStart`` into the agent's dataclass form."""
    messages = [
        {"role": _role_name(m.role), "content": m.content}
        for m in pb_start.messages
    ]
    attachments = [_to_agent_attachment(a) for a in pb_start.attachments]
    return AgentChatStart(
        model=pb_start.model,
        messages=messages,
        tools=_decode_tools_json(pb_start.tools_json),
        session_key=pb_start.session_key,
        temperature=pb_start.temperature or None,
        max_tokens=pb_start.max_tokens or None,
        attachments=attachments,
    )


def _decode_tools_json(raw: bytes) -> list[dict[str, Any]]:
    """Decode OpenAI ``tools`` JSON carried by the protobuf frame."""
    if not raw:
        return []
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        logger.warning("agent.chat.tools_json_invalid", error=str(exc))
        return []
    if not isinstance(decoded, list):
        logger.warning("agent.chat.tools_json_not_array")
        return []
    return [item for item in decoded if isinstance(item, dict)]


def _to_agent_attachment(pb: agent_pb2.Attachment) -> AgentAttachment:
    """Convert a protobuf ``Attachment`` to the agent dataclass.

    Empty strings / empty bytes on the proto side (the default for
    unset fields) map to ``None`` so providers can distinguish "unset"
    from "explicitly empty".
    """
    kind = _attachment_kind_name(pb.kind)
    return AgentAttachment(
        kind=kind,
        url=pb.url or None,
        bytes_=bytes(pb.bytes) if pb.bytes else None,
        mime=pb.mime or None,
        file_name=pb.file_name or None,
    )


def _attachment_kind_name(kind: Any) -> str:
    """Map ``AttachmentKind`` enum → lower-case string used in the dataclass.

    ``kind`` is the protobuf ``AttachmentKind`` wrapper (behaves like an
    int); typed as ``Any`` because the generated stub exposes the enum
    values as a custom wrapper class that mypy can't index against.
    """
    if kind == agent_pb2.ATTACHMENT_KIND_IMAGE:
        return "image"
    if kind == agent_pb2.ATTACHMENT_KIND_AUDIO:
        return "audio"
    if kind == agent_pb2.ATTACHMENT_KIND_VIDEO:
        return "video"
    return "file"


def _role_name(role: common_pb2.Role) -> str:
    mapping: dict[common_pb2.Role, str] = {
        common_pb2.USER: "user",
        common_pb2.ASSISTANT: "assistant",
        common_pb2.SYSTEM: "system",
        common_pb2.TOOL: "tool",
    }
    return mapping.get(role, "user")


def _call_resolver(
    resolve: _ResolverCallable,
    alias_or_model: str,
    aliases: Mapping[str, AliasEntry],
    *,
    provider_hint: str | None = None,
) -> _ResolvedTriple:
    """Call ``resolve`` with whichever signature it exposes.

    New-style resolvers (``ProviderRegistry.resolve``) take
    ``alias_or_model=`` + ``aliases=`` kwargs and return a triple. Legacy
    test resolvers are 1-arg ``(model) -> provider`` callables — for those
    we normalise to ``(provider, model, {})`` so the downstream code is
    signature-agnostic.

    ``provider_hint`` (W-D1) is forwarded to new-style resolvers when
    set. Legacy 1-arg resolvers ignore it; this preserves the existing
    test-injection contract.
    """
    # Prefer the new keyword-only form; fall back to the legacy 1-arg form.
    # Two-step degrade: try with provider_hint first; on TypeError (old
    # resolver without the kwarg) retry without it; on a further
    # TypeError fall back to the 1-arg legacy shape.
    try:
        result = resolve(
            alias_or_model=alias_or_model,
            aliases=aliases,
            provider_hint=provider_hint,
        )
    except TypeError:
        try:
            result = resolve(alias_or_model=alias_or_model, aliases=aliases)
        except TypeError:
            result = resolve(alias_or_model)
    if isinstance(result, tuple) and len(result) == 3:
        provider, model, params = result
        return provider, model, dict(params or {})
    # Legacy single-provider return.
    return result, alias_or_model, {}


def _apply_merged_params(start: AgentChatStart, params: Mapping[str, Any]) -> None:
    """Apply merged params onto a :class:`ChatStart`.

    ``temperature`` / ``max_tokens`` live in dedicated fields — a
    non-``None`` request-level value already on ``start`` wins over the
    merged default (request ≻ alias ≻ provider). Everything else is
    dumped into ``start.extra`` for the provider adapter to forward.
    """
    if not params:
        return
    extra: dict[str, Any] = dict(start.extra or {})
    for key, value in params.items():
        if key == "temperature":
            if start.temperature is None:
                start.temperature = float(value)
            continue
        if key == "max_tokens":
            if start.max_tokens is None:
                start.max_tokens = int(value)
            continue
        extra[key] = value
    start.extra = extra


def _error_frame(reason: str, message: str) -> agent_pb2.ServerFrame:
    return agent_pb2.ServerFrame(
        error=common_pb2.ErrorInfo(
            reason=_reason_to_proto(reason),
            message=message,
            retryable=reason in ("rate_limit", "timeout", "overloaded", "unknown"),
        )
    )


def _reason_to_proto(reason: str) -> common_pb2.FailoverReason:
    mapping: dict[str, common_pb2.FailoverReason] = {
        "billing": common_pb2.BILLING,
        "rate_limit": common_pb2.RATE_LIMIT,
        "auth": common_pb2.AUTH,
        "auth_permanent": common_pb2.AUTH_PERMANENT,
        "timeout": common_pb2.TIMEOUT,
        "model_not_found": common_pb2.MODEL_NOT_FOUND,
        "format": common_pb2.FORMAT,
        "context_overflow": common_pb2.CONTEXT_OVERFLOW,
        "overloaded": common_pb2.OVERLOADED,
    }
    return mapping.get(reason, common_pb2.UNKNOWN)
