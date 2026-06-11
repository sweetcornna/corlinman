# PLAN — `corlinman console`: interactive CLI agent console ("neural-brain REPL")

> Goal (2026-06-11): 借鉴 hermes-agent 的方式做 CLI 控制台，充当 agent 最内核的神经大脑来执行各种任务。
> 设计参考：claude-code 为主（oboard/claude-code-rev + claude-code 2.1.88 restored source），
> opencode 为辅（anomalyco/opencode）。简单任务路由给更便宜的模型。支持 workflow / multi-agent。

## 0. What exists already (verified against live tree, 2026-06-11)

| Piece | Where | Status |
|---|---|---|
| Agent loop (full brain: tools/subagents/memory) | `corlinman_server/agent_servicer.py` `CorlinmanAgentServicer` driving `corlinman_agent.reasoning_loop.ReasoningLoop` | done |
| In-process hosting of that servicer | `gateway/grpc/agent_server.py` (`serve_agent`, UDS/TCP, loopback-guarded) + standalone `corlinman_server/main.py` | done |
| Gateway-side chat facade | `gateway/services/chat_service.py` `ChatService(backend, tool_executor)` → `run(InternalChatRequest, cancel) -> AsyncIterator[InternalChatEvent]` | done |
| Backends | `GrpcAgentChatBackend` (full agent over gRPC), `DirectProviderBackend` (provider-only, no tools) | done |
| Subagents / multi-agent | `subagent.spawn` / `spawn_many` / `spawn_inline` tools, `Supervisor` caps, `agents/*.yaml` cards, blackboard | done (servicer-side) |
| Per-agent / per-request model binding | `AgentCard.model/provider`, `InternalChatRequest.model` + `provider_hint`, `ProviderRegistry.resolve(aliases=…)` | done |
| Ops CLI (`corlinman`) | `corlinman_server/cli/main.py` click group — onboard/init/doctor/… | done, **no interactive console** |
| Sessions | `AgentJournal` (sqlite/postgres) keyed by `session_key`; servicer journals turns | done |

The missing piece is exactly an interactive console: a terminal REPL that boots/talks to the
brain, streams tokens + tool-call progress, supports slash commands, sessions, and model routing.

## 1. Architecture

```
corlinman console [PROMPT] [--attach URL] [--model M] [--agent CARD]
                  [--session KEY] [--data-dir DIR] [--print/-p]

┌────────────────────────── console process ──────────────────────────┐
│ app.py      REPL loop (prompt_toolkit) — hermes dual-queue input    │
│ commands.py slash commands (/help /new /model /sessions …)          │
│ render.py   rich renderer — stream tokens, tool progress, status    │
│ brain.py    BrainSession — turn driver, cancel, session state       │
│   ├── embedded.py  (default) boot CorlinmanAgentServicer on a       │
│   │                private UDS in-process → AgentClient →           │
│   │                GrpcAgentChatBackend → ChatService               │
│   │                fallback: DirectProviderBackend (no tools)       │
│   └── attach.py    (--attach) SSE client → running gateway          │
│                    /v1/chat/completions  (opencode client/server)   │
│ router.py   model routing — small_fast_model for simple/utility     │
└──────────────────────────────────────────────────────────────────────┘
```

### Mode A — embedded (default; hermes "CLI is the agent")
1. `resolve_data_dir()` + load `config.toml`; export the providers/aliases drop the servicer
   expects (same shape `CORLINMAN_PY_CONFIG` carries) or pass a resolver directly.
2. Start `grpc.aio.server` with `CorlinmanAgentServicer(...)` bound to a **private per-process
   UDS** (`<data_dir>/run/console-<pid>.sock`) — never TCP, never the shared default socket, so
   a console never collides with a running gateway/agent pair.
3. `AgentClient(connect_channel(uds))` → `GrpcAgentChatBackend` → `ChatService`.
4. Each user turn → `InternalChatRequest(model=…, messages=[…], session_key=…, stream=True)` →
   `ChatService.run(req, cancel)`; render `TokenDelta/ToolCall/ToolResult/Done/Error` events.
5. Full brain applies: builtin tools, subagent spawn*, memory, persona — identical wire
   contract to production.

### Mode B — attach (`--attach http://host:port`; opencode pattern)
OpenAI-compatible SSE client to a running gateway. `X-Session-Key` header carries the session.
Same renderer; the event source differs. Auth: none required on `/v1/chat/completions` today
(localhost deployment model); pass-through `--header` escape hatch for reverse-proxied deploys.

### Conversation state
The servicer/journal own durable history. The console keeps the **in-flight window** —
`list[Message]` accumulated this session (claude-code keeps client-side messages; we mirror
that) — and replays it per request (the `/v1/chat/completions` contract is stateless-window).
`/new` resets window + session_key; `--session`/`/resume` loads recent turns from the journal
(embedded mode) for context continuation.

## 2. Borrowed design, explicitly

**From claude-code (primary)**
- Turn loop shape: stream → render tool_use as it arrives → loop until terminal event;
  single mutable turn-state object; cancel via `asyncio.Event` (maps to AbortController).
- `-p/--print` non-interactive one-shot mode (pipe-friendly: prompt → final text on stdout).
- Small-fast-model routing (`getSmallFastModel()`): utility subtasks (session title generation,
  history summarization) and *auto-routed simple turns* go to a cheaper model.
- Slash command registry as data (name/description/handler), `/help` generated from it.
- Session transcript + resume; status line showing model + session + token usage.

**From hermes-agent**
- prompt_toolkit REPL with fixed bottom input; dual queue (idle input vs interrupt-while-busy);
  Ctrl+C interrupts the running turn (sets cancel event) instead of killing the process.
- Tool progress modes `off|new|all|verbose` (`/verbose` toggles); spinner + elapsed time;
  cute one-line tool descriptions; bell-on-complete.
- `/model` two-stage interactive picker fed by `ProviderRegistry` + `[models.aliases]`.
- Session commands: `/new /resume /sessions /title /usage /status`.

**From opencode (secondary)**
- Clean client/server split: the console is a *client* of the same chat contract the web UI and
  channels use — `--attach` makes that literal.
- Schema-first events: one internal event enum consumed by every renderer.
- Per-agent model/provider binding honored from agent cards.

## 3. Model routing (简单任务 → 便宜模型)

`config.toml`:
```toml
[console]
small_fast_model = "gpt-4o-mini"   # any registry-resolvable id/alias
auto_route = false                  # opt-in: classify simple turns → small model
```
- `router.classify(text) -> "simple" | "complex"` — deterministic heuristics first
  (length, code-fence/file-path/multi-step markers, question shape); no LLM call needed for v1.
- Utility tasks (title gen on `/title auto`, `/compact` summaries) ALWAYS use small_fast_model.
- `/model` overrides per session; `--model` per invocation; routing never overrides an explicit
  user choice (claude-code rule).
- Subagent-level routing already exists servicer-side (`spawn_inline` model param) — the console
  surfaces it (`/agents` lists cards + bound models).

## 4. Workflow / multi-agent surfacing

v1 = render what the brain already does: `subagent.spawn*` tool calls show as nested progress
lines (`◐ subagent researcher … 12s`), blackboard tool calls visible in verbose mode. The
orchestrator card (`agents/orchestrator.yaml`) is selectable via `--agent orchestrator`, giving
workflow-style fan-out through the existing supervisor.
v2 (out of scope here): client-side workflow DSL, status-card style multi-pane TUI.

## 5. Files

```
python/packages/corlinman-server/src/corlinman_server/console/
  __init__.py      exports run_console()
  events.py        ConsoleEvent normalization (internal events + SSE → one stream)
  brain.py         BrainSession (turn driver, cancel, window, session_key)
  embedded.py      EmbeddedBrain (UDS servicer boot + ChatService)
  attach.py        AttachBrain (SSE client)
  router.py        ModelRouter (small_fast_model, classify, resolve picker data)
  commands.py      SlashCommand registry + implementations
  render.py        Renderer (rich): stream, tool progress, status line, markdown final
  app.py           ConsoleApp REPL (prompt_toolkit) + --print one-shot path
python/packages/corlinman-server/src/corlinman_server/cli/console.py   click command
docs/PLAN_CLI_CONSOLE.md  (this file)
tests: corlinman-server/tests/console/ — commands, router, events mapping, attach SSE parse,
       brain window handling (mock backend); no network, no real provider.
```

Deps added to corlinman-server: `rich>=13`, `prompt-toolkit>=3.0`.

## 6. Waves

1. **W1 core**: console package + REPL + renderer + commands + embedded brain (UDS) +
   `--print`; registered in cli/main.py. ← this PR
2. **W2 attach**: SSE attach mode. ← this PR
3. **W3 routing**: router + `[console]` config + /model picker. ← this PR
4. **W4 polish (follow-ups)**: /resume journal hydration UI, interactive approval gate for
   dangerous tools (claude-code permission modes), TTS/voice (hermes), multi-pane status cards.

## 7. Risks / guardrails

- **UDS privacy**: per-pid socket under data_dir `run/`, 0700 dir; refuse TCP entirely in
  embedded mode (the in-proc servicer has no auth — same reasoning as
  `gateway/grpc/agent_server.py` `_SAFE_HOSTS`).
- **No double-brain**: embedded console must not touch `/tmp/corlinman-py.sock` (the production
  socket) — collision with a running gateway would cross-wire sessions.
- **Channel contract**: console builds a *pydantic* `InternalChatRequest` (not SimpleNamespace);
  keep `persona_id=None` explicit (see lesson_channel_request_contract_gap).
- **Windows**: UDS unavailable → embedded mode falls back to `127.0.0.1:0` loopback with the
  same guard, or DirectProviderBackend; documented limitation.
