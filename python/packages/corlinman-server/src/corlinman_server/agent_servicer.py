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
import sys
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import grpc
import structlog
from corlinman_agent.agents import AgentCard, AgentCardRegistry, AgentExpander
from corlinman_agent.context_assembler import ContextAssembler, PlaceholderError
from corlinman_agent.hooks import LoggingHookEmitter
from corlinman_agent.permission import (
    ALLOW as _PERM_ALLOW,
    DENY as _PERM_DENY,
    LOG as _PERM_LOG,
    PermissionGate,
)
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
    APPLY_PATCH_TOOL,
    CODING_TOOLS,
    EDIT_FILE_TOOL,
    FileState,
    LIST_FILES_TOOL,
    READ_FILE_TOOL,
    REVERT_CHANGES_TOOL,
    RUN_SHELL_TOOL,
    SEARCH_FILES_TOOL,
    TODO_WRITE_TOOL,
    WRITE_FILE_TOOL,
    TodoStore,
    coding_tool_schemas,
    dispatch_apply_patch,
    dispatch_edit_file,
    dispatch_list_files,
    dispatch_read_file,
    dispatch_revert_changes,
    dispatch_run_shell,
    dispatch_search_files,
    dispatch_todo_write,
    dispatch_write_file,
    render_todo_block,
    resolve_workspace,
)
from corlinman_agent.coding._snapshot import snapshot as _snapshot_workspace
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

from corlinman_server.agent_journal import (
    AgentJournal,
    ResumeData,
    TURN_IN_PROGRESS,
)
from corlinman_server.gateway.services.chat_service import (
    _BUILTIN_OBSERVATION_PREFIX,
)
from corlinman_server.runner_pool import PoolStats, RunnerPool

logger = structlog.get_logger(__name__)

#: The "send file via current channel" tool — surfaced as a builtin so
#: the LLM can reply with a file (HTML, PDF, etc.) instead of dumping
#: raw text. The agent-side dispatch is a no-op stub: the actual upload
#: happens in the channel handler (`handle_one_telegram` /
#: `handle_one_qq`) which holds the channel sender + binding.
SEND_ATTACHMENT_TOOL = "send_attachment"


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
        SEND_ATTACHMENT_TOOL,
    }
) | CODING_TOOLS


def _send_attachment_tool_schema() -> dict[str, Any]:
    """OpenAI tool descriptor for the channel-side file-send tool."""
    return {
        "type": "function",
        "function": {
            "name": SEND_ATTACHMENT_TOOL,
            "description": (
                "Send a file from the local filesystem back to the user "
                "via the current chat channel (Telegram document/photo/"
                "voice; QQ private or group file). The file MUST already "
                "exist — write content to disk with `write_file` first "
                "if you need to create it. Use this whenever the user "
                "asks for a file (HTML, PDF, image, audio) instead of "
                "pasting the content as text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute filesystem path of the file to "
                            "send. The file must be readable by the "
                            "gateway process."
                        ),
                    },
                    "caption": {
                        "type": "string",
                        "description": (
                            "Optional caption shown alongside the file "
                            "(Telegram only; ignored on QQ)."
                        ),
                    },
                    "filename": {
                        "type": "string",
                        "description": (
                            "Optional display name for the file. "
                            "Defaults to the basename of `path`."
                        ),
                    },
                },
                "required": ["path"],
            },
        },
    }

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
        _send_attachment_tool_schema(),
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


#: Baseline coding-agent system prompt. Injected only when the assembled
#: context carries no system message of its own (no agent card matched).
#: Encodes behavioral rules C1–C12 from docs/RESEARCH_AGENT_PARITY.md §C,
#: adapted for a QQ-chatbot-shaped agent that also operates a real
#: workspace. A dynamic ``# Environment`` block (see ``_build_env_block``)
#: is appended at injection time.
_CODING_SYSTEM_PROMPT: str = """\
You are corlinman, an AI assistant that answers chat messages in a QQ \
client and can operate a real workspace — read, write and edit files, \
search code, run shell commands, search the web, and track multi-step \
work.

# Tone and output
Be concise and direct. Lead with the answer; skip preamble, filler, and \
recaps of what you are about to do. Plain chat-client text — no emoji \
unless the user uses them first, and no markdown heading deeper than `##`. \
When you reference code, cite it as `path:line`.

# Truthful reporting
Never claim something works, is fixed, or is complete unless you ran the \
relevant check and saw the result. Report the real output of commands and \
tests; if something failed, say so plainly with the actual error. Do not \
suppress, edit, or paraphrase failing output to look better. If you could \
not verify a claim, say "not verified" rather than implying success.

# Verify before "done"
Before declaring a task done: run the test, execute the script, or \
otherwise observe the change behaving the way you described. "It compiles" \
is not verification. If verification is impossible in this environment, \
say so and name what the user should run.

# Todo discipline
For any task with 3+ distinct steps, call `todo_write` first to lay out \
the plan, then keep it live. Keep exactly one step `in_progress`. Mark a \
step `completed` the moment it is verified — never batch completions at \
the end. Skip todos for trivial one-shot requests; do not pad small \
tasks with ceremonial todos.

# No speculative code
Do not write defensive code for cases that cannot occur. Do not add \
helpers for a single call site. Do not design for hypothetical future \
needs that nobody asked for. Build for the requirement in front of you.

# Read before edit
Never propose changes to code you have not read. Open the file, see the \
real contents and surrounding context, then edit. Edits applied to \
guessed-at text fail and waste a turn.

# Tool hierarchy
Prefer the dedicated tools — `read_file`, `write_file`, `edit_file`, \
`apply_patch`, `search_files`, `list_files`, `todo_write` — over \
`run_shell` for file and search work. Use `run_shell` for running code, \
tests, and tooling that has no dedicated wrapper. File tools are confined \
to your workspace directory; paths are workspace-relative.

# Destructive-action calibration
Local reversible actions inside the workspace (write, edit, scratch \
files) are free — just do them. Hard-to-reverse actions — deleting files \
the user did not ask you to delete, `rm -rf`, `git reset --hard`, force \
pushes, dropping a database, wiping a directory — require explicit user \
confirmation first. Never reach for a destructive operation as a \
shortcut when a forward fix would work.

# Respect user changes
If the user edited a file between your turns, treat their edits as \
intent. Do not revert, "clean up", or overwrite changes you did not make \
unless the user asked you to.

# Security default
When writing or reviewing code that touches user input, the network, the \
filesystem, secrets, or auth, default to catching the OWASP-top-10 class \
of issues: SQL/command injection, XSS, path traversal, SSRF, unsafe \
deserialization, leaked credentials, broken access control. Flag the \
risk; suggest the safer pattern.

# Ask only when blocked
Make a reasonable choice and proceed. Ask the user when you are truly \
blocked — ambiguous requirement with materially different solutions, or \
a destructive action that needs sign-off. Do not ask permission for \
routine work you are already authorized to do.

# Minimal comments
Add comments only where the code itself does not explain the WHY — \
non-obvious invariants, surprising trade-offs, links to the bug or spec \
that motivated the shape. Do not narrate what the next line does."""


def _build_env_block() -> str:
    """Build the dynamic ``# Environment`` system-prompt suffix.

    Recomputed on each call so the workspace path, date, and platform
    reflect the current process state. Workspace is resolved via the
    same env chain (`CORLINMAN_AGENT_WORKSPACE` →
    `CORLINMAN_DATA_DIR/workspace` → `~/.corlinman/workspace`) used by
    every coding tool.
    """
    workspace = resolve_workspace()
    py_version = (
        f"{sys.version_info.major}.{sys.version_info.minor}."
        f"{sys.version_info.micro}"
    )
    shell = os.environ.get("SHELL") or "unknown"
    today = date.today().isoformat()
    return (
        "# Environment\n"
        f"- workspace: {workspace}\n"
        f"- platform: {sys.platform}\n"
        f"- python: {py_version}\n"
        f"- shell: {shell}\n"
        f"- date: {today}"
    )


def _ensure_system_prompt(start: AgentChatStart) -> None:
    """Ensure ``start.messages`` carries a system message with the env block.

    When the assembled context has no system message, inject
    ``_CODING_SYSTEM_PROMPT`` plus a fresh ``# Environment`` block. When a
    system message is already present (an agent card matched, or a caller
    supplied one), preserve its content and append the env block — the
    env block is fact, not behavior, so it is always added. Mutates
    ``start`` in place. The CodexProvider lifts the leading system
    message into the Responses API ``instructions`` field.
    """
    env_block = _build_env_block()
    msgs = list(start.messages)
    for idx, m in enumerate(msgs):
        if isinstance(m, dict):
            role = m.get("role")
        else:
            role = getattr(m, "role", None)
        if role != "system":
            continue
        # Append the env block to the existing system message and stop.
        if isinstance(m, dict):
            content = m.get("content")
            base = content if isinstance(content, str) else ""
            new_msg = dict(m)
            new_msg["content"] = f"{base}\n\n{env_block}" if base else env_block
            msgs[idx] = new_msg
            start.messages = msgs
        else:
            # Non-dict message shape (e.g. an object). Leave as-is; the
            # injected env block is best-effort for the dict case.
            pass
        return
    start.messages = [
        {"role": "system", "content": f"{_CODING_SYSTEM_PROMPT}\n\n{env_block}"},
        *msgs,
    ]


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


# T1.4: keys aggregated by ``_CostMeter``. The first two are the durable
# cross-vendor pair; the rest are tracked when the provider reports them
# (Codex Responses API surfaces ``cached_input_tokens`` and
# ``reasoning_tokens``; Anthropic surfaces ``cache_read_input_tokens``).
# Unknown keys flow through unchanged so future providers don't need a
# meter-side change to be observed.
_COST_METER_BASE_KEYS = ("input_tokens", "output_tokens")


class _CostMeter:
    """Per-session token accumulator.

    One instance lives on the servicer for the process lifetime. Each
    turn's :class:`DoneEvent.usage` is folded into the running totals
    keyed by ``session_key``; an additional ``requests`` counter tracks
    the number of completed turns. No pricing math — model prices drift
    and the meter is the durable, vendor-neutral record. Cost dashboards
    consume :meth:`snapshot` and apply prices at read time.

    The meter is in-memory only. The servicer holds it like
    ``_todo_store`` — when the process dies the totals die with it. A
    future iteration may persist these to sqlite alongside the memory
    backend, but pricing tasks at request granularity also flow through
    the ``agent.cost.turn`` log line which is durable.
    """

    def __init__(self) -> None:
        # session_key → {token_key: int, …, "requests": int}.
        self._sessions: dict[str, dict[str, int]] = {}

    def add(self, session_key: str, usage: dict[str, int] | None) -> None:
        """Fold one turn's usage into the running totals.

        ``usage=None`` and ``session_key=""`` are tolerated as no-ops
        (legacy non-session callers, mid-stream errors). All integer
        values in ``usage`` are summed; the ``requests`` counter only
        bumps when usage was non-empty so it reflects observed cost
        events, not just any DoneEvent.
        """
        if not session_key or not usage:
            return
        bucket = self._sessions.setdefault(session_key, {})
        for key, value in usage.items():
            try:
                bucket[key] = bucket.get(key, 0) + int(value)
            except (TypeError, ValueError):
                # Defensive against weird upstream shapes; preserve the
                # rest of the usage dict.
                continue
        bucket["requests"] = bucket.get("requests", 0) + 1

    def snapshot(self, session_key: str) -> dict[str, int]:
        """Return a *copy* of the current totals for ``session_key``.

        Empty dict when the session has never recorded usage. Always a
        copy so admin callers can't mutate the meter's interior.
        """
        return dict(self._sessions.get(session_key, {}))


class CorlinmanAgentServicer(agent_pb2_grpc.AgentServicer):
    """Concrete implementation — replaces the default UNIMPLEMENTED stub."""

    def __init__(
        self,
        provider_resolver: _ResolverCallable | None = None,
        *,
        aliases: Mapping[str, AliasEntry] | None = None,
        context_assembler: Any | None = None,
        hook_bus: Any | None = None,
        permission_gate: PermissionGate | None = None,
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
        # Per-session task lists for the ``todo_write`` tool.
        self._todo_store = TodoStore()
        # T1.4: per-session token / cost accumulator. Updated from the
        # ``Chat`` DoneEvent branch when the provider reported usage;
        # ``cost_snapshot(session_key)`` exposes totals for a future
        # admin route.
        self._cost_meter = _CostMeter()
        # T3.2 hook bus — optional ``corlinman_hooks.HookBus``. When set,
        # ``_dispatch_builtin`` emits ``PreToolDispatch`` before and
        # ``ToolCalled`` after every builtin tool call. ``None`` means
        # no telemetry hook fan-out (the structlog event logs still
        # fire).
        self._hook_bus = hook_bus
        # T3.1 permission gate — declarative allow/deny/log per tool.
        # Constructed from env when not explicitly supplied so a stock
        # boot still gets one (default: allow-all).
        self._permission_gate = (
            permission_gate
            if permission_gate is not None
            else PermissionGate.from_env()
        )
        # T4.1 per-turn journal — opens lazily on first chat turn so a
        # smoke-test agent boot is unaffected. ``False`` once an init
        # failure has been logged so we don't retry every request.
        self._journal: AgentJournal | None = None
        self._journal_init_done = False
        self._journal_swept_stale = False
        # T4.2 per-session async lock — same-session RPCs serialize so
        # the todo store / cost meter / workspace snapshot can't race;
        # different sessions run concurrently as today.
        self._session_locks: dict[str, asyncio.Lock] = {}

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
        # T3.5: surface the session key as the Responses API prompt-cache
        # hint. The Codex provider only uses it when CORLINMAN_CODEX_
        # PROMPT_CACHE is set, so a stock boot remains identical.
        if start.session_key:
            extra: dict[str, Any] = dict(start.extra or {})
            extra.setdefault("prompt_cache_key", start.session_key)
            start.extra = extra
        start = await self._assemble_context(start)

        # Advertise the builtin tools to the model so it can call them.
        # Without this the loop only ever sees gateway-supplied tools
        # (plugins / MCP) — the calculator + web tools would be
        # dispatchable but invisible.
        _inject_builtin_tools(start)

        # Give the model a coding-agent system prompt when no agent card
        # supplied one — otherwise it operates the tools blind.
        _ensure_system_prompt(start)

        # Capture the user's text before any recall / todo block goes in
        # so it reflects the user's words (used for the post-turn memory
        # store *and* the snapshot label below).
        user_text = _last_user_text(start.messages)

        # T4.2: per-session async lock — same-session RPCs serialize so
        # the todo store / cost meter / workspace snapshot can't race.
        # Different sessions get distinct locks → real parallelism across
        # sessions. Held for the rest of the handler.
        session_lock = self._lock_for(start.session_key)
        await session_lock.acquire()
        lock_acquired = True

        # T4.1: open the journal lazily and look for a resumable turn
        # (same session_key + same user text within the resume window).
        # When found, prepend the prior turn's messages so the model
        # picks up where it left off; the tool results that already
        # landed are re-fed verbatim, so completed tools are not redone.
        journal = await self._get_journal()
        resume_data: ResumeData | None = None
        journal_turn_id: int | None = None
        if journal is not None and start.session_key and user_text:
            try:
                resume_data = await journal.find_resumable_turn(
                    start.session_key, user_text
                )
            except Exception as exc:  # noqa: BLE001 — degrade
                logger.warning("agent.journal.find_resumable_failed", error=str(exc))
        if resume_data is not None:
            # Use the resumed turn's id so post-dispatch appends + the
            # final complete/error stamp land on the same row.
            journal_turn_id = resume_data.turn_id
            replayed_tool_results = sum(
                1 for m in resume_data.messages if m.get("role") == "tool"
            )
            logger.info(
                "agent.chat.resumed",
                session=start.session_key,
                turn_id=journal_turn_id,
                replayed_tool_results=replayed_tool_results,
                replayed_messages=len(resume_data.messages),
                started_at_ms=resume_data.started_at_ms,
            )
            # Splice the replay history BEFORE the freshly-built start
            # messages, dropping the first user message of start (it's
            # the duplicate the resume already covers).
            replay = list(resume_data.messages)
            tail_messages = list(start.messages)
            # Strip the leading duplicate user turn — the replay already
            # contains a user message with the same text.
            for idx, msg in enumerate(tail_messages):
                role = (
                    msg.get("role") if isinstance(msg, dict)
                    else getattr(msg, "role", None)
                )
                if role == "user":
                    tail_messages.pop(idx)
                    break
            start.messages = replay + tail_messages
        elif journal is not None:
            try:
                journal_turn_id = await journal.begin_turn(
                    start.session_key, user_text
                )
                # Record the user message that started this turn so
                # resume can replay it.
                await journal.append_message(
                    journal_turn_id,
                    role="user",
                    content=user_text,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("agent.journal.begin_failed", error=str(exc))
                journal_turn_id = None

        # T2.4: snapshot the workspace so the agent (or the user) can
        # revert this turn's edits via ``revert_changes``. Best-effort —
        # degrades silently if git is missing on the host. Labelled with
        # the user message head so ``git log`` reads as a turn-by-turn
        # history.
        try:
            _snapshot_workspace(resolve_workspace(), user_text or "turn")
        except Exception as exc:  # noqa: BLE001 — never fail the chat
            logger.warning("agent.chat.snapshot_failed", error=str(exc))

        # T2.1: per-RPC file-read cache + staleness tracker. Threaded
        # into the file-tool dispatch below; other tools don't need it.
        file_state = FileState()

        # Automatic conversation memory: recall before answering.
        await self._recall_memory(start)

        # Re-show the session's task list so the model keeps sight of its
        # plan across turns (the todo_write tool persists it in-process).
        todo_block = render_todo_block(self._todo_store, start.session_key)
        if todo_block:
            start.messages = _inject_memory_note(
                list(start.messages), todo_block
            )

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
                        # Builtin tools (subagent.spawn{,_many}, blackboard.*,
                        # web/calc/coding) are dispatched in-process — the
                        # plugin runtime doesn't need to round-trip a result.
                        # We still emit an *observation-only* ToolCall frame
                        # so the gateway's chat stream surfaces tool calls
                        # to UI consumers (e.g. Telegram's mutable-spinner
                        # placeholder shows "🔧 调用工具: web_search").
                        # The ``_builtin:`` sentinel prefix on ``plugin``
                        # tells :mod:`gateway.services.chat_service` to skip
                        # ``executor.execute`` — otherwise it would round-
                        # trip a ``tool_result`` back to the loop, double-
                        # feeding the call_id that we already resolved
                        # in-process below.
                        yield agent_pb2.ServerFrame(
                            tool_call=agent_pb2.ToolCall(
                                call_id=event.call_id,
                                plugin=f"{_BUILTIN_OBSERVATION_PREFIX}{event.plugin}",
                                tool=event.tool,
                                args_json=event.args_json,
                                seq=seq,
                            )
                        )
                        seq += 1
                        logger.info(
                            "agent.tool.dispatch",
                            tool=event.tool,
                            call_id=event.call_id,
                            args=event.args_json.decode("utf-8", "replace")[:200],
                        )
                        result_json = await self._dispatch_builtin(
                            event, start, provider, file_state
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
                        # T4.1: journal the (assistant tool_call, tool result)
                        # pair so a future resume can replay completed tools
                        # instead of redoing them.
                        if journal is not None and journal_turn_id is not None:
                            try:
                                await journal.append_message(
                                    journal_turn_id,
                                    role="assistant",
                                    content="",
                                    tool_calls=[
                                        {
                                            "id": event.call_id,
                                            "type": "function",
                                            "function": {
                                                "name": event.tool,
                                                "arguments": event.args_json.decode(
                                                    "utf-8", "replace"
                                                ),
                                            },
                                        }
                                    ],
                                )
                                await journal.append_message(
                                    journal_turn_id,
                                    role="tool",
                                    content=result_json,
                                    tool_call_id=event.call_id,
                                )
                            except Exception as exc:  # noqa: BLE001
                                logger.warning(
                                    "agent.journal.append_tool_failed",
                                    error=str(exc),
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
                    # T4.4: stamp the turn errored so the breadcrumb sticks.
                    if journal is not None and journal_turn_id is not None:
                        try:
                            await journal.error_turn(
                                journal_turn_id,
                                f"{event.reason}: {event.message}",
                            )
                            journal_turn_id = None  # consumed
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "agent.journal.error_failed", error=str(exc)
                            )
                    yield _error_frame(event.reason, event.message)
                    return
                elif isinstance(event, DoneEvent):
                    # Store the completed turn so a later conversation can
                    # recall it. Best-effort — never blocks the Done frame.
                    await self._store_memory(
                        start.session_key, user_text, "".join(reply_parts)
                    )
                    # T4.1: journal the assistant's final reply + flip
                    # the turn to completed. Skip the assistant append
                    # when there is no text (pure tool-call turns).
                    if journal is not None and journal_turn_id is not None:
                        try:
                            final_text = "".join(reply_parts)
                            if final_text.strip():
                                await journal.append_message(
                                    journal_turn_id,
                                    role="assistant",
                                    content=final_text,
                                )
                            await journal.complete_turn(journal_turn_id)
                            journal_turn_id = None  # consumed
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "agent.journal.complete_failed", error=str(exc)
                            )
                    # T1.4: fold the turn's reported token usage into the
                    # per-session meter and log a structured per-turn
                    # record. ``usage`` is ``None`` when the provider did
                    # not report it (mid-stream errors, retries that
                    # bailed pre-completion) — silently skip in that case.
                    if event.usage:
                        self._cost_meter.add(start.session_key, event.usage)
                        logger.info(
                            "agent.cost.turn",
                            session=start.session_key,
                            model=start.model,
                            finish_reason=event.finish_reason,
                            **event.usage,
                        )
                    yield agent_pb2.ServerFrame(
                        done=agent_pb2.Done(finish_reason=event.finish_reason)
                    )
                    return
        except Exception as exc:
            # T4.4: stamp the turn errored so a follow-up Chat RPC can
            # find the breakage instead of seeing a phantom in_progress
            # row. Best-effort.
            if journal is not None and journal_turn_id is not None:
                try:
                    await journal.error_turn(
                        journal_turn_id, f"fatal: {exc!r}"[:1000]
                    )
                    journal_turn_id = None
                except Exception:  # noqa: BLE001
                    pass
            logger.exception("agent.chat.fatal", error=str(exc))
            yield _error_frame("unknown", str(exc))
        finally:
            inbound_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await inbound_task
            # T4.1: if the handler exited without a terminal event (cancel,
            # client disconnect, server stop), leave the turn marked
            # in_progress — a same-text retry within the resume window
            # will pick it back up. The boot-time sweep mops up turns that
            # never get a retry.
            # T4.2: always release the per-session lock.
            if lock_acquired:
                try:
                    session_lock.release()
                except RuntimeError:  # already released, defensive
                    pass

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

    # ------------------------------------------------------------------
    # T4.1 — Journal lifecycle (lazy open + boot-time stale sweep)
    # ------------------------------------------------------------------

    async def _get_journal(self) -> AgentJournal | None:
        """Lazily open the per-turn journal under ``<data_dir>/agent_journal.sqlite``.

        ``False`` once an init failure has been logged so we don't retry
        every request. The chat path is fully functional without a
        journal — it just loses the resume capability.
        """
        if self._journal_init_done:
            return self._journal
        self._journal_init_done = True
        try:
            path = _resolve_data_dir() / "agent_journal.sqlite"
            # ``open_from_env`` honours ``CORLINMAN_JOURNAL_BACKEND``;
            # unset / "sqlite" preserves the existing on-disk behavior
            # at ``path``. Future HA deployments can swap the backend
            # via env vars without touching this call site.
            self._journal = await AgentJournal.open_from_env(path)
            logger.info("agent.journal.opened", path=str(path))
        except Exception as exc:  # noqa: BLE001 — degrade silently
            logger.warning("agent.journal.init_failed", error=str(exc))
            self._journal = None
            return None
        # One-shot stale sweep on first open so a previously-crashed
        # gateway doesn't leave phantom in_progress rows.
        if not self._journal_swept_stale and self._journal is not None:
            self._journal_swept_stale = True
            try:
                await self._journal.mark_stale_in_progress_as_errored()
            except Exception as exc:  # noqa: BLE001
                logger.warning("agent.journal.sweep_failed", error=str(exc))
        return self._journal

    async def recent_errored_turns(
        self, session_key: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """T4.4 helper — recent errored turns for an operator / self-heal hook."""
        j = await self._get_journal()
        if j is None:
            return []
        return await j.recent_errored_turns(session_key, limit=limit)

    # ------------------------------------------------------------------
    # T4.2 — Per-session async lock
    # ------------------------------------------------------------------

    def _lock_for(self, session_key: str) -> asyncio.Lock:
        """Return the lock for ``session_key`` (creating one lazily).

        Empty session_key (one-shot HTTP callers) gets a NEW lock per
        call so they remain independent. Created locks linger for the
        process lifetime — the entries are tiny and bounded by the
        number of distinct chat sessions.
        """
        if not session_key:
            return asyncio.Lock()
        lock = self._session_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_key] = lock
        return lock

    def cost_snapshot(self, session_key: str) -> dict[str, int]:
        """Return the per-session token totals tracked by the cost meter.

        Shape: ``{"input_tokens": int, "output_tokens": int, …,
        "requests": int}``. Empty dict when the session has not yet
        produced a usage-bearing turn. T1.4 wires this into the
        ``Chat`` DoneEvent path; a future admin route can surface it
        to operators without poking at the meter directly.
        """
        return self._cost_meter.snapshot(session_key)

    # ------------------------------------------------------------------
    # T3.2 — hook bus emitters (no-op when no bus is configured)
    # ------------------------------------------------------------------

    def _emit_pre_tool_dispatch(
        self,
        event: ToolCallEvent,
        start: AgentChatStart,
        args_preview: str,
    ) -> None:
        if self._hook_bus is None:
            return
        try:
            from corlinman_hooks import HookEvent  # lazy: hooks dep is optional

            self._hook_bus.emit_nonblocking(
                HookEvent.PreToolDispatch(
                    tool=event.tool,
                    call_id=event.call_id,
                    args_preview=args_preview,
                    session_key_=start.session_key or "",
                )
            )
        except Exception as exc:  # noqa: BLE001 — never let a hook break a tool
            logger.warning("agent.tool.pre_emit_failed", error=str(exc))

    def _emit_tool_called(
        self,
        event: ToolCallEvent,
        start: AgentChatStart,
        *,
        ok: bool,
        duration_ms: int,
        error_code: str | None,
    ) -> None:
        if self._hook_bus is None:
            return
        try:
            from corlinman_hooks import HookEvent

            self._hook_bus.emit_nonblocking(
                HookEvent.ToolCalled(
                    tool=event.tool,
                    runner_id="builtin",
                    duration_ms=duration_ms,
                    ok=ok,
                    error_code=error_code,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent.tool.post_emit_failed", error=str(exc))

    async def _dispatch_builtin(
        self,
        event: ToolCallEvent,
        start: AgentChatStart,
        provider: CorlinmanProvider,
        file_state: FileState | None = None,
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

        # T3.2: pre-dispatch hook event — observers can audit / log /
        # plug their own policy on top of the gate. Fire-and-forget;
        # the dispatch path is authoritative.
        args_preview = event.args_json.decode("utf-8", "replace")[:200]
        self._emit_pre_tool_dispatch(event, start, args_preview)

        # T3.1: permission gate. ``deny`` short-circuits with a clean
        # ``permission_denied`` envelope; ``log`` is observer-only and
        # passes through; ``allow`` is the default.
        decision = self._permission_gate.decide(event.tool)
        if decision == _PERM_DENY:
            logger.warning(
                "agent.tool.denied",
                tool=event.tool,
                call_id=event.call_id,
                strict=self._permission_gate.strict,
            )
            result = json.dumps(
                {
                    "error": (
                        f"permission_denied: tool {event.tool!r} is not "
                        "permitted by the agent's permission rules"
                    ),
                    "tool": event.tool,
                }
            )
            self._emit_tool_called(event, start, ok=False, duration_ms=0,
                                   error_code="permission_denied")
            return result
        if decision == _PERM_LOG:
            logger.info(
                "agent.tool.logged",
                tool=event.tool,
                call_id=event.call_id,
            )

        started_at = time.perf_counter()
        ok = True
        error_code: str | None = None
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
                return dispatch_read_file(args_json=event.args_json, state=file_state)
            if event.tool == WRITE_FILE_TOOL:
                return dispatch_write_file(args_json=event.args_json, state=file_state)
            if event.tool == EDIT_FILE_TOOL:
                return dispatch_edit_file(args_json=event.args_json, state=file_state)
            if event.tool == LIST_FILES_TOOL:
                return dispatch_list_files(args_json=event.args_json)
            if event.tool == SEARCH_FILES_TOOL:
                return dispatch_search_files(args_json=event.args_json)
            if event.tool == RUN_SHELL_TOOL:
                return await dispatch_run_shell(args_json=event.args_json)
            if event.tool == APPLY_PATCH_TOOL:
                return dispatch_apply_patch(args_json=event.args_json)
            if event.tool == TODO_WRITE_TOOL:
                return dispatch_todo_write(
                    args_json=event.args_json,
                    store=self._todo_store,
                    session_key=start.session_key,
                )
            if event.tool == REVERT_CHANGES_TOOL:
                return dispatch_revert_changes(args_json=event.args_json)
            if event.tool == SEND_ATTACHMENT_TOOL:
                # No-op stub on the agent side. The real upload happens
                # in the channel handler (handle_one_telegram /
                # handle_one_qq) which observes the matching ToolCall
                # frame and has the sender + binding. We surface a
                # ``deferred_to_channel`` marker so the reasoning loop
                # treats the call as successful and stops re-invoking
                # the same tool in a loop. Errors during the actual
                # upload are reported to the user as a [corlinman error]
                # reply by the channel handler.
                try:
                    args = json.loads(
                        event.args_json.decode("utf-8") or "{}"
                    )
                except json.JSONDecodeError:
                    args = {}
                path = str(args.get("path") or "").strip()
                if not path:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "send_attachment requires a `path`",
                        }
                    )
                return json.dumps(
                    {
                        "ok": True,
                        "deferred_to_channel": True,
                        "note": (
                            "The channel handler is uploading the file. "
                            "Do not re-invoke send_attachment for the "
                            "same path; continue with the reply text."
                        ),
                    }
                )
        except Exception as exc:
            ok = False
            error_code = type(exc).__name__
            logger.exception(
                "agent.chat.builtin_tool_failed",
                tool=event.tool,
                call_id=event.call_id,
            )
            return json.dumps({"error": f"builtin_tool_failed: {exc}"})
        finally:
            # T3.2: post-dispatch hook event with timing + outcome.
            # Fires on every exit path (return, exception, fallthrough)
            # so subscribers get a complete trace of every tool call.
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            self._emit_tool_called(
                event, start,
                ok=ok, duration_ms=duration_ms, error_code=error_code,
            )
        # Unreachable in practice — BUILTIN_TOOLS is the gate above the
        # dispatch — but return a clean envelope rather than implicit None.
        # NOTE: ok/error_code were captured by the ``finally`` above as
        # the still-True default; subscribers seeing a tool name they
        # don't recognise should treat that as the diagnostic, not
        # rely on the ok flag here.
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
