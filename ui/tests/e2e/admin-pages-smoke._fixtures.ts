/**
 * Shared fixtures for `admin-pages-smoke.spec.ts`.
 *
 * Each constant mirrors the wire shape its consuming endpoint
 * actually returns — see the type definitions in `ui/lib/api.ts` and
 * the corresponding FastAPI handlers under
 * `python/packages/corlinman-server/src/corlinman_server/gateway/`.
 *
 * Sibling-only export — these fixtures are intentionally not part of
 * the `@/lib/api` public surface; they exist purely so the spec stays
 * scannable.
 */

export const SESSION_KEY = "telegram:42:smoke-test";
export const TURN_ID = "deadbeefcafef00d0123456789abcdef";
export const REVEAL_VALUE = "sk-test-cleartext-XyZ4";

/** `GET /admin/sessions` — one row, no `503 sessions_disabled`. */
export const SESSIONS_LIST_RESPONSE = {
  sessions: [
    {
      session_key: SESSION_KEY,
      message_count: 4,
      last_message_at: Date.now() - 60_000,
      last_seen_at_ms: Date.now() - 30_000,
      last_user_text_preview: "How does the journal flush work?",
    },
  ],
} as const;

/**
 * `GET /admin/sessions/{key}/cost` — the W2.3 cost footer + list-row
 * cells consume this. Shape mirrors `SessionCostResponse` in
 * `components/sessions/cost-footer.tsx`.
 */
export const COST_RESPONSE = {
  session_key: SESSION_KEY,
  turn_count: 2,
  total_elapsed_ms: 2_500,
  total_cost_usd: 0.0245,
  cost_status_breakdown: { estimated: 1, billed: 1, unknown: 0 },
  total_tool_calls: 3,
  last_turn_at_ms: Date.now() - 30_000,
  avg_turn_ms: 1_250,
  last_tool_name: "bash",
} as const;

/**
 * `GET /admin/sessions/{key}/turns` — W1.2 backport. Three pills' worth
 * of past turns so the navigator paints something interesting.
 */
export const TURNS_LIST_RESPONSE = {
  session_key: SESSION_KEY,
  turns: [
    {
      turn_id: "aaaa1111aaaa1111aaaa1111aaaa1111",
      started_at_ms: Date.now() - 120_000,
      ended_at_ms: Date.now() - 118_000,
      status: "completed",
      finish_reason: "stop",
      elapsed_ms: 2_000,
      tool_call_count: 1,
      user_text_preview: "first prompt",
    },
    {
      turn_id: "bbbb2222bbbb2222bbbb2222bbbb2222",
      started_at_ms: Date.now() - 90_000,
      ended_at_ms: Date.now() - 88_500,
      status: "completed",
      finish_reason: "stop",
      elapsed_ms: 1_500,
      tool_call_count: 0,
      user_text_preview: "follow-up",
    },
    {
      turn_id: "cccc3333cccc3333cccc3333cccc3333",
      started_at_ms: Date.now() - 30_000,
      ended_at_ms: Date.now() - 28_000,
      status: "completed",
      finish_reason: "stop",
      elapsed_ms: 2_000,
      tool_call_count: 2,
      user_text_preview: "third turn",
    },
  ],
  next_cursor: null,
} as const;

/**
 * Minimal 4-event turn fixture — `TurnStart` + a tool block + `TurnComplete`.
 * Drives both the SSE live stream and the drill-down JSON replay. We
 * intentionally keep this skinnier than `task-observability.spec.ts`'s
 * 14-event fixture: the goal here is "does the page mount", not "does
 * every block type render".
 */
export const FIXTURE_TURN_EVENTS = [
  {
    turn_id: TURN_ID,
    sequence: 1,
    timestamp_ms: 1_700_000_000_000,
    event_type: "TurnStart",
    payload: {
      model: "anthropic/claude-3-5-sonnet",
      user_text: "Smoke-test turn body.",
      system_message_preview: "",
    },
  },
  {
    turn_id: TURN_ID,
    sequence: 2,
    timestamp_ms: 1_700_000_000_100,
    event_type: "BlockStart",
    // The timeline reducer (lib/sessions/store.ts) keys parts by
    // `block_id` and branches on `block_kind` — see BlockStartPayload.
    payload: {
      block_id: "call_smoke",
      block_kind: "tool_use",
      tool_name: "bash",
    },
  },
  {
    turn_id: TURN_ID,
    sequence: 3,
    timestamp_ms: 1_700_000_000_200,
    event_type: "ToolStateCompleted",
    payload: {
      block_id: "call_smoke",
      output: "ok",
      is_error: false,
    },
  },
  {
    turn_id: TURN_ID,
    sequence: 4,
    timestamp_ms: 1_700_000_000_300,
    event_type: "TurnComplete",
    payload: {
      finish_reason: "stop",
      usage: { input_tokens: 10, output_tokens: 5 },
      elapsed_ms: 300,
      estimated_cost_usd: 0.0001,
      cost_status: "estimated",
    },
  },
] as const;

/** Encode the fixture events as an SSE-formatted body. */
export function buildSseTurnEventsBody(): string {
  return (
    FIXTURE_TURN_EVENTS.map(
      (ev) =>
        `id: ${ev.turn_id}:${ev.sequence}\ndata: ${JSON.stringify(ev)}\n`,
    ).join("\n") + "\n"
  );
}

/**
 * 3 log entries (info + warn + error) wrapped in SSE frames. The logs
 * page subscribes to `data:` frames under the `log` and `message`
 * events.
 */
const LOG_ENTRIES = [
  {
    ts: new Date(Date.now() - 5_000).toISOString(),
    level: "info",
    subsystem: "gateway",
    message: "started",
    trace_id: "trace-1",
  },
  {
    ts: new Date(Date.now() - 3_000).toISOString(),
    level: "warn",
    subsystem: "providers",
    message: "rate-limit nearing",
    trace_id: "trace-2",
  },
  {
    ts: new Date(Date.now() - 1_000).toISOString(),
    level: "error",
    subsystem: "channels",
    message: "websocket dropped",
    trace_id: "trace-3",
  },
];

export function buildSseLogsBody(): string {
  return (
    LOG_ENTRIES.map(
      (ev) => `event: log\ndata: ${JSON.stringify(ev)}\n`,
    ).join("\n") + "\n"
  );
}

/**
 * `GET /admin/providers` — one enabled openai-kind row. Empty params
 * schema so the editor dialog stays cheap.
 */
export const PROVIDERS_RESPONSE = {
  providers: [
    {
      name: "openai",
      kind: "openai",
      enabled: true,
      base_url: null,
      api_key_source: "env",
      api_key_env_name: "OPENAI_API_KEY",
      params: {},
      params_schema: { type: "object", properties: {} },
      capabilities: { chat: true, embedding: false },
    },
  ],
} as const;

/** `POST /admin/providers/{name}/test` — happy-path probe result. */
export const PROVIDER_TEST_OK = {
  ok: true,
  latency_ms: 120,
  models_count: 12,
} as const;

/** `GET /admin/providers/{name}/models` — two-row catalog. */
export const PROVIDER_MODELS_RESPONSE = {
  models: [
    { id: "gpt-4o", display_name: "GPT-4o" },
    { id: "gpt-4o-mini", display_name: "GPT-4o mini" },
  ],
} as const;

/** `GET /admin/personas` — one editable persona for nested picker smoke. */
export const PERSONAS_RESPONSE = {
  personas: [
    {
      id: "alyssa",
      display_name: "Alyssa P. Hacker",
      short_summary: "Test persona",
      system_prompt: "# Alyssa\n\nHelpful test persona.",
      avatar_url: null,
      model_bindings: {
        text: { provider: null, model: null },
        image: { provider: null, model: null },
        voice: { provider: null, model: null },
      },
      is_builtin: false,
      created_at_ms: 1_700_000_000_000,
      updated_at_ms: 1_700_000_000_000,
    },
  ],
} as const;

/**
 * `GET /admin/providers/kinds` — new descriptor shape from W1.1. We
 * include params_schema so any consumer that immediately renders the
 * dynamic form has something valid to chew on.
 */
export const PROVIDER_KINDS_RESPONSE = {
  kinds: [
    {
      kind: "openai",
      label: "OpenAI",
      description: "OpenAI public API",
      params_schema: { type: "object", properties: {} },
    },
    {
      kind: "anthropic",
      label: "Anthropic",
      description: "Anthropic Messages API",
      params_schema: { type: "object", properties: {} },
    },
  ],
} as const;

/**
 * `GET /admin/credentials` — one provider with a populated `api_key`
 * preview so the eye-icon row renders the reveal button.
 */
export const CREDENTIALS_RESPONSE = {
  providers: [
    {
      name: "openai",
      kind: "openai",
      enabled: true,
      fields: [
        {
          key: "api_key",
          set: true,
          preview: "…XyZ4",
          env_ref: null,
        },
        {
          key: "base_url",
          set: false,
          preview: null,
          env_ref: null,
        },
      ],
    },
  ],
} as const;

/** `GET /admin/oauth/status` — empty so the panel renders the "Login" CTAs. */
export const OAUTH_STATUS_EMPTY = {
  providers: [
    { id: "anthropic", source: "none", expires_in_seconds: null, username: null },
    { id: "codex", source: "none", expires_in_seconds: null, username: null },
    { id: "gemini", source: "none", expires_in_seconds: null, username: null },
    { id: "xai", source: "none", expires_in_seconds: null, username: null },
  ],
} as const;

/**
 * `GET /admin/models` — v2 shape (`aliases: AliasView[]`) with one
 * alias pointing at the stubbed openai provider.
 */
export const MODELS_V2_RESPONSE = {
  default: "claude-opus-4-7",
  providers: [
    {
      name: "openai",
      kind: "openai",
      enabled: true,
      base_url: null,
      api_key_source: "env",
      api_key_env_name: "OPENAI_API_KEY",
      params: {},
      params_schema: { type: "object", properties: {} },
    },
    {
      name: "anthropic",
      kind: "anthropic",
      enabled: true,
      base_url: null,
      api_key_source: "env",
      api_key_env_name: "ANTHROPIC_API_KEY",
      params: {},
      params_schema: { type: "object", properties: {} },
    },
  ],
  aliases: [
    {
      name: "smart",
      provider: "openai",
      model: "gpt-4o",
      params: {},
      effective_params_schema: { type: "object", properties: {} },
    },
  ],
} as const;
