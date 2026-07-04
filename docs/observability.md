# Observability

corlinman exposes observability at two layers:

- **Task observability** — what just happened in a turn? Which tools
  fired with what args, how long they took, what reasoning the model
  produced, how much it cost. Backed by a typed event stream surfaced
  over SSE + a SQLite journal. **See [Task event stream](#task-event-stream).**
- **Platform observability** — tracing spans + Prometheus metrics for
  the gateway process itself (RPS, latency histograms, plugin invokes,
  channel ingest counters). See [Platform tracing & metrics](#platform-tracing--metrics).

This document catalogues both. Operators chasing "why was that one turn
slow / wrong / expensive" want the first half; SREs wiring Grafana
dashboards want the second.

---

## Task event stream

### Overview

Every chat turn the agent executes is rendered to a chronologically-
ordered stream of typed `EventEnvelope` events. The same envelopes are
journaled to the `turn_events` SQLite table on emission, so the UI can
either consume the live SSE feed (`/admin/sessions/{key}/events/live`)
or replay a historical turn from the journal
(`/admin/sessions/{key}/turns/{turn_id}/events`) — both render through
the identical timeline components.

This replaces the previous "fire-once `ToolCallEvent` + lost reasoning"
model and brings parity with Claude Code's `content_block_*` stream and
opencode's `Part` discriminated union. The legacy gRPC `ServerFrame`
keeps emitting (`token` / `tool_call` / `done` / `error`) so existing
channel adapters and SDK consumers don't break — the new stream runs
alongside it. Channels that want the richer signals (heartbeat,
cancellation) can subscribe to `EventEmitter` directly; see
[Channel adapters](#channel-adapters).

```
                       ┌───────────────────────┐
   ReasoningLoop ──────► EventEmitter (in-mem) ├──► SSE clients (admin UI)
        │              └────────────┬──────────┘
        │                           │ tee
        │                           ▼
        │                  ┌────────────────┐
        └─ legacy ServerFrame ► turn_events (sqlite, journaled)
                                   │
                                   └──► JSON replay route
```

### Event taxonomy

14 typed events. `turn_id` + `sequence` (monotonic per turn) is the
correlation key; `timestamp_ms` is wall-clock.

| Event | Trigger | Payload | Emit from | Consume in |
|---|---|---|---|---|
| `TurnStart` | Start of `_run_one_round` | `model`, `user_text`, `system_message_preview` | `corlinman_agent.reasoning_loop` | UI summary card, channel preamble |
| `BlockStart` | Provider emits `content_block_start` | `index`, `block_type` (`text` / `reasoning` / `tool_use`), `tool_name?`, `tool_call_id?` | `reasoning_loop` | UI timeline (creates a new part) |
| `TextDelta` | Each model text token | `index`, `text`, `cumulative_len?` | `reasoning_loop` | UI text part, throttled via rAF |
| `ReasoningDelta` | Each thinking-mode token | `index`, `text`, `signature?` | `reasoning_loop` | UI ReasoningBlock shimmer |
| `ToolInputDelta` | Provider streams partial JSON args (Anthropic) | `index`, `partial_json` | `reasoning_loop` | UI tool widget args accumulator |
| `BlockStop` | Provider emits `content_block_stop` | `index`, `elapsed_ms` | `reasoning_loop` | UI flips part to settled state |
| `ToolStateRunning` | Just before plugin dispatch | `tool_call_id`, `tool_name`, `args_json`, `started_at_ms` | `corlinman_server.runner_pool` | UI tool widget → "running"; channel spinner `🔧 {name} {args}` |
| `ToolStateHeartbeat` | Every 10s while a tool runs | `tool_call_id`, `elapsed_ms`, `stdout_tail?` | `runner_pool` heartbeat task | UI ticks elapsed; channel `🔧 {name} … 23s` |
| `ToolStateCompleted` | Post-dispatch (success or error) | `tool_call_id`, `result_summary` (≤4kB), `result_json_ref?`, `elapsed_ms`, `is_error` | `runner_pool` | UI tool widget → "completed"/"error"; channel `✅/❌ {name} ({duration})` |
| `SubagentSpawned` | Parent spawns a child session | `parent_session`, `child_session`, `child_agent_id`, `depth`, `prompt_preview` | `corlinman_subagent.supervisor` | UI sub-agent tree (depth cap 3) |
| `SubagentEvent` | Bubble child envelope into parent stream | `child_session`, `envelope: EventEnvelope` | `supervisor` BubbleEmitter | UI nested timeline beneath spawning tool widget |
| `SubagentCompleted` | Child finishes | `child_session`, `finish_reason`, `tool_calls_made`, `elapsed_ms`, `summary` | `supervisor` | UI collapses tree, channel summary |
| `Cancelling` | The moment `ReasoningLoop.cancel()` is called | `reason` | `corlinman_agent.cancel` | UI badge `⏹ cancelling`; channel `⏹ 正在取消…` |
| `TurnComplete` | End of a clean turn | `finish_reason`, `usage`, `elapsed_ms`, `estimated_cost_usd?`, `cost_status?` | `reasoning_loop` | UI status pill + cost footer refresh; channel post-turn footer |
| `TurnErrored` | Error path | `reason`, `message`, `elapsed_ms` | `reasoning_loop` error handler | UI red banner, channel `❌` line |

### API endpoints

All three routes are admin-scoped — `Cookie: session=...` from `/admin/login` is required.

#### `GET /admin/sessions/{key}/events/live` — live SSE

Subscribe to the in-process `EventEmitter` for `key`. Catches up from
the journal (`turn_events` rows ≥ `last_event_id` if supplied), then
streams new events. 10s server-side keepalive ticks prevent idle
proxies from closing the socket. Reconnect uses standard SSE
`Last-Event-ID: <turn_id>:<sequence>` semantics — both the header and a
`?last_event_id=...` query-string fallback are accepted (some proxies
strip headers on long-lived streams).

```bash
curl -N --cookie "session=$COOKIE" \
  "http://localhost:6005/admin/sessions/telegram:42/events/live"

# →
id: 0123abcd:1
data: {"turn_id":"0123abcd","sequence":1,"timestamp_ms":1700000000000,"event_type":"TurnStart","payload":{"model":"…","user_text":"…"}}

id: 0123abcd:2
data: {"turn_id":"0123abcd","sequence":2,"timestamp_ms":1700000000050,"event_type":"BlockStart","payload":{"index":0,"block_type":"reasoning"}}

: keepalive
```

#### `GET /admin/sessions/{key}/turns/{turn_id}/events` — JSON replay

Paginated dump of every event for a single turn, ordered by sequence.
`?after_sequence=N` + `?limit=M` cursor; `next_cursor: null` signals
exhaustion.

```bash
curl --cookie "session=$COOKIE" \
  "http://localhost:6005/admin/sessions/telegram:42/turns/0123abcd/events?limit=5000"

# → {
#   "events": [ {turn_id, sequence, timestamp_ms, event_type, payload}, ... ],
#   "next_cursor": null
# }
```

#### `GET /admin/sessions/{key}/cost` — aggregated session cost

Aggregates `turn_events` for the session. Feeds the sticky cost footer
on `/admin/sessions/{key}`; polled every 15s by the UI plus a refetch
on `visibilitychange`.

```bash
curl --cookie "session=$COOKIE" \
  "http://localhost:6005/admin/sessions/telegram:42/cost"

# → {
#   "session_key": "telegram:42",
#   "turn_count": 14,
#   "total_elapsed_ms": 87421,
#   "total_cost_usd": 0.1234,
#   "cost_status_breakdown": {"estimated": 12, "billed": 2, "unknown": 0},
#   "total_tool_calls": 31,
#   "last_turn_at_ms": 1700000123456,
#   "avg_turn_ms": 6244,
#   "last_tool_name": "bash"
# }
```

When `cost_status_breakdown.unknown > 0` the UI prefixes the headline
total with `~` and surfaces an info dot tooltipped "estimated".

### Frontend UI

The `/admin/sessions/{key}` page renders five linked surfaces:

- **EventTimeline** (`data-testid="event-timeline"`) — ordered turn
  cards, one per `TurnStart`. Each card contains the ordered parts.
- **ReasoningBlock** (`data-testid="reasoning-block"`) — collapsible
  "Thinking" panel with shimmer while `data-streaming="true"`.
- **ToolWidget** (`data-testid="tool-widget"`) — one row per tool
  call. State badge (`data-tool-state="pending|running|completed|error"`),
  inline arg summary, live-ticking elapsed counter. Click to expand →
  per-tool renderer shows full args + result.
- **SubagentTree** — nested timeline beneath the spawning tool widget
  when the child emits events through the `BubbleEmitter`.
- **CostFooter** (`data-testid="cost-footer"`) — sticky bottom row of
  five pills: total USD, turn count, average turn time, tool call
  count, "last turn N ago".

<!-- TODO: screenshot — save to docs/assets/observability-session-detail.png -->
<!-- The screenshot should show /admin/sessions/{key} mid-turn with    -->
<!-- a streaming reasoning block, two completed tool widgets, one      -->
<!-- running tool widget with subagent tree, and the cost footer.      -->

Per-turn drill-down lives at
`/admin/sessions/{key}/turns/{turn_id}` — the same timeline component
mounted in `mode="replay"`, seeded from the JSON replay endpoint.
Pixel-identical rendering means a finished turn reviewed an hour later
looks the same as it did when it was live.

### Channel adapters

Telegram / QQ / Discord / Slack / Feishu adapters render a mutable
status line per turn. Two consumption paths feed it:

1. **Legacy event tap** (`corlinman_channels._status._StatusFormatter`)
   — receives `tool_call` / `tool_result` / `done` / `error` from the
   in-process gRPC `ServerFrame` bus. Drives the steady-state
   `🔧 {tool} {arg}` and `✅ {tool} ({duration})` lines.
2. **New emitter.subscribe consumer** (W4.1) — subscribes to the same
   `EventEmitter` the SSE route consumes. Picks up the two signals the
   legacy tap doesn't carry:
   - `ToolStateHeartbeat` → refreshes the spinner to
     `🔧 {tool} … {elapsed_s}s` for long-running tools.
   - `Cancelling` → flips the spinner to `⏹ 正在取消…` within ~1s of the
     cancel button being pressed (previously waited for the next round).
   - `TurnComplete` → appends a one-line footer to the final reply:
     `(elapsed: 12.4s · 3 tool calls · ~$0.012)`. The `~` is dropped
     when `cost_status == "billed"`.

Both consumers run side by side; channels that don't subscribe to the
emitter keep their existing UX. Adapters added in the future should
prefer the emitter path — it's the same stream the UI sees.

### Configuration

| Knob | Default | Where | Notes |
|---|---|---|---|
| `CORLINMAN_TURN_EVENTS_TTL_DAYS` | 30 | env var (gateway) | Prune `turn_events` rows older than N days at gateway boot + once / day. Set to 0 to disable. |
| `CORLINMAN_TOOL_HEARTBEAT_INTERVAL_MS` | 10000 | env var (runner_pool) | How often `ToolStateHeartbeat` fires while a tool runs. Lower bound 1000. |
| `CORLINMAN_SSE_KEEPALIVE_INTERVAL_MS` | 10000 | env var (gateway) | SSE `: keepalive\n\n` comment frequency. Tune below your reverse proxy idle timeout. |
| `CORLINMAN_SSE_SUBSCRIBER_QUEUE_SIZE` | 256 | env var (gateway) | Per-subscriber bounded queue. Overflow drops oldest + reconnect uses `Last-Event-ID` for catch-up. |
| `[observability].emit_legacy_serverframe` | `true` | `config.toml` | When set to `false`, channels that still rely on the legacy gRPC `ServerFrame` stream lose their data source. Don't touch unless every adapter has migrated to `emitter.subscribe`. |

### Admin UI fixes (May 2026)

Tracking issue: [`docs/PLAN_UI_FIXES.md`](PLAN_UI_FIXES.md). This round
reconciles a split-brain state between `main` and the live deployment
at `corlinman.cornna.xyz`: the live gateway shipped a handful of admin
endpoints (`replay`, `provider test`, `provider/{name}/models`,
`provider kinds`) that never made it into `main`, while the
[task event stream](#task-event-stream) endpoints
(`/events/live`, `/turns/{turn_id}/events`, `/cost`) landed in `main`
but are absent from the live build. Until `main` is re-deployed, the UI
on the live origin calls endpoints that 404 either way — that's the
"用不了" complaint.

#### Endpoints backported into `main`

| Endpoint | Behaviour |
|---|---|
| `POST /admin/providers/{name}/test` | Zero-cost provider probe. For openai-compatible kinds it hits `/v1/models` on the configured `base_url`; for `anthropic` / `google` it returns `ok=true` with a `note` flag (those vendors don't expose a free models endpoint without a billed token). Never echoes the api key; latency capped at 5s. |
| `GET /admin/providers/{name}/models` | Model catalog discovery. Proxies `/v1/models` for openai-compatible providers, returns a hardcoded list from `corlinman_providers.specs` for `anthropic` / `google`. 30s in-memory cache; feeds the new `<ModelPickerDialog>`. |
| `GET /admin/providers/kinds` | **BREAKING** — response shape changed from `{kinds: [string]}` to `{kinds: [{kind, label, description, params_schema}]}`. Drives the schema-rendered custom-provider creation form. Old clients that consume just the `kind` string need to map over the new array. |
| `GET /admin/sessions/{key}/turns` | Past-turns listing. `{turns: [{turn_id, started_at_ms, ended_at_ms, status, model, tool_call_count, finish_reason, user_text_preview}], next_cursor}`. Cursor pagination via `?limit=50&before_id=...`. Powers the past-turns pill row above the EventTimeline so the session detail page is reachable beyond deep links. |
| `GET /admin/credentials/{provider}/{key}/reveal` | Admin-only cleartext reveal for the eye-icon UX on the credentials page. Auth-gated; the returned value is **never logged** (req/resp body redacted in the access log). |

#### UI upgrades shipped alongside

- **Credentials page** rebuilt around hermes-agent's `EnvPage` shape:
  `<EnvVarRow>` (paste-only secret input, eye-icon reveal with per-row
  client-side cache so toggling doesn't re-fetch) +
  `<ProviderGroupCard>` (prefix-grouped, collapsible per provider).
- **`<ModelPickerDialog>`** — two-column provider / model picker with a
  single search filter, mounted on `/admin/models` (add-alias flow) and
  `/admin/agents/[name]` (per-agent model override).
- **Past-turns pill row** above the EventTimeline on
  `/admin/sessions/{key}` — horizontal navigator with a "Load more"
  button. Calls `GET /admin/sessions/{key}/turns`.
- **`<TestConnectionButton>`** — one-click probe per provider row, with
  toast feedback showing latency or the upstream error message.
- **E2E smoke** (`ui/tests/e2e/admin-pages-smoke.spec.ts`) — visits
  seven admin surfaces, fails on 404 XHRs and console errors, so the
  next "UI calls missing endpoint" regression breaks CI before deploy.

#### Deployment guidance

Re-deploy `main` to production as soon as this round merges. The live
UI on `corlinman.cornna.xyz` already references the new SSE / cost /
replay endpoints; until the backend catches up, the session detail
page reports SSE 404s and the cost footer stays empty. Both the new
task-observability endpoints and the backported legacy endpoints
coexist by path, so deploying `main` is purely additive — no live
features regress.

### Future work

Out of scope for this round (tracked in
[`docs/PLAN_TASK_OBSERVABILITY.md` §4](PLAN_TASK_OBSERVABILITY.md)):

- Replace gRPC `ServerFrame` with `EventEnvelope` end-to-end and
  deprecate the legacy stream.
- Per-token timing — currently per-block is enough.
- Cost calculation accuracy improvements — we surface what
  `_CostMeter` already produces.
- Migrate Telegram/QQ adapters fully off the in-process tap and onto
  the SSE stream directly.
- WebSocket-based browsing of historical turn streams.
- OTel-style distributed tracing for the agent loop (the gateway-
  process spans below already cover the HTTP boundary).

---

## Platform tracing & metrics

This document catalogues the tracing spans and Prometheus metrics exposed
by the corlinman platform. It is intended as a quick
reference when debugging production issues or extending dashboards.

## Scrape endpoint

Prometheus scrapes `GET /metrics` on the gateway (default port `6005`).
The endpoint emits text-exposition v0.0.4 (`prometheus_client.generate_latest`) and is served by
`corlinman_server.gateway.routes.metrics`. Metric definitions live in
`corlinman_server.gateway.core.metrics`, which registers every family on a
dedicated `prometheus_client.CollectorRegistry` (`REGISTRY`) rather than the
process-global default registry.

## Metric families

### Core families

| Metric | Type | Labels |
|---|---|---|
| `corlinman_http_requests_total` | counter | `route`, `status` |
| `corlinman_chat_stream_duration_seconds` | histogram | `model`, `finish` |
| `corlinman_plugin_execute_total` | counter | `plugin`, `status` |
| `corlinman_plugin_execute_duration_seconds` | histogram | `plugin` |
| `corlinman_backoff_retries_total` | counter | `reason` |
| `corlinman_agent_grpc_inflight` | gauge | — |
| `corlinman_channels_rate_limited_total` | counter | `channel`, `reason` |
| `corlinman_vector_query_duration_seconds` | histogram | `stage` |

### Additional families

| Metric | Type | Labels |
|---|---|---|
| `corlinman_protocol_dispatch_total` | counter | `protocol` |
| `corlinman_protocol_dispatch_errors_total` | counter | `protocol`, `code` |
| `corlinman_wstool_invokes_total` | counter | `tool`, `ok` |
| `corlinman_wstool_invoke_duration_seconds` | histogram | `tool` |
| `corlinman_wstool_runners_connected` | gauge | — |
| `corlinman_file_fetcher_fetches_total` | counter | `scheme`, `ok` |
| `corlinman_file_fetcher_bytes_total` | counter | `scheme` |
| `corlinman_telegram_updates_total` | counter | `chat_type`, `mention_reason` |
| `corlinman_telegram_media_total` | counter | `kind` |
| `corlinman_hook_emits_total` | counter | `event_kind`, `priority` |
| `corlinman_hook_subscribers_current` | gauge | `priority` |
| `corlinman_skill_invocations_total` | counter | `skill_name` |
| `corlinman_agent_mutes_total` | counter | `expanded_agent` |
| `corlinman_rate_limit_triggers_total` | counter | `limit_type` |
| `corlinman_approvals_total` | counter | `decision` |

Label cardinality is kept bounded:
- `protocol` ∈ `{block, openai_function, unknown}`
- `code` ∈ `{unknown_tool, protocol_not_advertised, parse, coercion}`
- `ok` ∈ `{true, false}`
- `scheme` ∈ `{file, http, https, ws-tool, other}`
- `priority` ∈ `{critical, normal, low}`
- `kind` ∈ `{photo, voice, document, text}`
- `decision` ∈ `{allow, deny, timeout}`
- `mention_reason` ∈ `{private, group_addressed, group_ignored}`
- `limit_type` ∈ `{<reason>_<channel>}` — keep channels bounded

## Tracing spans

| Span | Module | Fields |
|---|---|---|
| `hook_emit` | `corlinman_hooks.bus` | `event_kind`, `session_key`, `priority_tier_count` |
| `placeholder_render` | `corlinman_server.gateway.grpc.placeholder` | `template_len`, `depth_used`, `unresolved_count` |
| `protocol_dispatch` | `corlinman_providers.plugins.protocol.dispatcher` | `outcomes_count`, `block_count`, `fc_count` |
| `block_parse` | `corlinman_providers.plugins.protocol.block` | `envelope_count`, `error_count` |
| `wstool_invoke` | `corlinman_wstool.runtime` | `tool`, `runner_id`, `duration_ms`, `ok` |
| `file_fetch` | `corlinman_wstool.file_fetcher` | `uri_scheme`, `total_bytes`, `ok` |
| `telegram_webhook` | `corlinman_channels.telegram.webhook` | `chat_type`, `mention_reason`, `media_kind` |
| `epa_backfill` (structlog event) | `corlinman_agent.rag.epa_backfill` | `chunks_processed`, `basis_axes`, `wall_clock_s`, `chunks_skipped`, `namespaces_touched`, `namespace`, `status` |

Spans are emitted via OpenTelemetry. The gateway forwards them to an
OTLP collector when `OTEL_EXPORTER_OTLP_ENDPOINT` is set (see
`corlinman_server.telemetry`); once initialised, `structlog` stamps
`trace_id` / `span_id` onto log lines.

## Common queries

### PromQL

- Protocol dispatch QPS:
  ```promql
  sum by(protocol) (rate(corlinman_protocol_dispatch_total[1m]))
  ```
- WsTool p99 latency (global):
  ```promql
  histogram_quantile(0.99, sum by(le) (rate(corlinman_wstool_invoke_duration_seconds_bucket[5m])))
  ```
- FileFetcher bytes/s by scheme:
  ```promql
  sum by(scheme) (rate(corlinman_file_fetcher_bytes_total[1m]))
  ```
- Approval allow ratio:
  ```promql
  sum(rate(corlinman_approvals_total{decision="allow"}[5m]))
  / sum(rate(corlinman_approvals_total[5m]))
  ```
- Dispatch error breakdown:
  ```promql
  sum by(code) (rate(corlinman_protocol_dispatch_errors_total[5m]))
  ```
- Hook emit rate by kind:
  ```promql
  sum by(event_kind) (rate(corlinman_hook_emits_total[1m]))
  ```
- Rate-limit drops by limit_type:
  ```promql
  sum by(limit_type) (rate(corlinman_rate_limit_triggers_total[5m]))
  ```

### Tracing filters

The gateway logs via `structlog`; the uvicorn/root log level is set with `LOG_LEVEL` (default `info`). Because structlog routes through stdlib `logging`, individual loggers can be raised or lowered with the stdlib API:

- Raise the global level:
  ```bash
  LOG_LEVEL=debug
  ```
- Drill into WsTool timing:
  ```python
  import logging
  logging.getLogger("corlinman_wstool.runtime").setLevel(logging.DEBUG)
  ```
- Quiet everything except hook emits:
  ```python
  import logging
  logging.getLogger().setLevel(logging.WARNING)
  logging.getLogger("corlinman_hooks.bus").setLevel(logging.INFO)
  ```

With an OTLP collector attached, the same field names (`tool`,
`runner_id`, `duration_ms`, `event_kind`, ...) are queryable in Tempo /
Jaeger as span attributes.

## Pointing an OTel collector at the gateway

The gateway initialises a tracer provider when `OTEL_EXPORTER_OTLP_ENDPOINT`
is set. Minimal setup with `docker-compose`:

```yaml
# ops/docker-compose.observability.yml (excerpt)
services:
  otel-collector:
    image: otel/opentelemetry-collector-contrib:latest
    command: ["--config=/etc/otelcol/config.yaml"]
    volumes:
      - ./otel-collector.yaml:/etc/otelcol/config.yaml
    ports:
      - "4317:4317"   # OTLP gRPC (tracing)
      - "4318:4318"   # OTLP HTTP

  corlinman-gateway:
    environment:
      OTEL_EXPORTER_OTLP_ENDPOINT: "http://otel-collector:4317"
```

Collector config routes traces to Tempo/Jaeger and leaves metrics to
Prometheus (which scrapes `/metrics` directly — no OTLP metric export is
configured).

## Grafana dashboard

`ops/dashboards/corlinman.json` — import into Grafana 10+, wire the
`DS_PROMETHEUS` input to a Prometheus datasource that scrapes the
gateway. Dashboard UID: `corlinman-gateway`.
