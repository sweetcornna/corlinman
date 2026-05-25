# Changelog

All notable changes to corlinman are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased] — `/admin/system` + auto-update detection

> The gateway now knows when a new release ships and tells the operator.
> A 30s-polling `<UpdateBubble>` in the admin TopNav lights up amber when
> the latest GitHub release tag outranks `importlib.metadata.version`;
> clicking it lands on `/admin/system`, which renders sanitized release
> notes plus copy-paste upgrade commands (Native / Docker / Docker + QQ).
> No in-app one-click upgrade — the gateway can't sudo into the host —
> but the operator-driven flow is now first-class instead of "check the
> repo by hand." Plan at
> [`docs/PLAN_AUTO_UPDATE.md`](docs/PLAN_AUTO_UPDATE.md); operator doc
> at [`docs/system-updates.md`](docs/system-updates.md).

### Added

- **`<UpdateBubble>` in the admin TopNav** — quietly polls
  `/admin/system/info` every 30s; renders an amber chip with the new
  tag when one is available; dismissable per-tag via `localStorage`
  (the chip stays hidden for that tag, reappears on the next release).
- **`/admin/system` page** — three cards: current vs. latest version
  with deploy-mode hint (`docker` / `native`, sniffed from env),
  sanitized release-notes markdown (`react-markdown` + `rehype-
  sanitize`), and tabbed upgrade commands (Native / Docker / Docker +
  QQ) with copy buttons. Sidebar entry **System** under the settings
  group (icon: `MonitorCog`).
- **Three admin endpoints** —
  `GET /admin/system/info` (current `UpdateStatus` + deploy mode),
  `POST /admin/system/check-updates` (force-poll, server-side rate-
  limited to 1/min, returns fresh `UpdateStatus`),
  `GET /admin/system/upgrade-commands` (returns
  `{native, docker, docker_with_qq}` strings pre-filled with the
  target tag).
- **`UpdateChecker`** — polls
  `api.github.com/repos/ymylive/corlinman/releases/latest` with stored
  `If-None-Match` so a no-change poll costs zero against the GitHub
  rate-limit budget. 6h TTL, semver compare via
  `packaging.version.Version`, optional `CORLINMAN_GITHUB_TOKEN` for
  higher rate limits, prerelease channel opt-in.
- **`[system.update_check]` config stanza** in
  `docs/config.example.toml` — `enabled` / `interval_hours` /
  `include_prereleases` / `repo` / `github_token`, fully commented.
- **`system.update_check` scheduler builtin** — registered with the
  scheduler tool registry but pending a lifespan `scheduler.spawn()`
  wire-up; in the meantime `<UpdateBubble />`'s 30s poll and the on-
  page-load fetch on `/admin/system` keep detection live whenever an
  admin tab is open.
- **30 i18n keys** across `system.*` and `update.bubble.*` (`en` +
  `zh-CN`).
- **`docs/system-updates.md`** — operator-facing doc covering
  configuration, security model, GitHub rate-limit math, air-gapped
  deploys, and troubleshooting. `docs/quickstart.md` cross-links it
  from the "Watching the agent work" section.

### Changed

- **BREAKING: version unified to `1.1.1`** across the workspace
  `pyproject.toml`, `corlinman-server`'s own `pyproject.toml`, and
  `ui/package.json`. The git tag was already `v1.1.1`; this commit
  collapses the three previous version-of-truth sources so
  `importlib.metadata.version("corlinman-server")` matches the
  deployed tag — which the update checker depends on for the
  current-vs.-latest comparison to be meaningful.
- **`<ReleaseNotes>` renders GitHub release bodies through
  `rehype-sanitize`** — `<script>`, `javascript:` URLs, inline event
  handlers (`onclick=`, …), and `<iframe>`/`<object>`/`<embed>` are
  stripped; a unit test asserts a `<script>` payload in the release
  body doesn't reach the DOM.

---

## [Unreleased] — task observability overhaul

> Makes the agent's work visible. Today nobody can see what tools fired
> in a turn, what args went in, what came back, how long anything took,
> or what happened on a turn 10 minutes ago — even though the gateway
> collects most of that data. This release ports proven UX patterns
> from Claude Code, opencode, and hermes-agent into a single typed
> event stream that drives both the admin UI and the channel adapters.

### Added

- **Typed `EventEnvelope` event stream** — 14 events
  (`TurnStart` / `BlockStart` / `TextDelta` / `ReasoningDelta` /
  `ToolInputDelta` / `BlockStop` / `ToolStateRunning` /
  `ToolStateHeartbeat` / `ToolStateCompleted` / `SubagentSpawned` /
  `SubagentEvent` / `SubagentCompleted` / `Cancelling` /
  `TurnComplete` / `TurnErrored`) emitted by `ReasoningLoop` +
  `runner_pool` + `subagent.supervisor`. The legacy gRPC `ServerFrame`
  keeps emitting alongside so existing channel adapters and SDK
  consumers don't break.
- **`turn_events` SQLite table** (journal migration `004_turn_events`)
  — every emitted envelope is journaled (`turn_id` / `sequence` /
  `event_type` / `payload_json` / `timestamp_ms`). Replays from this
  table render identically to the live stream. TTL prune at boot +
  daily; configurable via `CORLINMAN_TURN_EVENTS_TTL_DAYS` (default 30
  days).
- **Three admin SSE/JSON routes** —
  `GET /admin/sessions/{key}/events/live` (SSE, 10s keepalive,
  `Last-Event-ID` resume + `?last_event_id=…` proxy fallback),
  `GET /admin/sessions/{key}/turns/{turn_id}/events` (paginated JSON
  replay), `GET /admin/sessions/{key}/cost` (aggregated cost / turn
  count / tool-call total).
- **`/admin/sessions/{key}` event timeline** — live SSE-driven turn
  cards. `ReasoningBlock` shimmer while streaming; `ToolWidget` with
  pending → running → completed/error state machine, live-ticking
  elapsed counter, expandable args + result through per-tool renderers
  (`bash` / `read_file` / `write_file` / `webfetch` / `grep` /
  fallback `generic`). rAF-batched merges so a fast-streaming turn
  doesn't tank rendering.
- **`/admin/sessions/{key}/turns/{turn_id}` drill-down** — same
  timeline component in replay mode, seeded from the JSON replay
  endpoint. Top-of-page `TurnSummaryCard` with elapsed / tool count /
  cost / finish reason.
- **Sticky cost footer** — five pills (total USD, turn count, avg
  turn time, tool calls, last-turn-N-ago); 15s polling + a
  `visibilitychange` refetch on tab focus. Session list grows three
  columns (total / avg / last tool used).
- **Sub-agent tree** — `BubbleEmitter` bubbles child envelopes into
  the parent stream; the UI renders the child's events nested inside
  the spawning tool widget, depth cap 3.
- **Tool heartbeat** — `ToolStateHeartbeat` fires every 10s while a
  tool runs so a `sleep 60` no longer leaves the UI quiet (configurable
  via `CORLINMAN_TOOL_HEARTBEAT_INTERVAL_MS`).
- **Channel post-turn footer + cancel/heartbeat consumer** — channel
  `_status.py` now subscribes to `EventEmitter` directly. Heartbeats
  refresh the spinner with `🔧 {tool} … {elapsed_s}s`; cancellation
  shows `⏹ 正在取消…` within ~1s instead of waiting for the next round;
  every reply gets a one-line footer `(elapsed: 12.4s · 3 tool calls ·
  ~$0.012)` (the `~` drops to `$` when `cost_status == "billed"`).
- **`ui/tests/e2e/task-observability.spec.ts`** — Playwright spec
  covers the live timeline (reasoning, two tool widgets, expand-to-
  see-args, cost footer pills) plus the drill-down replay.
- **Docs** — `docs/observability.md` now leads with the task event
  stream (taxonomy table, API endpoints with curl examples,
  configuration env vars); `docs/quickstart.md` gains a "Watching the
  agent work" section.

### Changed

- **`Cancelling` event is emitted the moment `ReasoningLoop.cancel()`
  is called** — previously the user had to wait for the next reasoning
  round to see anything change. Same emit point now feeds the UI
  badge + the channel spinner.

---

> Admin UI fixes — credentials, model picker, sessions navigation.
> Reconciles a split-brain state between `main` and the live
> deployment at `corlinman.cornna.xyz` (legacy endpoints existed on
> live but never landed in main; new observability endpoints exist in
> main but not yet on live), then ports the hermes `EnvPage` paste-
> only credentials pattern + two-column `ModelPickerDialog`. Plan at
> [`docs/PLAN_UI_FIXES.md`](docs/PLAN_UI_FIXES.md).

### Added

- **Provider test-connection endpoint** —
  `POST /admin/providers/{name}/test`. Zero-cost probe: hits
  `/v1/models` for openai-compatible kinds; returns
  `ok=true` + `note` for anthropic / google (no free probe surface).
  Latency capped at 5s; the api key is never echoed in the response or
  the access log. UI surfaces it as a per-row "Test connection" button
  with toast feedback.
- **Provider model discovery endpoint** —
  `GET /admin/providers/{name}/models`. Proxies upstream `/v1/models`
  for openai-compatible providers, returns a hardcoded list from
  `corlinman_providers.specs` for anthropic / google. 30s in-memory
  cache. Feeds the new `<ModelPickerDialog>`.
- **Provider kinds descriptor endpoint** —
  `GET /admin/providers/kinds`. Returns
  `{kinds: [{kind, label, description, params_schema}]}` so the custom-
  provider creation form can render itself from JSON-Schema instead of
  hard-coding the per-kind shape.
- **Session turns listing endpoint** —
  `GET /admin/sessions/{key}/turns?limit=50&before_id=...`. Paginated
  cursor over the `turns` SQLite table. Powers the past-turns pill row
  above the EventTimeline so the session detail page is reachable
  beyond deep links.
- **Credential reveal endpoint** —
  `GET /admin/credentials/{provider}/{key}/reveal`. Admin-only, auth-
  gated, return body redacted in access log. Backs the eye-icon UX on
  the credentials page.
- **Session replay endpoint backported** —
  `POST /admin/sessions/{session_key}/replay` with
  `{mode: "transcript" | "rerun", since_turn_id?}`. Lives on the live
  deployment today; brought back into main so a redeploy doesn't
  regress the existing `<ReplayDialog>` consumer.
- **`<ModelPickerDialog>`** — two-column provider / model picker with
  a single search filter (port of hermes-agent's
  `ModelPickerDialog.tsx`). Mounted on `/admin/models` (add-alias) and
  `/admin/agents/[name]` (per-agent model override).
- **`<EnvVarRow>` + `<ProviderGroupCard>`** — hermes-style credentials
  UI: paste-only secret input, eye-icon reveal with per-row client-
  side cache (toggle doesn't re-fetch), replace / clear buttons,
  prefix-grouped collapsible cards.
- **`<PastTurnsPills>`** — horizontal turn navigator above
  `EventTimeline` on `/admin/sessions/{key}`. ≤10 pills with
  `(turn_id, status, elapsed)`, "Load more" pagination.
- **`<TestConnectionButton>`** — per-provider one-click probe with
  toast feedback (latency on success, upstream error message on
  failure).
- **E2E smoke** — `ui/tests/e2e/admin-pages-smoke.spec.ts` visits
  seven admin surfaces (`sessions`, `logs`, `providers`, `credentials`,
  `models`, `agents`, session detail), fails on 404 XHRs and console
  errors. Catches "UI calls missing endpoint" regressions before
  deploy.

### Changed

- **BREAKING:** `GET /admin/providers/kinds` response shape changed
  from `{kinds: [string]}` to
  `{kinds: [{kind, label, description, params_schema}]}`.
  `<AddCustomProviderModal>` migrated; downstream consumers reading
  just the `kind` string need to map over the new array.
- **`/admin/providers/{name}/test` for anthropic / google** returns
  `ok=true` plus a `note` flag rather than a real round-trip. Those
  vendors don't expose a free models endpoint without a billed token;
  flagging the response keeps the UI honest about what it actually
  verified.
- **`<EnvVarRow>` eye-icon reveal** caches the fetched cleartext per
  row; subsequent toggles render from cache instead of re-hitting
  `/admin/credentials/{provider}/{key}/reveal`. Cache scope is the
  component instance; navigating away clears it.

### Fixed

- **`/admin/sessions/{key}` had no way in beyond deep links.** The
  detail page assumed you arrived with a `turn_id` in the URL.
  The past-turns pill row above the timeline now exposes every turn
  in the session, paginated.
- **Live deployment regressed when consuming a stale UI bundle** —
  the live UI calls SSE / cost / replay endpoints that the live
  backend either didn't ship yet (new) or shipped under a different
  path (legacy). Documented the deployment ordering in
  [`docs/observability.md`](docs/observability.md) §"Admin UI fixes
  (May 2026)".



> 4 commits on top of v1.1.0. Focuses on the per-turn hot path
> (~500-800 ms shaved off a 10-round task), adds hermes-agent-style
> auto-resume of in-progress turns at gateway boot, and tightens the
> live status streaming so a `todo_write` no longer hides the
> current tool being called.

### Added

- **Hermes-style auto-resume at boot** — when the gateway / agent
  process starts, `AgentResumeService` scans the journal for
  `in_progress` turns within a 10-minute window, sweeps anything
  older to `errored`, and either lets the channel's existing inbox
  drain re-deliver (QQ family) or seeds a fresh `pending` inbox row
  with `message_id="resume:<turn_id>"` for future channel drains
  (Telegram / Discord / Slack / Feishu). The chat handler's
  `find_resumable_turn` matcher then replays the journaled
  `(tool_call, tool_result)` pairs so the agent picks up where it
  left off. Boot log line: `agent.resume.scan_complete found=N
  resumed=M skipped=K window_minutes=10`.
- **`channel` column on `journal_turns`** — SQLite gets an
  idempotent `ALTER TABLE` at next open; Postgres gets
  `migrations/journal_postgres_v3.sql` (also inlined as a no-op
  `IF NOT EXISTS` so fresh deployments don't need a separate
  migration step).

### Changed

- **Telegram spinner keeps the op-flow line visible under the todo
  list.** Previously when the agent called `todo_write`, the
  placeholder switched to showing JUST the checkbox list and the
  user lost visibility of the current tool. Now the placeholder
  shows both, separated by a blank line:
  ```
  📋 任务清单 (1/4):
  ☑ Search market data
  ▣ Drafting decision memo
  ☐ Build chart

  🔧 web_search  'gpt-5.5 news'
  ```
- **QQ-family summary block drops the ☐ pending todo list.** QQ /
  QQ-official / WeChat-official can't edit messages, so a list of
  pending future work appearing in the reply preamble is visual
  noise. The block reverts to the legacy `📋 本次操作:` header with
  just the operation log (`✅ web_search …`, `📎 已发送文件 …`).
  The `format_todo_list` helper stays — Telegram + other edit-
  capable channels still use it.

### Performance

- **`_builtin_tool_schemas()` cached at module load** — the 13-tool
  schema list was rebuilt every round. Now resolved once into
  `_CACHED_BUILTIN_TOOL_SCHEMAS` and reused. Saves ~30-50 ms × N
  rounds (potentially ~500 ms on a 10-round task).
- **`ReasoningLoop._estimate_tokens` incremental cache** — was
  walking the entire message list every round (O(N) per call,
  effectively O(N²) over a long task). Now keeps a running
  character total + invalidates on compaction / list shrink /
  seed-message mutation. Saves ~5-15 ms × N rounds.
- **`AgentJournal.append_messages` batched transaction** — the
  `(assistant tool_call, tool_result)` pair was two separate
  `BEGIN IMMEDIATE` / `COMMIT` cycles per tool call. Now one
  transaction wraps both inserts. Saves ~5 ms × tools-per-round.
- **`SkillRegistry.refresh()` 30-second debounce** — was
  `rglob() + stat()`-ing every `.md` file on every turn. Now
  gated by a monotonic interval (env-overridable via
  `CORLINMAN_SKILL_REFRESH_INTERVAL_MS`, default 30 000). Saves
  ~5-10 ms / turn after the first turn.
- **Workspace snapshot drops the `rev-parse` subprocess** —
  `_snapshot.snapshot()` was forking three times (`git add` +
  `git commit` + `git rev-parse`). The third call is now replaced
  by a direct `.git/HEAD` parse (handles `ref:` indirection +
  loose refs + `packed-refs` fallback). Saves ~2-3 ms / turn.

### Fixed

- **gRPC client message-size limits asymmetric with server.** The
  agent server set `max_send_message_length = max_receive_message
  _length = 64 MB`, but the client at `corlinman_grpc.agent_client
  .connect_channel` left both at gRPC's 4 MB default. Large tool
  results (>4 MB shell output / file reads) silently failed with
  `RESOURCE_EXHAUSTED` despite the server happily sending them.
  Client now mirrors 64 MB on both sides.

## [1.1.0] — 2026-05-24 — channel parity + Claude-Code-style task UX

> 10 commits on top of v1.0.0. Brings the new chat channels to feature
> parity with Telegram (status streaming + file replies), adds two
> brand-new channels (QQ official bot + WeChat 公众号), fixes the
> session-management page (it was reading from the wrong store),
> simplifies the admin UI by ~16 pages, ports Claude Code's summary-
> based context compaction + mid-turn user-message injection, and
> renders the agent's task list as a live ☑/▣/☐ checkbox view.

### Added

- **QQ 官方机器人 channel** — Tencent 官方 bot platform (api.sgroup.qq.com).
  WebSocket gateway + REST sender + Ed25519 webhook sig + access-token
  single-flight refresh. Image attachments via `send_attachment`; non-
  images render an explanatory line (platform limitation).
- **微信公众号 channel** — webhook with sha1 signature verification +
  4.5 s passive-reply window with automatic fallback to customer-
  service messages over the 48 h reply window. Temp-media upload for
  image / voice replies. AES encryption is a documented v1 gap.
- **Discord / Slack / Feishu mutable-spinner status** — the three
  channels now render the same Telegram-style "🧠 思考中 → 🔧 调用工具
  → ✅ 完成 → ✍️ 生成回复 → final reply" mutable placeholder, with
  per-channel file uploads via `send_attachment` (Discord 25 MiB
  multipart, Slack `files.upload`, Feishu two-step `/im/v1/files`).
- **QQ tool-activity summary block** — QQ can't edit messages, so when
  a turn used ≥1 tool the agent's reply is now prepended with a
  compact `📋 本次操作: …` block listing every tool call + duration +
  outcome + file uploads. Env-gated via `CORLINMAN_QQ_TOOL_SUMMARY=0/1`.
- **Hermes-style detailed status** — Telegram spinner now shows arg
  previews (`🔧 web_search 'gpt-5.5 news'`), durations
  (`✅ web_search (302ms)`), errors (`❌ run_shell 失败 (42ms): perm…`),
  and reasoning deltas (`💭 推理: …` lines from Anthropic thinking
  blocks + DeepSeek-R1 reasoning_content). Mirrors hermes-agent's
  `_last_activity_desc` mutable spinner line.
- **`send_attachment` everywhere** — Discord, Slack, Feishu, QQ-official
  joined the existing Telegram + QQ-OneBot support. The agent calls
  `send_attachment(path=...)` and each channel picks the right transport.
- **Live task-list rendering** — `todo_write` tool calls now render as
  `📋 任务清单 (3/5): ☑ Search… ▣ Drafting… ☐ Build…`. Telegram
  spinners edit in place; QQ / QQ-official / WeChat prepend the final
  snapshot to the reply.
- **Claude-Code-style context compaction** — when token estimate ≥ 95 %
  of `CORLINMAN_CONTEXT_BUDGET` the reasoning loop now runs a
  summarization sub-call (same model, no tools, ≤1500 output tokens),
  replacing older messages with one synthetic system block:
  `PRIOR CONVERSATION SUMMARY: …`. Failure falls back to the existing
  elision path. The naive elision threshold dropped from 100 % to 60 %
  of budget so it fires earlier.
- **Mid-turn user-message injection** — while the agent is processing
  turn N for session-key X, a NEW message arriving for the same
  session is INJECTED into the running turn as additional user
  context (Claude Code's "supplemental message" UX). The second RPC
  returns `Done(finish_reason="supplemented")` and the channel
  silently keeps the typing indicator alive; no parallel turn is
  spawned. New `HookEvent.UserSupplemented` event fires for audit.
  `ReasoningLoop.inject_user_message(text)` is the public surface.
- **AgentJournal session APIs** — `list_session_summaries(*, limit)`
  + `delete_session(session_key)` on both the SQLite and Postgres
  backends. Aggregates chat history per session, returns
  `(session_key, first_seen, last_seen, turn_count, message_count,
  last_user_text, last_status)`. The Sessions admin page now reads
  this surface and operators can finally see + delete real chat
  history.
- **Sessions admin page rework** — Delete per row + Clear-all button
  + AlertDialog confirmations + last-seen column + empty-state copy.
  `DELETE /admin/sessions/{session_key}` and `DELETE /admin/sessions`
  routes on the backend with audit logs.
- **`useDevMode()` hook + Developer Settings page** — admin sidebar
  now shows 10 operator items by default with a toggle on
  `/admin/dev-settings` to surface the 11 developer-only pages (Config,
  Tenants, Credentials, Agents, Skills, Plugins, RAG, Profiles,
  Evolution, Hooks, Nodes). Preference persists in `localStorage`
  (`corlinman.devMode.v1`).
- **Per-channel concurrency cap** — every chat channel now caps
  in-flight turns at `CORLINMAN_<CHANNEL>_MAX_CONCURRENCY` (default 8),
  preventing a 100-message burst from spawning 100 parallel LLM
  streams.
- **gRPC keepalive aligned** — client + both server bind sites use the
  same `keepalive_time_ms=30s` + `max_ping_strikes=0` to stop the
  intermittent "UNAVAILABLE: Too many pings" on long agent turns.

### Changed

- **Sidebar trimmed** — removed 6 niche admin pages
  (`embedding`, `tagmemo`, `canvas`, `diary`, `characters`,
  `federation`) along with their backend routes. ~9 400 lines deleted.
  Provider-runtime embedding code is unaffected (just the deleted
  admin UI for it).
- **`JournalBackend.find_resumable_turn` / `begin_turn`** gained a
  `user_id` kwarg so group-chat members can't replay each other's
  tool side effects (default preserves legacy single-user behavior).
- **Sessions route data source** — `GET /admin/sessions` now reads
  from `agent_journal.sqlite` (the source of truth) instead of the
  unused legacy `sessions.sqlite` (which has been empty since 0.7.x).
  Legacy file is still consulted as a fallback if the journal is
  unavailable.

### Fixed

- **`/admin/sessions` returned empty** because it was reading the
  wrong store; see "Changed" above.
- **Long tasks loop until `_MAX_ROUNDS`** because the old elision-only
  compaction kept feeding the same `tool_calls` skeletons to the
  model. Summary-based compaction collapses redundant retries into a
  single sentence so the model has room to plan.
- **Discord / Slack / Feishu had no typing-indicator parity** — now
  fired (Discord `/typing`; Slack stub for missing-API; Feishu stub).

### Removed

- Admin UI pages: `embedding`, `tagmemo`, `canvas`, `diary`,
  `characters`, `federation`. Matching backend admin routes too.

## [1.0.0] — 2026-05-24 — Python port complete + production-ready edge

> Major release. Cuts the umbilical to the Rust gateway and finishes the
> Python port that started in the 0.6.x line. Adds Telegram + three more
> chat channels, real-time status streaming, file replies, multi-gateway
> HA via shared Postgres, a pluggable hook event bus, context-aware
> permissions, and hardens every I/O edge (SSRF + sandbox + reactive
> token refresh). 128 commits since `v0.6.8`.

### Added

- **Telegram channel** — long-poll bot adapter for private + group
  chats with keyword filter, `require_mention_in_groups`, allowed-
  chat allowlist, and graceful 429 back-off on the decorative
  endpoints.
- **Discord / Slack / Feishu channels** — text-only adapters with the
  same router + rate-limit + chat-service plumbing as QQ + Telegram.
- **Real-time status streaming** — Telegram clients see a live "is
  typing…" indicator + a placeholder that edits in place as the agent
  runs tools (`🧠 思考中... → 🔧 调用工具: write_file → 📎 已发送文件
  → ✍️ 生成回复中... → final reply`). QQ private chats get NapCat's
  `set_input_status` indicator. Mirrors hermes-agent's
  `_last_activity_desc` mutable spinner.
- **`send_attachment` builtin tool** — agent can reply with files
  (HTML / PDF / images / voice) instead of dumping raw text.
  Telegram picks document / photo / voice by MIME; QQ uses NapCat's
  `upload_private_file` / `upload_group_file` extensions.
- **Per-turn journal resume** — `AgentJournal.find_resumable_turn`
  matches a fresh Chat RPC against an in-progress turn (within ~5 min)
  and replays the journaled `(assistant tool_call, tool_result)` pairs
  so a gateway/agent restart picks up where it left off. Resume key
  scoped by `user_id` so group-chat members can't replay each other's
  tool side-effects.
- **`PostgresJournalBackend`** — multi-gateway HA via shared Postgres.
  Race-safe `INSERT ... ON CONFLICT DO NOTHING RETURNING turn_id`
  with a partial unique index on
  `(session_key, user_text, user_id)` WHERE `status='in_progress'`.
  SQLite remains default; switch via `CORLINMAN_JOURNAL_BACKEND=postgres`
  + `CORLINMAN_JOURNAL_POSTGRES_DSN`. Migrations at
  `migrations/journal_postgres_v{1,2}.sql`. asyncpg +
  pytest-postgresql are optional extras.
- **`HookBus` push subscribers** — register `(predicate, callable)` to
  receive `UserPromptSubmit` / `PreToolDispatch` / `ToolCalled` /
  `TurnComplete` / `TurnErrored` events. Sync + async, exception-
  isolated.
- **Context-aware `PermissionGate.decide_with_context(tool, model,
  session_key, user_id)`** with fnmatch rules
  (`{model: "claude-*", user_pattern: "guest*"}`). Legacy
  `decide(tool)` still works.
- **Dynamic skill reload** — `SkillRegistry.refresh()` runs per chat
  turn, picking up new / updated / removed `*.md` from
  `~/.corlinman/skills/` without a restart. Emits
  `agent.skills.refreshed added=... updated=... removed=...`.
- **Reactive 401 refresh** — OpenAI / OpenAI-compatible / Azure /
  Google / Bedrock / DeepSeek / GLM / Qwen all self-heal on env-var
  key rotation. Codex + Anthropic were already self-healing; Codex now
  single-flights via `asyncio.Lock` and serializes RMW of
  `~/.codex/auth.json` with `fcntl.flock`.
- **Durable QQ inbox (`inbox.sqlite`)** — every accepted QQ message
  recorded `pending → dispatched → done/dead`. Boot drainer flips
  stale `dispatched` rows back to `pending`.
- **NapCat heartbeat watcher** — detects bot-QQ kicked offline (>120 s
  silence) with a structured warning naming the ws endpoint.
- **Per-channel concurrency cap** — default 8, env-overridable via
  `CORLINMAN_{QQ,TELEGRAM,DISCORD,SLACK,FEISHU}_MAX_CONCURRENCY`.
- **`SIGTERM` close path** — gateway shutdown drains the Postgres
  pool, aiosqlite WAL, inbox, blackboard, and HookBus before exit.
- **Tier 2 coding tools** — per-turn file-state cache, fuzzy edit
  matcher with staleness guard, token-aware context compaction,
  workspace `git`-backed snapshot + `revert_changes` tool.

### Changed

- **BREAKING:** `JournalBackend.begin_turn(...)` return type is now
  `int | None`. SQLite always returns an int; Postgres may return
  `None` on conflict so the caller re-runs `find_resumable_turn`.
- **BREAKING:** `JournalBackend.begin_turn` + `find_resumable_turn`
  gained `user_id: str | None = None` (default preserves legacy).
- **BREAKING:** Removed the embedded new-api onboard/admin surface.
  `[providers.<name>]` blocks with `kind = "newapi"` migrate silently
  to `kind = "openai_compatible"` at load. The
  `corlinman-newapi-client` package, `/admin/newapi*` router,
  `/admin/onboard/newapi/{probe,channels}` endpoints, and
  `corlinman config migrate-sub2api` CLI helper are gone.
- gRPC keepalive aligned client ↔ both server bind sites
  (`keepalive_time_ms=30s` + `max_ping_strikes=0`) — fixes
  `UNAVAILABLE: Too many pings` on long agent turns.
- `_builtin:` sentinel namespace extracted to a shared
  `_BUILTIN_OBSERVATION_PREFIX` constant. In-process builtin tools
  now emit observation-only `ToolCall` frames so channel UIs can
  render the mutable spinner without double-feeding `tool_result`s.
- LRU cap (4096 entries, env-overridable
  `CORLINMAN_MAX_SESSION_CACHE`) on `_session_locks` and the cost
  meter's session map.

### Fixed

- Channels passed `dict` to `chat_service.run` causing
  `AttributeError: 'dict' object has no attribute 'model'` on every
  Telegram inbound. Switched to `SimpleNamespace`.
- Telegram typing pulse leak on placeholder send failure (pulse task
  now lives inside the `try/finally`).
- Telegram final `edit_message_text` / `send_message` unwrapped —
  failures now degrade with a warning log instead of stranding the
  placeholder on "✍️ 生成回复中...".
- Telegram `editMessageText` ignored HTTP 429 — now parses
  `parameters.retry_after` into a shared back-off deadline.
- OneBot writer dropped actions on transient WS send failure — now
  requeues to a front buffer and raises for reconnect.
- Telegram long-poll committed `offset` before `put` — could lose
  updates on cancel mid-batch. Now commits post-put.
- OneBot `_inbound_q` blocking put caused WS 1009 + reconnect storm
  under burst — switched to `put_nowait` + drop-oldest.
- Reasoning loop ignored `signal_input_closed` — half-closed bidi
  streams timed out at 30 s instead of terminating promptly.
- Out-of-order `tool_result` envelopes polluted next-round
  collection — now drained + dropped with
  `reasoning_loop.stale_tool_result`.
- aiosqlite BEGIN+ROLLBACK left the connection in an undefined tx
  state, silently no-op'ing subsequent writes. Switched to
  `async with conn:`.
- `send_attachment` size unguarded — added a 45 MiB pre-flight check.
- Built-in tool calls never visible to channels — Telegram status
  placeholder stuck on "🧠 思考中..." the whole turn. Observation-
  only `_builtin:` frames now flow through.
- Heartbeat watcher rendered `None` as the literal "Nones" — split
  into a distinct "received yet" branch naming the ws endpoint.
- Codex `_ensure_fresh` + `_attempt_token_recovery` raced on
  concurrent refresh — now share an `asyncio.Lock`.

### Security

- **`web_fetch` SSRF guard** — `is_safe_host` resolves the host via
  `socket.getaddrinfo` and rejects any IP that's private / loopback /
  link-local / multicast / reserved / metadata
  (`169.254.169.254` / `fd00:ec2::254`). Manual 5-redirect loop re-
  validates each hop. Dev-only override
  `CORLINMAN_WEB_FETCH_ALLOW_PRIVATE=1` (never opens the metadata
  endpoints).
- **`run_shell` sandbox** — POSIX `RLIMIT_CPU=60s`,
  `RLIMIT_FSIZE=100 MiB`, `RLIMIT_NPROC=64`, `RLIMIT_NOFILE=256`,
  `RLIMIT_AS=2 GiB` (Linux). `setsid()` + `os.killpg(SIGKILL)` so
  shell-spawned forks die with the parent. Minimal env whitelist
  (no provider keys / gRPC creds reach the subprocess). Hard
  timeout cap lowered from 120 s → 60 s.
- **Coding-tool symlink escape** — `resolve_in_workspace` walks each
  ancestor with `os.lstat`, refusing symlink components. Every write
  site opens with `O_NOFOLLOW`, catching the TOCTOU race at the
  syscall layer.
- `_codex_oauth.persist_codex_credential` now holds `fcntl.flock`
  around its read-modify-write window so the Codex CLI + gateway
  can't garble `auth.json`.

### Removed

- `corlinman-newapi-client` package and the `/admin/newapi*` surface.

## [0.7.1] — 2026-05-17 — warm pool

Adds the warm-pool surface that v0.7.0 deferred. Architectural note:
the Rust gateway talks gRPC to a long-running Python servicer, so the
literal OpenClaw "container per session" doesn't apply. Instead the
pool ships Python-side with a boot-time pre-warm hook so the upstream
provider SDK's auth handshake happens before the first user chat,
not on the user-facing hot path.

### Added

- **`corlinman_server.runner_pool.RunnerPool[T]`** — bounded warm
  pool with `max_warm_per_key` + `max_active_total` and oldest-idle
  eviction. Generic on the pooled type; ships with provider warming
  as the first caller, designed to grow to per-tenant / sandboxed
  resources in v0.8.
- **`CorlinmanAgentServicer.prewarm_providers(model_names)`** —
  resolve each model alias at boot, park the result warm. Failures
  log and skip (best-effort; the cold path stays intact).
- **`pool_stats()`** accessor for operator tooling.
- env: `CORLINMAN_RUNNER_POOL_WARM` (default 2),
  `CORLINMAN_RUNNER_POOL_MAX` (default 8).

### Added (v0.7.0 hygiene)

- 4 v0.7 smoke tests: end-to-end orchestrator `spawn_many` round-trip,
  `parent_tools` threading via the runner's allowlist-escalation
  reject, and pool prewarm contracts.

## [0.7.0] — 2026-05-17 — multi-agent

Headline: parallel sibling agents, a shared trace-scoped blackboard,
a deterministic Pareto scorer for prompt-template variants, and
BuildKit cache mounts that drop incremental Docker rebuilds from
~12 min to ~90 s. Inspired by Nous Research's
[hermes-agent](https://github.com/NousResearch/hermes-agent) (true
multi-agent + GEPA prompt evolution) and
[openclaw](https://github.com/openclaw/openclaw) (pre-warmed pool
pattern). Full notes:
[`docs/release-notes-v0.7.0.md`](docs/release-notes-v0.7.0.md).

### Added

- **`subagent.spawn_many`** tool. Dispatches up to 3 sibling children
  concurrently under one parent context via `asyncio.gather`. The
  supervisor's existing per-parent concurrency cap (default 3)
  still governs live siblings; fan-outs exceeding the cap reject
  up-front with a clean args-invalid envelope.
- **Shared blackboard** (`blackboard.read` / `blackboard.write`).
  Trace-scoped, append-only sqlite scratchpad for sibling agents to
  coordinate. Writes never overwrite; reads return the latest value at
  call time; trace isolation is the security boundary.
- **`agents/orchestrator.yaml`**: new planner persona that
  decomposes → dispatches → reduces.
- **GEPA-lite Pareto scorer** (`corlinman_evolution_engine.score_variants`).
  Deterministic, no LLM-judge, no DSPy dependency — token Jaccard
  against the episodes that already succeeded.
- **Builtin-tool interception** in the agent servicer routes the four
  new tools in-process rather than through the Rust plugin registry.
- **BuildKit cache mounts** on the rust-builder + py-builder stages
  for cargo registry / git / target and uv wheel cache.

### Deferred to v0.7.1

- Pre-warmed Python agent runner pool (OpenClaw-style). Designed in
  [`docs/multi-agent-release-plan.md`](docs/multi-agent-release-plan.md) §2.3.

## [Unreleased] — targets v0.5.0

Free-form named providers + 7 new market `kind`s, **plus a BREAKING swap
from `sub2api` to `newapi`** as the channel-pool sidecar. Full notes:
[`docs/release-notes-v0.5.0.md`](docs/release-notes-v0.5.0.md).

### Removed (BREAKING)

- **`ProviderKind::Sub2api` removed.** The `kind = "sub2api"` provider entry
  is no longer recognised. Replace with `kind = "newapi"` pointing at a
  [QuantumNous/new-api](https://github.com/QuantumNous/new-api) instance.
  Run `corlinman config migrate-sub2api --apply` to rewrite legacy entries
  automatically. See [`docs/migration/sub2api-to-newapi.md`](docs/migration/sub2api-to-newapi.md).

### Added

- **`ProviderKind::Newapi`** + new-api admin client crate
  (`corlinman-newapi-client`). MIT-licensed sidecar that pools channels
  (LLM / embedding / audio TTS) behind one OpenAI-wire endpoint. Replaces
  the LGPL-3.0 sub2api integration.
- **4-step interactive onboard wizard** (account → newapi connect →
  pick defaults → confirm). The gateway calls new-api's `/api/channel`
  to populate model dropdowns; the operator only types the URL + token
  once.
- **`/admin/newapi` connector page** with live channel health, usage
  quota, token TTL, and a 1-token round-trip test button.
- **`corlinman config migrate-sub2api [--dry-run|--apply]`** CLI
  subcommand that rewrites legacy `kind = "sub2api"` entries to
  `kind = "newapi"` in place (with backup).
- **Full i18n coverage (zh-CN + en)** for the new onboard wizard and
  admin newapi page.
- **Free-form `[providers.*]` configuration**: the providers section is
  now a `BTreeMap<String, ProviderEntry>` keyed by an operator-chosen
  name. Add OpenRouter, SiliconFlow, Ollama, vLLM, or any other
  OpenAI-wire-compatible vendor by writing two TOML lines — no Rust
  patch required. The six legacy slot names (`anthropic`, `openai`,
  `google`, `deepseek`, `qwen`, `glm`) continue to infer their `kind`
  for backwards compatibility.
- **Seven new `ProviderKind` variants**: `mistral`, `cohere`,
  `together`, `groq`, `replicate`, `bedrock`, `azure`. The first five
  route through the shared `OpenAICompatibleProvider` Python adapter
  with documented default base URLs; `bedrock` and `azure` are
  declared but raise `NotImplementedError` at build time pending real
  SigV4 / deployment-routing support.
- **Validator**: free-form names without an explicit `kind` produce a
  `missing_kind` error pointing at the offending entry, listing every
  valid kind in the message.

### Docs

- New: [`docs/providers.md`](docs/providers.md) — provider model + 14
  supported `kind`s + four end-to-end recipes (OpenRouter + OpenAI
  embedding, fully-local Ollama, CN-resident SiliconFlow, Groq
  alongside OpenAI).
- Updated: [`docs/config.example.toml`](docs/config.example.toml) leads
  with `[providers.openai]` plus six commented-out vendor recipes; adds
  named-provider `[embedding]` and full-form `[models.aliases.*]`
  examples.
- Updated: [`docs/architecture.md`](docs/architecture.md) §7 inline
  sample reflects the free-form shape; reading list links the new
  providers reference.
- Updated: [`README.md`](README.md) Configuration section shows the
  new `kind = "..."` shape; documentation map links the new doc.

### Migration notes

- No data migration. Existing configs with first-party slot names
  parse unchanged.
- New entries MUST set `kind` explicitly; `corlinman config validate`
  surfaces any missing `kind` field with a one-line fix hint.
- `bedrock` and `azure` parse and validate but raise at adapter-build
  time today — declare `kind = "openai_compatible"` against a
  compatible proxy until the real adapters ship.

## [0.4.0] — 2026-04-23

Admin UI redesign: **Tidepool** design system. Warm-amber glass
aesthetic, day+night themes, and a reusable primitive library power a
from-scratch re-skin of all 15 admin pages. Backend and API unchanged —
this is a pure frontend release.

### Added

- **Design tokens** (`ui/app/globals.css`): `--tp-*` namespace for
  amber / ember / peach accents, ink ramp, glass layers, edge colours,
  gradients, shadows, and row alternation. Day and night palettes share
  every variable name; `data-theme="light|dark"` (mirrored to the
  `.dark` class for Tailwind compatibility) selects the active set.
- **12 new UI primitives** (`ui/components/ui/`):
  `<GlassPanel>` (soft/strong/subtle/primary variants respecting the
  ≤5 blur-layer/viewport budget), `<AuroraBackground>`,
  `<ThemeToggle>` (sun/moon pill with no-FOUC boot script),
  `<MiniSparkline>`, `<StreamPill>`, `<FilterChipGroup>`,
  `<StatChip>` (tick-up animation + ambient sparkline),
  `<JsonView>` (syntax-highlighted), `<LogRow>`, `<DetailDrawer>`,
  `<CommandPalette>` (configurable via `PaletteGroup[]`), plus
  `<UptimeStreak>`.
- **Motion tokens** (`ui/lib/motion.ts`): `tickUp` and `paletteIn`
  framer-motion variants alongside existing `fadeUp` / `stagger` /
  `springPop`. Continuous ambient animations (breathing, draw-in,
  just-now fades, badge pulses) live as CSS keyframes under `.tp-*`
  utility classes — cheaper than per-frame React work.
- **Typography**: Instrument Serif (display) loaded via `next/font`
  as `var(--font-instrument-serif)`, paired with existing Geist sans
  and Geist mono.
- **Theme persistence**: shared `corlinman-theme` storage key between
  `next-themes` and the inline boot script in `app/layout.tsx`.
  Hydration is race-free because the boot script writes
  `data-theme` + `.dark` before React mounts.
- **UI docs**: new "Tidepool design system" section in `ui/README.md`
  documenting tokens, primitive APIs, motion patterns, performance
  budget, and a new-page quick-start.

### Changed

- **All 15 admin pages retokened** onto Tidepool: Dashboard, Logs,
  Plugins, Approvals, Skills, Characters, Hooks, Scheduler, Nodes,
  Playground, Canvas, Tag Memo, Diary, Channels (QQ + Telegram),
  Config, Login, Models, Providers, Embedding, RAG, Agents. Direct
  colour/background classes replaced with `tp-*` tokens, `<Card>`
  uses swapped for `<GlassPanel>` where the glass treatment applies.
- **Admin layout** (`app/(admin)/layout.tsx`): `<AuroraBackground>`
  mounted once behind the sidebar + main grid; container spacing
  normalised to `gap-4 p-4`.
- **Command palette** (`components/cmdk-palette.tsx`): inner
  rendering delegated to the new `<CommandPalette>` primitive via a
  declarative `PaletteGroup[]` config. `useCommandPalette` hook,
  `CommandPaletteProvider`, `NAV_CMDS` registry, recent-routes, and
  test-chat drawer preserved.
- **i18n**: pages that gained Tidepool prose (hero copy, empty
  states, filter chips) now partition their new keys under a
  `<page>.tp.*` sub-namespace to keep diffs legible.

### Fixed

- **WCAG AA contrast**: darkened day-mode `--primary` to amber-800
  (`hsl(20 82% 33%)`) after `<Button>` primary text failed 4.5:1
  against foreground on the warm base. Night mode uses amber-400
  (`hsl(35 90% 65%)`) on dark ink.
- **Aurora visibility**: removed `bg-background` from `<body>` in
  `app/layout.tsx`; the admin layout now owns the backdrop, while
  the login route re-adds `bg-background` on its own root.
- **Offline-state HTML dumps**: plugins and scheduler pages detected
  backend HTML error responses (rather than JSON) and rendered the
  raw markup; `OfflineBlock` now suppresses dumps whose first line
  starts with `<`.
- **Telegram page `<dl>` a11y**: nested `<FilterStatCell>` broke
  definition-list semantics. Converted the wrapper to
  `<div>/<div>/<div>` so axe passes.

### Performance

- Dashboard blur-layer count dropped from 7 → 4 per viewport by
  defaulting non-primary `<StatChip>` instances to `<GlassPanel
  variant="subtle">` (tp-glass-inner, no `backdrop-filter`). Primary
  chip retains the full glass treatment to anchor the eye.
- All continuous animations (breathing dots, draw-in underlines,
  badge pulses, just-now fades) run as CSS keyframes gated by
  `@media (prefers-reduced-motion: reduce)`.

### Migration notes

- No backend changes. Existing deployments can upgrade by pulling the
  new `ui-static/` bundle only.
- Custom pages that used raw `bg-card` / `text-muted-foreground`
  continue to render — Tidepool tokens compose alongside legacy
  shadcn tokens rather than replacing them.
- Users with persisted theme preferences from the previous
  `next-themes` default key will see a one-time flip to dark on
  first visit; the new `corlinman-theme` key is then used
  consistently.

[0.4.0]: https://github.com/ymylive/corlinman/releases/tag/v0.4.0

## [0.3.0] — 2026-04-23

Sprint 9 (Batch 1–4) rollup: hierarchical tags + EPA cache in the
vector store, manifest v2, reserved placeholder namespaces, and
dual-track tool-call protocol. All additions are backwards-compatible.
Upgrade guide: [`docs/migration/v1-to-v2.md`](docs/migration/v1-to-v2.md).

### Added

- **Manifest v2** (`corlinman-plugins`): new `manifest_version`,
  `protocols`, `hooks`, `skill_refs` fields. Absent `manifest_version`
  is treated as v1 and auto-migrates to v2 in memory with default
  protocols `["openai_function"]`. Unknown `protocols` values are
  rejected at load; unknown `hooks` names warn but don't fail.
- **Vector schema v6** (`corlinman-vector`): new `tag_nodes`
  (hierarchical tag tree: `id / parent_id / name / path / depth`) and
  `chunk_epa` (per-chunk EPA projection cache). `chunk_tags` retargets
  its FK to `tag_nodes.id`; flat v5 tags materialise as depth-0 nodes
  so legacy queries keep working. Migration is idempotent and runs
  in-transaction on first open.
- **Config sections**: `[hooks]`, `[skills]`, `[variables]`,
  `[agents]`, `[tools.block]`, `[telegram.webhook]`, `[vector.tags]`,
  `[wstool]`, `[canvas]`, `[nodebridge]`. All `#[serde(default)]` —
  existing `config.toml` loads unchanged.
- **Placeholder namespaces**: reserved `var / sar / tar / agent /
  session / tool / vector / skill`. Cycle detection, async resolution,
  `{{角色}}` agent-card expansion with single-agent-gate semantics.
- **On-disk authoring surfaces**: `skills/*.md` (openclaw-style YAML
  frontmatter + Markdown), `agents/*.yaml` (character cards),
  `TVStxt/{tar,var,sar,fixed}/*.txt` (four-tier cascade variables).
  Sample files ship in-repo.
- **New Rust crates**: `corlinman-hooks` (in-process hook bus),
  `corlinman-skills` (openclaw skill loader + system-prompt injector),
  `corlinman-wstool` (local WebSocket tool bus), `corlinman-nodebridge`
  (Node.js worker bridge listener).
- **New Python package**: `corlinman-tagmemo` (EPA basis fitting +
  pyramid build; feeds `chunk_epa` cache).
- **Admin UI pages**: `/skills`, `/characters`, `/hooks`,
  `/playground/protocol`, `/channels/telegram`, `/nodes`, plus
  tagmemo / diary / canvas surfaces.
- **Dual-track tool invocation**: agents may emit tool calls as
  `<<<[TOOL_REQUEST]>>>` structured blocks (with `「始」…「末」`
  value fencing) in addition to OpenAI function-call JSON. Opt in per
  agent via manifest `protocols = ["block"]` + `[tools.block].enabled
  = true`. Legacy plugins remain reachable via
  `fallback_to_function_call = true`.

### Migration notes

- Legacy v1 plugin manifests parse unchanged.
- v5 vector DBs migrate forward on first open; there is no shipped
  down-path — rollback is "restore the pre-upgrade data-dir backup".
- Existing `config.toml` needs no edits.

[0.3.0]: https://github.com/ymylive/corlinman/releases/tag/v0.3.0

## [0.2.0] — 2026-04-21

Major release. Dynamic provider registry, per-alias model params,
first-class embedding config, and admin UI to manage all of it.
Full notes: [`docs/release-notes-v0.2.0.md`](docs/release-notes-v0.2.0.md).

### Added

- **Config**: `[providers.<name>].kind` enum + `params` map;
  `[models.aliases.<name>].params`; new `[embedding]` section.
  Backward-compatible — configs without `kind` on first-party
  providers still parse via inferred-kind defaults.
- **Rust admin routes**: `/admin/providers` (CRUD + 409 reference
  guard); `/admin/embedding` (GET/POST, benchmark stubbed to 501);
  `/admin/models/aliases` extended with single-row upsert + delete.
- **Python**: dynamic `ProviderRegistry` driven by `[providers.*]`
  specs; `params_schema()` on every provider; new
  `CorlinmanEmbeddingProvider` ABC with OpenAI-compatible + Google
  implementations; `benchmark_embedding()` helper (p50/p99 latency +
  cosine matrix).
- **UI**: `/providers` + `/embedding` pages, `/models` inline-accordion
  for params, hand-rolled `<DynamicParamsForm>` JSON-Schema renderer,
  ~145 new i18n keys across zh-CN + en.

### Fixed

- `/admin/approvals` returned 503 in production because `ApprovalGate`
  was never constructed at boot. `build_runtime_with_logs` now wires
  it from the live config handle + the RAG SQLite.

### Changed

- Docker image drops the `ui-builder` stage. Production serves the
  Next.js static export via nginx from `/opt/corlinman/ui-static/`;
  bundling it was dead weight and segfaulted node under Rosetta 2
  cross-builds.

### Known issues

- `/admin/embedding/benchmark` is a 501 stub until the Python helper
  is reachable over gRPC from Rust. UI handles the fallback.
- Rust gateway doesn't yet export `CORLINMAN_PY_CONFIG` to the Python
  subprocess; the legacy prefix-matching path keeps chats working
  while the config-driven registry integration lands.

[0.2.0]: https://github.com/ymylive/corlinman/releases/tag/v0.2.0

## [0.1.3] — 2026-04-21

zh-CN / en internationalisation + static-bundle API fix. Pure frontend
release — no Rust, Python, or Dockerfile changes.

### Added

- Full zh-CN / en i18n across every admin page, layout, login, dashboard,
  and `⌘K` palette. `react-i18next` + two TypeScript locale bundles
  (378 keys each, compile-time parity enforced).
- Language toggle in the topnav + command-palette action. Choice persists
  in `localStorage`; first-visit detection falls back to
  `navigator.language` (`zh*` → Chinese, else English).
- Inline pre-hydration boot script sets `<html lang>` so language
  selection applies before React mounts (no FOUC).

### Fixed

- **`GATEWAY_BASE_URL` default**: changed from `"http://localhost:6005"`
  to `""`. The static export used to bake localhost into the visitor's
  bundle, making every `/admin`, `/health`, `/v1` call from a deployed
  origin fail with `ERR_CONNECTION_REFUSED`. Relative URLs now resolve
  through the current origin, which nginx already reverse-proxies to
  the gateway. `NEXT_PUBLIC_GATEWAY_URL` remains the local-dev
  override; mock-server paths untouched.

### Dependencies

- Added: `i18next`, `react-i18next`, `i18next-browser-languagedetector`.

[0.1.3]: https://github.com/ymylive/corlinman/releases/tag/v0.1.3

## [0.1.2] — 2026-04-21

Admin UI redesign. Pure frontend release — no Rust, Python, or
Dockerfile changes.

### Changed

- **Admin UI fully redesigned in a Linear / Vercel aesthetic**: dark-first
  with a single indigo accent, Geist Sans / Mono typography, borders-over-shadows,
  compact 6–8 px radii. `next-themes` light/dark toggle preserved.
- **New dashboard landing page** (`/`): four stat cards with inline
  sparklines, SSE-driven recent-activity feed, and a 7-check system health
  panel backed by `/health`.
- **Sidebar + topnav**: 240 ↔ 56 px collapsible sidebar with an animated
  active-indicator (framer-motion `layoutId`); topnav adds auto
  breadcrumb, live health dot, theme toggle, and a `⌘K` search pill.
- **Global command palette** (`cmdk`): fuzzy navigation over all
  destinations, a test-chat drawer that POSTs to `/v1/chat/completions`,
  plus theme-toggle and logout actions. Recent commands persist in
  `localStorage`.
- **Motion language**: 200 ms page-transition fades, skeleton shimmers,
  `sonner` toasts, slide-up issues drawer on the config page. No bouncy
  spring animations.
- **Refined pages**: Plugins, Agents, RAG, Channels, Scheduler, Approvals,
  Models, Config, Logs — consistent status dots, inline-edit affordances,
  virtualised logs list with pause-stream toggle, live scheduler countdowns.
- **New login page**: two-column layout with a constellation backdrop
  SVG and inline error with shake micro-animation.

### Added

- `framer-motion`, `cmdk`, `geist`, `sonner` as UI dependencies.
- `fetchHealth()` + `HealthStatus` type in `ui/lib/api.ts`.

### Stability

- Playwright E2E selectors audited and preserved.
- Vitest suite (including Chinese login-form labels) still green.
- No API contracts changed.

[0.1.2]: https://github.com/ymylive/corlinman/releases/tag/v0.1.2

## [0.1.1] — 2026-04-21

Deployment hotfix. Surfaced the first time the 1.0 image was built
against a real server. All changes are docker / runtime fixes — no
code behaviour changes outside the boot path.

### Fixed

- **`docker/Dockerfile`**: drop stale `pnpm -C ui export` step —
  Next.js 14 removed the `next export` command; `output: "export"` in
  `ui/next.config.ts` already emits the static bundle during
  `next build`.
- **`docker/Dockerfile`**: bump rust base from `1.85-slim` to
  `1.95-slim` to match the project's `rust-toolchain.toml`.
  `cargo-chef 0.1.77` transitively raised its MSRV to `rustc 1.88`.
- **`docker/Dockerfile`**: add `binutils` + `g++` to the rust-builder
  apt layer (required by `link-cplusplus`) and force the BFD linker via
  `RUSTFLAGS=-C link-arg=-fuse-ld=bfd`. `lld` SIGSEGVs under Rosetta 2
  / QEMU user-mode emulation when cross-building amd64 images from
  Apple Silicon hosts.
- **`docker/Dockerfile`**: correct runtime `COPY` of the CLI binary —
  cargo emits `/build/target/release/corlinman` (per `[[bin]] name`),
  not `corlinman-cli`.
- **`rust/crates/corlinman-gateway/src/main.rs`**: honour `BIND` env
  var (default `127.0.0.1`, containerised deploys set `0.0.0.0`).
  Previously the listener was hard-bound to `127.0.0.1` and docker
  port-publishing never reached it.
- **`docker/Dockerfile`**: carry the python source tree into the
  runtime image. `uv sync --no-editable` ignores workspace members, so
  venv `.pth` shims pointed at `/build/python/packages/*/src/` which
  don't exist in runtime — `corlinman-python-server` died at
  `ModuleNotFoundError`. Adding `COPY --from=py-builder /build/python
  /build/python` resolves the editable paths.

### Added

- **Runtime env knobs**: `BIND` (listener address) and `OPENAI_BASE_URL`
  (consumed by `AsyncOpenAI` when `[providers.openai].base_url` isn't
  threaded through — see Known Issues).

### Known issues carried over

- `corlinman_providers.registry.resolve()` still ignores `[providers.*]`
  settings from `config.toml`. Until a deeper fix lands, point non-default
  OpenAI-compatible backends at the right host via `OPENAI_BASE_URL`.
- Docker image does not supervise the python agent out of the box;
  production deploys use a startup script (`docker/start.sh` pattern)
  that spawns `corlinman-python-server` alongside `corlinman-gateway`.

[0.1.1]: https://github.com/ymylive/corlinman/releases/tag/v0.1.1

## [0.1.0] — 2026-04-21

First tagged release. The 1.0 release prep sprint (S8) wraps seven prior
implementation sprints (M0–M7) into a shippable self-hosted intelligent
agent platform.

### Added

- **Core gateway** (`rust/crates/corlinman-gateway`): OpenAI-compatible
  `/v1/chat/completions` (stream + non-stream), `/v1/embeddings`,
  `/v1/models`, WebSocket admin endpoints, and the full admin REST surface
  (`/admin/plugins`, `/admin/rag/*`, `/admin/approvals`, `/admin/scheduler/*`,
  `/admin/config`, `/admin/logs/stream`, `/admin/health/metrics`). Session
  history persisted to `~/.corlinman/sessions.sqlite` with a configurable
  trim cap.
- **Python agent plane** (`python/packages/corlinman-server`,
  `corlinman-agent`, `corlinman-providers`): gRPC `Agent.Chat` reasoning
  loop with streaming token deltas, tool-call loop, and providers for
  Anthropic, OpenAI, Google, DeepSeek, Qwen, and GLM.
- **Plugin runtime** (`rust/crates/corlinman-plugins`): three plugin
  types (sync / async / service) over JSON-RPC 2.0 stdio or gRPC.
  Includes manifest parser, `plugin-manifest.toml` validation, async
  task callback registry (`/plugin-callback/:task_id`), approval gate
  for human-in-the-loop tool execution, hot reload of the plugin
  registry, and a Docker sandbox runner for untrusted plugins.
- **RAG** (`rust/crates/corlinman-vector`): SQLite + FTS5 BM25,
  usearch HNSW dense recall, reciprocal-rank fusion, optional
  gRPC-backed cross-encoder rerank, tag-filter pushdown, LRU unload,
  and multi-step schema migrations (v1 → v4).
- **Channels** (`rust/crates/corlinman-channels`): QQ (go-cqhttp /
  OneBot v11) and Telegram adapters with rate limiting, multimodal
  uploads, user-to-session binding.
- **Observability** (M7): W3C `traceparent` propagation, OpenTelemetry
  OTLP exporter, three-tier Prometheus metrics (gateway / plugin /
  provider), `/health` probes driven by real component state, `corlinman
  doctor` with 20+ diagnostic checks (config / agent gRPC ping / SQLite
  / usearch / plugin registry / docker / disk / memory / log rotation /
  provider HTTPS smoke / manifest duplicates / broken symlinks /
  pending-approvals overflow / python subprocess health / …).
- **Admin UI** (`ui/`): Next.js 15 + React 19 dashboard for plugins,
  RAG, approvals, scheduler, config, logs, and health metrics.
  Playwright e2e coverage.
- **CLI** (`rust/crates/corlinman-cli`): `corlinman onboard`,
  `corlinman doctor`, `corlinman plugins`, `corlinman config`,
  `corlinman dev`, `corlinman vector`, and — new in this release —
  `corlinman qa run` + `corlinman qa bench`.

### Docs

- `docs/roadmap.md` — canonical sprint plan (through M8 and beyond).
- `docs/architecture.md`, `docs/plugin-authoring.md`, `docs/runbook.md`.
- `docs/perf-baseline-1.0.md` — p50 / p99 numbers for chat, RAG, and
  plugin exec roundtrips. Used by CI to detect ≥20 % regressions.
- `qa/scenarios/*.yaml` — 8 executable scenarios covering chat
  stream + non-stream, tool-call loop, plugin sync + async, RAG hybrid
  retrieval, OneBot echo, and a marked-live fresh-install walkthrough.

### Known gaps (deferred to 0.1.1)

- **No prebuilt docker image yet.** Build from source with `cargo build
  --release -p corlinman-gateway -p corlinman-cli`; the `ghcr.io/ymylive/corlinman:0.1.0`
  image is pending a v0.1.1 follow-up once a build host with docker is
  available.
- **Screenshot placeholder**: `README.md` references
  `docs/assets/dashboard.png`; the actual PNG will be added with the
  installation walkthrough screencast.
- **`fresh-install` QA scenario** is marked `requires_live: true` — it's
  exercised by the S8 T4 screencast rather than the offline CI runner.
- **1.0 release comms** (blog / Zhihu / Hacker News / r/selfhosted /
  r/LocalLLaMA) are a separate content-production task, not part of
  this release artefact.

### Reference

Commit history on the `main` branch:

- `sprint-1` through `sprint-3`: M1 / M2 / M3 / M4 scope
- `sprint-4` (M5 channels), `sprint-5` (M6 auth + logs + approvals),
  `sprint-6` (M6 admin UI + Playwright)
- `sprint-7` (M7 observability)
- `sprint-8` (this release — M8 1.0 prep)

[0.1.0]: https://github.com/ymylive/corlinman/releases/tag/v0.1.0
