/**
 * corlinman admin API client.
 *
 * Always hits the real gateway at `NEXT_PUBLIC_GATEWAY_URL`. Default
 * is an empty string so request paths resolve relative to the current
 * origin (nginx proxies `/admin/*`, `/health`, `/v1/*`, `/metrics`,
 * and `/plugin-callback` to the gateway in production). `credentials:
 * "include"` forwards the session cookie the gateway sets.
 *
 * For local dev without a reverse proxy, set
 * `NEXT_PUBLIC_GATEWAY_URL=http://localhost:6005` as an opt-in escape
 * hatch.
 */

import type { LiveEvent as _W21LiveEvent } from "@/lib/sessions/event-stream";

export const GATEWAY_BASE_URL = process.env.NEXT_PUBLIC_GATEWAY_URL ?? "";

export interface ApiError extends Error {
  status?: number;
  traceId?: string;
}

export class CorlinmanApiError extends Error implements ApiError {
  status?: number;
  traceId?: string;
  constructor(message: string, status?: number, traceId?: string) {
    super(message);
    this.name = "CorlinmanApiError";
    this.status = status;
    this.traceId = traceId;
  }
}

export interface RequestOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
}

/** Thin fetch wrapper; throws CorlinmanApiError on non-2xx. */
export async function apiFetch<T>(
  path: string,
  opts: RequestOptions = {},
): Promise<T> {
  const { body, headers, ...rest } = opts;

  const res = await fetch(`${GATEWAY_BASE_URL}${path}`, {
    credentials: "include",
    headers: {
      "content-type": "application/json",
      ...(headers ?? {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    ...rest,
  });

  const traceId = res.headers.get("x-request-id") ?? undefined;
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new CorlinmanApiError(
      text || `Request failed: ${res.status}`,
      res.status,
      traceId,
    );
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// --- typed admin surfaces ---------------------------------------------------
// All hit live `corlinman-gateway::routes::admin` endpoints.

export type PluginStatus = "loaded" | "disabled" | "error";

export interface PluginSummary {
  name: string;
  version: string;
  status: PluginStatus;
  manifest_path: string;
  origin: "Bundled" | "Global" | "Workspace" | "Config";
  plugin_type: "synchronous" | "asynchronous" | "messagePreprocessor";
  capabilities: string[];
  description: string;
  last_touched_at: string;
  error?: string;
}

export async function listPlugins(): Promise<PluginSummary[]> {
  return apiFetch<PluginSummary[]>("/admin/plugins");
}

export interface AgentSummary {
  name: string;
  file_path: string;
  bytes: number;
  last_modified: string;
  // W1.2: tier the registry resolved this card from. ``built-in`` rows
  // are immutable from the API surface. Older gateways predate this
  // field, so it's optional on the wire — callers default to
  // ``"user"`` when missing so the source badge still renders.
  source?: "built-in" | "user" | "project";
  // W1.2: card.description (first line of the card body). Optional —
  // older gateways omit it.
  description?: string;
}

export async function listAgents(): Promise<AgentSummary[]> {
  return apiFetch<AgentSummary[]>("/admin/agents");
}

export interface ApprovalItem {
  id: string;
  plugin: string;
  tool: string;
  sessionKey: string;
  requestedAt: string;
  argsPreview: string;
}

export async function listPendingApprovals(): Promise<ApprovalItem[]> {
  return apiFetch<ApprovalItem[]>("/admin/approvals");
}

// --- Approvals (S2 T3 wired, S5 T4 expanded with batch helper) -------------
// Matches the Rust `ApprovalOut` shape in
// rust/crates/corlinman-gateway/src/routes/admin/approvals.rs.
export interface Approval {
  id: string;
  plugin: string;
  tool: string;
  session_key: string;
  args_json: string;
  requested_at: string;
  decided_at: string | null;
  decision: string | null;
}

export function fetchApprovals(includeDecided: boolean): Promise<Approval[]> {
  const qs = includeDecided ? "?include_decided=true" : "";
  return apiFetch<Approval[]>(`/admin/approvals${qs}`);
}

export interface DecideResult {
  id: string;
  decision: string;
}

export function decideApproval(
  id: string,
  approve: boolean,
  reason?: string,
): Promise<DecideResult> {
  return apiFetch<DecideResult>(`/admin/approvals/${id}/decide`, {
    method: "POST",
    body: { approve, reason },
  });
}

/** Outcome of a single decide call inside a batch. */
export interface BatchDecideOutcome {
  id: string;
  ok: boolean;
  error?: string;
}

/** Fires every decide in parallel with `Promise.allSettled` and reports per-id
 * outcomes so the caller can revert optimistic updates for the failed ones. */
export async function decideApprovalsBatch(
  ids: string[],
  approve: boolean,
  reason?: string,
): Promise<BatchDecideOutcome[]> {
  const results = await Promise.allSettled(
    ids.map((id) => decideApproval(id, approve, reason)),
  );
  return results.map((r, i) => {
    const id = ids[i]!;
    if (r.status === "fulfilled") return { id, ok: true };
    const msg = r.reason instanceof Error ? r.reason.message : String(r.reason);
    return { id, ok: false, error: msg };
  });
}

/** Convenience re-export for callers that want the SSE helper. */
export { openEventStream } from "./sse";

// ---------------------------------------------------------------------------
// S6 T1 — RAG admin surface
// ---------------------------------------------------------------------------

export interface RagStats {
  ready: boolean;
  files: number;
  chunks: number;
  tags: number;
}
export function fetchRagStats(): Promise<RagStats> {
  return apiFetch<RagStats>("/admin/rag/stats");
}

export interface RagHit {
  chunk_id: number;
  score: number;
  content_preview: string;
}
export interface RagQueryResponse {
  backend: string;
  q: string;
  k: number;
  hits: RagHit[];
}
export function queryRag(q: string, k = 10): Promise<RagQueryResponse> {
  const qs = new URLSearchParams({ q, k: String(k) }).toString();
  return apiFetch<RagQueryResponse>(`/admin/rag/query?${qs}`);
}
export function rebuildRag(): Promise<{ status: string; target: string }> {
  return apiFetch<{ status: string; target: string }>("/admin/rag/rebuild", {
    method: "POST",
  });
}

// ---------------------------------------------------------------------------
// S6 T2 — QQ channel admin surface
// ---------------------------------------------------------------------------

export interface QqStatus {
  configured: boolean;
  enabled: boolean;
  ws_url: string | null;
  self_ids: number[];
  group_keywords: Record<string, string[]>;
  runtime: "unknown" | "connected" | "disconnected";
  recent_messages: unknown[];
  // NapCat WS heartbeat health.
  health_online?: boolean | null;
  health_last_event_at_ms?: number | null;
  health_seconds_since_event?: number | null;
  health_checked_at_ms?: number | null;
  // Bot QQ account state — independent of WS health. False after
  // Tencent kicks the account offline; UI should surface a "re-scan
  // QR via NapCat WebUI" banner when False.
  account_online?: boolean | null;
  account_qq?: number | null;
  account_nickname?: string | null;
  account_checked_at_ms?: number | null;
  account_last_error?: string | null;
}
export function fetchQqStatus(): Promise<QqStatus> {
  return apiFetch<QqStatus>("/admin/channels/qq/status");
}
export function reconnectQq(): Promise<unknown> {
  return apiFetch("/admin/channels/qq/reconnect", { method: "POST" });
}
export function updateQqKeywords(
  groupKeywords: Record<string, string[]>,
): Promise<{ status: string; group_keywords: Record<string, string[]> }> {
  return apiFetch("/admin/channels/qq/keywords", {
    method: "POST",
    body: { group_keywords: groupKeywords },
  });
}

// v0.3 — QQ scan-login (NapCat proxy). Four endpoints:
//   POST /admin/channels/qq/qrcode         → { token, image_base64?, qrcode_url?, expires_at }
//   GET  /admin/channels/qq/qrcode/status  → { status, account?, message? }
//   GET  /admin/channels/qq/accounts       → { accounts: QqAccount[] }
//   POST /admin/channels/qq/quick-login    → { status, account?, message? }
export interface QqAccount {
  uin: string;
  nickname?: string;
  avatar_url?: string;
  /** epoch-ms */
  last_login_at: number;
}
export interface QqQrcode {
  token: string;
  /** Base64 PNG (no data: prefix) when NapCat returned an image. */
  image_base64: string | null;
  /** ptqrshow URL when NapCat returned a URL instead of an image. */
  qrcode_url: string | null;
  /** epoch-ms expiry. */
  expires_at: number;
}
export type QqLoginStatus =
  | "waiting"
  | "scanned"
  | "confirmed"
  | "expired"
  | "error";
export interface QqQrcodeStatus {
  status: QqLoginStatus;
  account?: QqAccount;
  message?: string;
}
export function requestQqQrcode(): Promise<QqQrcode> {
  return apiFetch<QqQrcode>("/admin/channels/qq/qrcode", { method: "POST" });
}
export function fetchQqQrcodeStatus(token: string): Promise<QqQrcodeStatus> {
  const qs = new URLSearchParams({ token });
  return apiFetch<QqQrcodeStatus>(
    `/admin/channels/qq/qrcode/status?${qs.toString()}`,
  );
}
export function fetchQqAccounts(): Promise<{ accounts: QqAccount[] }> {
  return apiFetch<{ accounts: QqAccount[] }>("/admin/channels/qq/accounts");
}
export function qqQuickLogin(uin: string): Promise<QqQrcodeStatus> {
  return apiFetch<QqQrcodeStatus>("/admin/channels/qq/quick-login", {
    method: "POST",
    body: { uin },
  });
}

// ---------------------------------------------------------------------------
// S6 T3 — Scheduler admin surface
// ---------------------------------------------------------------------------

export interface SchedulerJob {
  name: string;
  cron: string;
  timezone: string | null;
  action_kind: string;
  next_fire_at: string | null;
  last_status: string | null;
  // W6 extras — present on `source === "runtime"` rows only. Optional so
  // the legacy config-derived rows keep type-checking.
  action_type?: string | null;
  enabled?: boolean;
  persona_id?: string | null;
  prompt_template?: string | null;
  qq_account?: string | null;
  /** `"config"` for `[[scheduler.jobs]]` rows; `"runtime"` for
   * operator-created jobs (editable / pausable from the UI). */
  source?: "config" | "runtime";
}
export function fetchSchedulerJobs(): Promise<SchedulerJob[]> {
  return apiFetch<SchedulerJob[]>("/admin/scheduler/jobs");
}
/** Pause a runtime scheduler job (sets `enabled=false`, stops its loop). */
export function pauseSchedulerJob(name: string): Promise<SchedulerJob> {
  return apiFetch<SchedulerJob>(
    `/admin/scheduler/jobs/${encodeURIComponent(name)}/pause`,
    { method: "POST" },
  );
}
/** Resume a runtime scheduler job (sets `enabled=true`, restarts its loop). */
export function resumeSchedulerJob(name: string): Promise<SchedulerJob> {
  return apiFetch<SchedulerJob>(
    `/admin/scheduler/jobs/${encodeURIComponent(name)}/resume`,
    { method: "POST" },
  );
}
export interface SchedulerHistory {
  job: string;
  at: string;
  source: string;
  status: string;
  message: string;
}
export function fetchSchedulerHistory(): Promise<SchedulerHistory[]> {
  return apiFetch<SchedulerHistory[]>("/admin/scheduler/history");
}
export function triggerSchedulerJob(name: string): Promise<unknown> {
  return apiFetch(`/admin/scheduler/jobs/${encodeURIComponent(name)}/trigger`, {
    method: "POST",
  });
}

// ---------------------------------------------------------------------------
// S6 T4 — Config admin surface
// ---------------------------------------------------------------------------

export interface ConfigGetResponse {
  toml: string;
  version: string;
  meta: Record<string, unknown>;
}
export function fetchConfig(): Promise<ConfigGetResponse> {
  return apiFetch<ConfigGetResponse>("/admin/config");
}
export interface ConfigIssue {
  path: string;
  code: string;
  message: string;
  level: "error" | "warn";
}
export interface ConfigPostResponse {
  status: "ok" | "invalid";
  issues: ConfigIssue[];
  requires_restart: string[];
  version?: string;
}
export function postConfig(
  toml: string,
  dryRun: boolean,
): Promise<ConfigPostResponse> {
  return apiFetch<ConfigPostResponse>("/admin/config", {
    method: "POST",
    body: { toml, dry_run: dryRun },
  });
}
export function fetchConfigSchema(): Promise<unknown> {
  return apiFetch("/admin/config/schema");
}

// ---------------------------------------------------------------------------
// Channel enable toggle — convenience wrappers over /admin/config that
// mutate only the `enabled` field of `[channels.qq]` / `[channels.telegram]`
// while preserving the rest of the TOML (including comments and ordering).
//
// Regex-scoped to a single `enabled = true|false` line inside the addressed
// section header. Trailing comments on that line are preserved. If the
// section or the `enabled` key is missing, it's appended/inserted rather
// than touching anything else.
// ---------------------------------------------------------------------------

export type ChannelName =
  | "qq"
  | "telegram"
  | "discord"
  | "slack"
  | "feishu"
  | "wechat_official"
  | "qq_official";

/** Read the current `enabled` flag for a channel from a TOML string. */
export function readChannelEnabled(toml: string, channel: ChannelName): boolean {
  const headerRe = new RegExp(`^\\[channels\\.${channel}\\]\\s*$`, "m");
  const headerMatch = headerRe.exec(toml);
  if (!headerMatch) return false;
  const body = sectionBody(toml, headerMatch.index + headerMatch[0].length);
  const enabledMatch = /^\s*enabled\s*=\s*(true|false)/m.exec(body);
  return enabledMatch?.[1] === "true";
}

/**
 * Return `toml` with the `enabled` field of `[channels.<channel>]` set to
 * `next`. Creates the section if absent. Preserves the rest of the file.
 * Exported for unit testing — prefer `setChannelEnabled()` in UI code.
 */
export function patchChannelEnabled(
  toml: string,
  channel: ChannelName,
  next: boolean,
): string {
  const header = `[channels.${channel}]`;
  const headerRe = new RegExp(`^\\[channels\\.${channel}\\]\\s*$`, "m");
  const headerMatch = headerRe.exec(toml);

  if (!headerMatch) {
    const sep = toml.endsWith("\n\n") ? "" : toml.endsWith("\n") ? "\n" : "\n\n";
    return `${toml}${sep}${header}\nenabled = ${next}\n`;
  }

  const headerStart = headerMatch.index;
  const headerEnd = headerStart + headerMatch[0].length;
  const body = sectionBody(toml, headerEnd);
  const bodyEnd = headerEnd + body.length;

  const enabledRe = /^(\s*enabled\s*=\s*)(true|false)/m;
  if (enabledRe.test(body)) {
    const newBody = body.replace(enabledRe, `$1${next}`);
    return toml.slice(0, headerEnd) + newBody + toml.slice(bodyEnd);
  }

  // Section exists but lacks an `enabled` line — insert right after header.
  const insertion = `\nenabled = ${next}`;
  return toml.slice(0, headerEnd) + insertion + body + toml.slice(bodyEnd);
}

/** Extract the body of a TOML section starting at `from` up to the next `^\[` header or EOF. */
function sectionBody(toml: string, from: number): string {
  const rest = toml.slice(from);
  const nextHeader = /\n\[/.exec(rest);
  return nextHeader ? rest.slice(0, nextHeader.index) : rest;
}

/**
 * Fetch current config, flip `channels.<channel>.enabled`, POST back.
 * Returns the raw `/admin/config` response — callers should inspect
 * `status === "invalid"` and surface `issues` to the user (e.g. Telegram
 * rejecting enable when `bot_token` is missing).
 */
export async function setChannelEnabled(
  channel: ChannelName,
  enabled: boolean,
): Promise<ConfigPostResponse> {
  const current = await fetchConfig();
  const nextToml = patchChannelEnabled(current.toml, channel, enabled);
  return postConfig(nextToml, false);
}

// ---------------------------------------------------------------------------
// S6 T5 — Models admin surface
// ---------------------------------------------------------------------------

export interface ProviderRow {
  name: string;
  enabled: boolean;
  has_api_key: boolean;
  api_key_kind: "env" | "literal" | null;
  base_url: string | null;
}
export interface ModelsResponse {
  default: string;
  aliases: Record<string, string>;
  providers: ProviderRow[];
}
export function fetchModels(): Promise<ModelsResponse> {
  return apiFetch<ModelsResponse>("/admin/models");
}
export function updateAliases(
  aliases: Record<string, string>,
  defaultModel?: string,
): Promise<{ status: string; default: string; aliases: Record<string, string> }> {
  return apiFetch("/admin/models/aliases", {
    method: "POST",
    body: { aliases, default: defaultModel },
  });
}

// ---------------------------------------------------------------------------
// S6 T6 — Plugin invoke + Agent editor
// ---------------------------------------------------------------------------

export interface PluginInvokeResponse {
  status: "success" | "error" | "accepted";
  duration_ms: number;
  result?: unknown;
  result_raw?: string | null;
  code?: number;
  message?: string;
  task_id?: string;
}
export function invokePlugin(
  name: string,
  tool: string,
  args: unknown,
): Promise<PluginInvokeResponse> {
  return apiFetch<PluginInvokeResponse>(
    `/admin/plugins/${encodeURIComponent(name)}/invoke`,
    { method: "POST", body: { tool, arguments: args } },
  );
}

export interface PluginDetail {
  summary: PluginSummary;
  manifest: Record<string, unknown>;
  diagnostics: unknown[];
}
export function fetchPluginDetail(name: string): Promise<PluginDetail> {
  return apiFetch<PluginDetail>(`/admin/plugins/${encodeURIComponent(name)}`);
}

export interface AgentContent {
  name: string;
  file_path: string;
  bytes: number;
  last_modified: string | null;
  content: string;
}
export function fetchAgent(name: string): Promise<AgentContent> {
  return apiFetch<AgentContent>(`/admin/agents/${encodeURIComponent(name)}`);
}
export function saveAgent(
  name: string,
  content: string,
): Promise<{ status: string; name: string; file_path: string; bytes: number }> {
  return apiFetch(`/admin/agents/${encodeURIComponent(name)}`, {
    method: "POST",
    body: { content },
  });
}

// ---------------------------------------------------------------------------
// Wave 2 — Agent CRUD (W2.1) + Subagent activity (W2.2)
//
// Mirrors `gateway/routes_admin_a/agents.py` (W1.2) and the subagent
// surface in `routes_admin_a/subagents.py` (W1.3). The list endpoint
// extended `AgentSummary` with `source` + `description` (added above).
// ---------------------------------------------------------------------------

/** Body for POST /admin/agents. Slug regex: `^[a-z][a-z0-9_-]*$`. */
export interface CreateAgentBody {
  name: string;
  format: "yaml" | "md";
  body: string;
  /** Override a built-in card. Server returns 409 without this set when
   * the name collides with a `source === "built-in"` registry entry. */
  force?: boolean;
}

/** 201 response envelope from POST /admin/agents. Mirrors
 * `CreatedAgentResponse` on the gateway. The `card` field is reserved
 * for future enrichment — today the wire returns the flat row only. */
export interface CreatedAgentResponse {
  status: "ok";
  name: string;
  file_path: string;
  bytes: number;
  source: "user" | "project";
  last_modified: string | null;
}

/** POST /admin/agents → 201 + created card. 400 on duplicate / invalid
 * name; 409 ``shadows_builtin`` when name collides with a built-in and
 * `force` is unset. */
export function createAgent(
  body: CreateAgentBody,
): Promise<CreatedAgentResponse> {
  return apiFetch<CreatedAgentResponse>("/admin/agents", {
    method: "POST",
    body,
  });
}

/** DELETE /admin/agents/{name} → 204 on success. 409 for built-ins
 * (server refuses to delete read-only cards), 404 when the overlay
 * file doesn't exist. */
export function deleteAgent(name: string): Promise<void> {
  return apiFetch<void>(`/admin/agents/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

/** POST /admin/agents/reload — re-scan the agent dir stack. Returns
 * the post-reload registry size + every resolved card name (the
 * gateway calls this list ``names``; consumers can ignore it when
 * they don't need the cache-busting payload). */
export interface ReloadAgentsResponse {
  status: "ok";
  count: number;
  names: string[];
}
export function reloadAgents(): Promise<ReloadAgentsResponse> {
  return apiFetch<ReloadAgentsResponse>("/admin/agents/reload", {
    method: "POST",
  });
}

/** Lifecycle states a subagent walks through. Mirrors
 * `SubagentState` in `routes_admin_a/subagents.py`. */
export type SubagentState =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "timeout"
  | "killed"
  | "stalled";

/** One row in `GET /admin/subagents` / status / SSE frames. */
export interface SubagentStatusResponse {
  request_id: string;
  parent_session_key: string;
  subagent_type: string;
  description: string | null;
  state: SubagentState;
  /** epoch-ms */
  started_at: number | null;
  /** epoch-ms */
  finished_at: number | null;
  child_session_key: string | null;
  finish_reason: string | null;
  tool_calls_made: number;
  elapsed_ms: number;
  error: string | null;
  summary: string;
}

export interface SubagentListResponse {
  subagents: SubagentStatusResponse[];
}

/** GET /admin/subagents?include_terminal=… */
export function listSubagents(
  opts: { include_terminal?: boolean } = {},
): Promise<SubagentListResponse> {
  const qs = new URLSearchParams();
  if (opts.include_terminal !== undefined) {
    qs.set("include_terminal", String(opts.include_terminal));
  }
  const suffix = qs.toString() ? `?${qs}` : "";
  return apiFetch<SubagentListResponse>(`/admin/subagents${suffix}`);
}

/** GET /admin/subagents/{request_id}/status */
export function fetchSubagentStatus(
  request_id: string,
): Promise<SubagentStatusResponse> {
  return apiFetch<SubagentStatusResponse>(
    `/admin/subagents/${encodeURIComponent(request_id)}/status`,
  );
}

/** GET /admin/subagents/{request_id}/events — per-child SSE.
 * Frames carry the same SubagentStatusResponse JSON; close on terminal
 * state. Callers attach `addEventListener("subagent", …)` and own
 * cleanup via `.close()`. */
export function streamSubagentEvents(request_id: string): EventSource {
  return new EventSource(
    `${GATEWAY_BASE_URL}/admin/subagents/${encodeURIComponent(request_id)}/events`,
    { withCredentials: true },
  );
}

/** GET /admin/subagents/events/live — global overview SSE.
 * Frames: `event: subagent\ndata: <SubagentStatus JSON>\n\n`. */
export function streamSubagentsOverview(): EventSource {
  return new EventSource(
    `${GATEWAY_BASE_URL}/admin/subagents/events/live`,
    { withCredentials: true },
  );
}

/** POST /admin/subagents/{request_id}/kill — best-effort cancel. Server
 * returns the post-kill status snapshot so the UI can transition the
 * row to `killed` immediately. */
export function killSubagent(
  request_id: string,
): Promise<SubagentStatusResponse> {
  return apiFetch<SubagentStatusResponse>(
    `/admin/subagents/${encodeURIComponent(request_id)}/kill`,
    { method: "POST", body: {} },
  );
}

// ---------------------------------------------------------------------------
// UI redesign — health + dashboard metrics
// ---------------------------------------------------------------------------

export interface HealthCheck {
  name: string;
  /** Normalised boolean (true iff status === "ok"). Populated by fetchHealth. */
  ok: boolean;
  /** Raw gateway status string ("ok" | "warn" | "unhealthy" | ...). */
  status?: string;
  detail?: string;
  checked_at?: string;
}

interface GatewayHealthCheck {
  name: string;
  status: string;
  detail?: string;
  checked_at?: string;
}

export interface HealthStatus {
  status: "ok" | "healthy" | "degraded" | "warn" | "unhealthy" | string;
  checks?: HealthCheck[];
  version?: string;
}

interface GatewayHealthStatus {
  status: string;
  checks?: GatewayHealthCheck[];
  version?: string;
}

/**
 * GET /health — returns aggregated gateway health.
 *
 * The gateway reports each check as `{ name, status, detail }` where
 * `status` is a string ("ok" / "warn" / "unhealthy" / ...). The admin UI
 * wants a boolean, so we normalise here — `ok` is true iff the raw
 * `status` equals "ok".
 */
export async function fetchHealth(): Promise<HealthStatus> {
  const raw = await apiFetch<GatewayHealthStatus>("/health");
  return {
    status: raw.status,
    version: raw.version,
    checks: (raw.checks ?? []).map((c) => ({
      name: c.name,
      status: c.status,
      ok: c.status === "ok",
      detail: c.detail,
      checked_at: c.checked_at,
    })),
  };
}

// ---------------------------------------------------------------------------
// Feature C (v0.2) — custom providers + per-alias params + embedding
//
// Contract: docs/feature-c contract (see Python/Rust counterparts). All
// requests go through admin auth middleware. 503 from any of these
// endpoints means the gateway is v0.1.x and has not been upgraded yet — the
// UI renders a "backend feature pending" empty state (do not toast).
// ---------------------------------------------------------------------------

export type ProviderKind =
  | "anthropic"
  | "openai"
  | "google"
  | "deepseek"
  | "qwen"
  | "glm"
  | "openai_compatible"
  // Free-form-providers refactor: market LLMs surfaced as named kinds even
  // though they all run through the OpenAI-compatible backend today.
  | "mistral"
  | "cohere"
  | "together"
  | "groq"
  | "replicate"
  | "bedrock"
  | "azure";

/** Loose JSON Schema (draft 2020-12) — enough for the subset we render. */
export type JSONSchema = {
  type?: "string" | "number" | "integer" | "boolean" | "object" | "array";
  title?: string;
  description?: string;
  default?: unknown;
  enum?: unknown[];
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  format?: string;
  properties?: Record<string, JSONSchema>;
  required?: string[];
  additionalProperties?: boolean | JSONSchema;
  items?: JSONSchema;
  // Tolerate other fields without breaking.
  [key: string]: unknown;
};

export type ProviderCapabilities = {
  embedding?: boolean;
  chat?: boolean;
};

export interface ProviderView {
  name: string;
  kind: ProviderKind;
  enabled: boolean;
  base_url: string | null;
  api_key_source: "env" | "value" | "unset";
  api_key_env_name: string | null;
  params: Record<string, unknown>;
  params_schema: JSONSchema;
  capabilities?: ProviderCapabilities;
}

export interface ProviderUpsert {
  name: string;
  kind: ProviderKind;
  enabled?: boolean;
  base_url?: string;
  api_key?: { env: string } | { value: string } | null;
  params?: Record<string, unknown>;
}

export interface ProvidersResponse {
  providers: ProviderView[];
}

export async function fetchProviders(): Promise<ProviderView[]> {
  const res = await apiFetch<ProvidersResponse>("/admin/providers");
  return res.providers ?? [];
}

export async function upsertProvider(
  body: ProviderUpsert,
): Promise<ProviderView> {
  return apiFetch<ProviderView>("/admin/providers", {
    method: "POST",
    body,
  });
}

export async function deleteProvider(name: string): Promise<void> {
  await apiFetch<void>(`/admin/providers/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

/** Server returns 409 with `{ error, references: string[] }` when a
 * provider is still referenced by an alias or by embedding. Surface the list
 * so the UI can explain why the delete was refused. */
export interface ProviderConflict {
  error: string;
  references: string[];
}

export interface AliasView {
  name: string;
  provider: string;
  model: string;
  params: Record<string, unknown>;
  effective_params_schema: JSONSchema;
}

export interface AliasUpsert {
  name: string;
  provider: string;
  model: string;
  params?: Record<string, unknown>;
}

/** Extended /admin/models response — aliases now carry params + the
 * merged schema the UI should render. The legacy (string-map) shape is
 * still served by v0.1 gateways and handled in ModelsPage. */
export interface ModelsResponseV2 {
  default: string;
  providers: ProviderView[];
  aliases: AliasView[];
}

export async function fetchModelsV2(): Promise<ModelsResponseV2> {
  return apiFetch<ModelsResponseV2>("/admin/models");
}

export async function upsertAlias(body: AliasUpsert): Promise<AliasView> {
  return apiFetch<AliasView>("/admin/models/aliases", {
    method: "POST",
    body,
  });
}

export async function deleteAlias(name: string): Promise<void> {
  await apiFetch<void>(
    `/admin/models/aliases/${encodeURIComponent(name)}`,
    { method: "DELETE" },
  );
}

// ---------------------------------------------------------------------------
// Wave 1-D — EvolutionLoop proposal queue
//
// Mirrors the gateway routes in
// rust/crates/corlinman-gateway/src/routes/admin/evolution.rs.
// ---------------------------------------------------------------------------

export type EvolutionRisk = "low" | "medium" | "high";

/**
 * MetricSnapshot — mirrors the Rust `corlinman_auto_rollback::metrics::
 * MetricSnapshot`. Written into `evolution_history.metrics_baseline` at
 * apply time and into `evolution_proposals.baseline_metrics_json` by the
 * ShadowTester. Both surfaces feed the UI's `<MetricsDelta />` viz.
 */
export interface MetricSnapshot {
  target: string;
  /** epoch-ms */
  captured_at_ms: number;
  window_secs: number;
  /** event_kind → count over the window. Stable shape across snapshots. */
  counts: Record<string, number>;
}

export interface EvolutionProposal {
  id: string;
  kind: string;
  target: string;
  diff: string;
  reasoning: string;
  risk: EvolutionRisk;
  status: string;
  /** Serialized `ShadowMetrics` (free-form per-kind shape). Populated on
   * `shadow_done` rows — used by `MetricsDelta` for the post-shadow leg. */
  shadow_metrics?: Record<string, unknown>;
  signal_ids: number[];
  trace_ids: string[];
  /** epoch-ms */
  created_at: number;
  decided_at?: number;
  decided_by?: string;
  applied_at?: number;
  /** W1-A: identifier of the eval run that produced `shadow_metrics`. */
  eval_run_id?: string;
  /** W1-A: pre-shadow baseline `MetricSnapshot` JSON. */
  baseline_metrics_json?: MetricSnapshot;
  /** W1-B: epoch-ms the AutoRollback monitor flipped this row. */
  auto_rollback_at?: number;
  /** W1-B: human-readable threshold-breach reason from the monitor. */
  auto_rollback_reason?: string;
}

/**
 * One row in `GET /admin/evolution/history`. Mirrors `HistoryEntryOut`
 * in `rust/crates/corlinman-gateway/src/routes/admin/evolution.rs`.
 *
 * `metrics_baseline` is the `MetricSnapshot` JSON the W1-B applier wrote
 * at apply time. `shadow_metrics` + `baseline_metrics_json` come from
 * the original proposals row so the UI can render the full lineage of
 * baseline → shadow → post-apply on one card.
 */
export interface HistoryEntry {
  proposal_id: string;
  kind: string;
  target: string;
  risk: EvolutionRisk;
  /** "applied" | "rolled_back". */
  status: string;
  /** epoch-ms */
  applied_at: number;
  /** epoch-ms; null while the proposal is still applied. */
  rolled_back_at: number | null;
  /** Manual-rollback reason from the history table. */
  rollback_reason: string | null;
  /** Auto-rollback breach summary from the proposals row. */
  auto_rollback_reason: string | null;
  metrics_baseline: MetricSnapshot;
  shadow_metrics: Record<string, unknown> | null;
  baseline_metrics_json: MetricSnapshot | null;
  before_sha: string;
  after_sha: string;
  eval_run_id: string | null;
  reasoning: string;
}

export function fetchEvolutionPending(): Promise<EvolutionProposal[]> {
  return apiFetch<EvolutionProposal[]>(
    "/admin/evolution?status=pending&limit=50",
  );
}

export function fetchEvolutionApproved(): Promise<EvolutionProposal[]> {
  return apiFetch<EvolutionProposal[]>(
    "/admin/evolution?status=approved&limit=50",
  );
}

export function fetchEvolutionHistory(): Promise<HistoryEntry[]> {
  return apiFetch<HistoryEntry[]>("/admin/evolution/history?limit=50");
}

/** POST /admin/evolution/:id/apply — flips approved→applied and runs the
 * EvolutionApplier. Used by the Approved tab; mirrors the existing
 * approve/deny mutations. */
export interface EvolutionApplyResult {
  id: string;
  status: string;
  history_id?: number;
}

export function applyEvolutionProposal(
  id: string,
): Promise<EvolutionApplyResult> {
  return apiFetch<EvolutionApplyResult>(
    `/admin/evolution/${encodeURIComponent(id)}/apply`,
    { method: "POST" },
  );
}

export interface EvolutionDecideResult {
  id: string;
  status: string;
  decided_at?: number;
  decided_by?: string;
}

export function approveEvolutionProposal(
  id: string,
  decided_by: string,
): Promise<EvolutionDecideResult> {
  return apiFetch<EvolutionDecideResult>(
    `/admin/evolution/${encodeURIComponent(id)}/approve`,
    {
      method: "POST",
      body: { decided_by },
    },
  );
}

export function denyEvolutionProposal(
  id: string,
  decided_by: string,
  reason?: string,
): Promise<EvolutionDecideResult> {
  return apiFetch<EvolutionDecideResult>(
    `/admin/evolution/${encodeURIComponent(id)}/deny`,
    {
      method: "POST",
      body: { decided_by, reason },
    },
  );
}

// ---------------------------------------------------------------------------
// Wave 1-C — weekly EvolutionProposal budget
//
// Mirrors GET /admin/evolution/budget on the gateway. `per_kind` may be empty
// when no kind-level caps are configured; `enabled` is false by default until
// the operator opts the gate in.
// ---------------------------------------------------------------------------

export interface BudgetSlot {
  limit: number;
  used: number;
  remaining: number;
}

export interface BudgetPerKindEntry extends BudgetSlot {
  kind: string;
}

export interface BudgetSnapshot {
  enabled: boolean;
  window_start_ms: number;
  window_end_ms: number;
  weekly_total: BudgetSlot;
  per_kind: BudgetPerKindEntry[];
}

export function fetchBudget(): Promise<BudgetSnapshot> {
  return apiFetch<BudgetSnapshot>("/admin/evolution/budget");
}


// ---------------------------------------------------------------------------
// Wave 2.3 — Credentials manager (EnvPage-style provider grouping)
//
// Mirrors `/admin/credentials*` on the gateway. Reads/writes string fields
// inside `[providers.<name>]` blocks in config.toml. Plaintext values are
// NEVER returned from the server — `preview` is a "…last4" tail when the
// stored value is a literal, otherwise null. `env_ref` surfaces the
// conventional env-var name (or the actual `{ env = "X" }` override the
// operator wrote, if any).
// ---------------------------------------------------------------------------

export interface CredentialField {
  key: string;
  set: boolean;
  preview: string | null;
  env_ref: string | null;
}

export interface CredentialProvider {
  name: string;
  kind: string;
  enabled: boolean;
  fields: CredentialField[];
}

export interface CredentialsListResponse {
  providers: CredentialProvider[];
}

export function listCredentials(): Promise<CredentialsListResponse> {
  return apiFetch<CredentialsListResponse>("/admin/credentials");
}

export function setCredential(
  provider: string,
  key: string,
  value: string,
): Promise<{ status: string }> {
  return apiFetch<{ status: string }>(
    `/admin/credentials/${encodeURIComponent(provider)}/${encodeURIComponent(key)}`,
    { method: "PUT", body: { value } },
  );
}

export function deleteCredential(
  provider: string,
  key: string,
): Promise<void> {
  return apiFetch<void>(
    `/admin/credentials/${encodeURIComponent(provider)}/${encodeURIComponent(key)}`,
    { method: "DELETE" },
  );
}

export function setProviderEnabled(
  provider: string,
  enabled: boolean,
): Promise<{ status: string }> {
  return apiFetch<{ status: string }>(
    `/admin/credentials/${encodeURIComponent(provider)}/enable`,
    { method: "POST", body: { enabled } },
  );
}

// ---------------------------------------------------------------------------
// Onboard finalize-skip — bootstrap with the mock provider.
// (The newapi-specific endpoints + per-channel probe/list/finalize calls
// were removed in the 2026-05 provider-auth reshape; provider setup now
// lives under /admin/credentials, /admin/providers, /admin/oauth.)
// ---------------------------------------------------------------------------

/**
 * `POST /admin/onboard/finalize-skip` (Wave 2.1 + 2.2).
 *
 * Idempotent shortcut for "I don't have a real LLM yet — bootstrap me with
 * the mock provider so I can poke around the console". Writes
 * `[providers.mock] enabled = true` and `[models].default = "mock"` to
 * config.toml. Returns `{status:"ok",mode:"mock"}` on success.
 */
export function finalizeSkipOnboard(): Promise<{
  status: string;
  mode: string;
}> {
  return apiFetch("/admin/onboard/finalize-skip", {
    method: "POST",
  });
}

// ---------------------------------------------------------------------------
// First-run wizard finalize endpoints (Agent B contract — PLAN_FIRST_RUN_WIZARD.md)
//
// The wizard chains six sequential steps. Steps 2–5 each call exactly one of
// the endpoints below. Backend definitions live in:
//   python/.../gateway/routes_admin_b/onboard.py
//
// All endpoints require a valid admin session cookie; we simply forward the
// already-set cookie via the shared `apiFetch` helper.
// ---------------------------------------------------------------------------

/**
 * `POST /admin/onboard/finalize-account` (B1).
 *
 * Wizard Step 2: rotate the default admin username. Body carries only the
 * new username — the gateway trusts the authed session for the password.
 *
 * Errors:
 *   - 409 `username_unchanged` if the candidate equals the current username.
 *   - 422 `invalid_username` on malformed input (lowercase + alnum + `_-`).
 */
export interface OnboardFinalizeAccountResponse {
  status: string;
  username: string;
}

export function finalizeOnboardAccount(
  new_username: string,
): Promise<OnboardFinalizeAccountResponse> {
  return apiFetch<OnboardFinalizeAccountResponse>(
    "/admin/onboard/finalize-account",
    { method: "POST", body: { new_username } },
  );
}

/**
 * `POST /admin/onboard/finalize-password` (B2).
 *
 * Wizard Step 3: rotate the default admin password. Thin wrapper over the
 * existing `auth.change_password` service — but additionally clears the
 * `must_change_password` seed flag on success.
 *
 * Errors:
 *   - 401 `invalid_old_password`
 *   - 422 `weak_password`
 */
export interface OnboardFinalizePasswordResponse {
  status: string;
  must_change_password: boolean;
}

export function finalizeOnboardPassword(
  old_password: string,
  new_password: string,
): Promise<OnboardFinalizePasswordResponse> {
  return apiFetch<OnboardFinalizePasswordResponse>(
    "/admin/onboard/finalize-password",
    { method: "POST", body: { old_password, new_password } },
  );
}

/**
 * `POST /admin/onboard/finalize-persona` (B3).
 *
 * Wizard Step 4: pick how to seed the operator's primary persona.
 *   - "skip":    do nothing.
 *   - "default": ensure built-in `grantley` persona exists and is active.
 *   - "custom":  response contains `{ redirect: "/persona" }`; the UI is
 *                expected to defer the navigation until the rest of the
 *                wizard has finished.
 */
export type OnboardPersonaChoice = "skip" | "default" | "custom";

export interface OnboardFinalizePersonaResponse {
  status: string;
  choice: OnboardPersonaChoice;
  redirect?: string;
}

export function finalizeOnboardPersona(
  choice: OnboardPersonaChoice,
): Promise<OnboardFinalizePersonaResponse> {
  return apiFetch<OnboardFinalizePersonaResponse>(
    "/admin/onboard/finalize-persona",
    { method: "POST", body: { choice } },
  );
}

/**
 * `POST /admin/onboard/finalize-image-provider` (B4).
 *
 * Wizard Step 5: choose the image-generation backend.
 *   - `{ choice: "skip" }`
 *   - `{ choice: "reuse", provider_name }` — probe current provider; on miss
 *     the server replies 409 with `{ supported: false, hint }`.
 *   - `{ choice: "separate", spec }` — register a new provider that
 *     advertises `image_capable = true`.
 *
 * Returns `{ status, image_provider }` on success.
 */
export type OnboardImageChoice = "skip" | "reuse" | "separate";

/**
 * Lightweight provider-spec payload accepted by the "separate" branch of the
 * image-provider step. Mirrors a subset of {@link ProviderUpsert} plus the
 * extra image-only knobs (`image_capable`, `image_model`) introduced by
 * Agent C.
 */
export interface OnboardImageProviderSpec {
  name: string;
  base_url: string;
  api_key: string;
  image_model?: string;
  /** Forwarded for forward-compat; the server fills this in if omitted. */
  image_capable?: boolean;
  /**
   * The wizard always targets OpenAI-compatible endpoints for the
   * "separate" branch; the field is still threaded in case the backend
   * later supports more shapes.
   */
  kind?: ProviderKind;
}

export type OnboardImageProviderBody =
  | { choice: "skip" }
  | { choice: "reuse"; provider_name: string }
  | { choice: "separate"; spec: OnboardImageProviderSpec };

export interface OnboardFinalizeImageProviderResponse {
  status: string;
  image_provider: string;
}

/** Returned in the body of 409 when the probe says the provider can't draw. */
export interface OnboardImageNotSupported {
  supported: false;
  hint: string;
}

export function finalizeOnboardImageProvider(
  body: OnboardImageProviderBody,
): Promise<OnboardFinalizeImageProviderResponse> {
  return apiFetch<OnboardFinalizeImageProviderResponse>(
    "/admin/onboard/finalize-image-provider",
    { method: "POST", body },
  );
}

// --- profiles (W3.1 + W3.2) -------------------------------------------------
//
// CRUD over `/admin/profiles`. The wire shape is defined by
// ``routes_admin_a/profiles.py`` (FastAPI ``ProfileOut``).
//
// Server quirk: ``GET /admin/profiles`` returns a *bare list* (FastAPI
// ``response_model=list[ProfileOut]``), not the ``{profiles: [...]}``
// envelope you'd get from a more elaborate paginated endpoint. We wrap
// it client-side so callers don't have to know that detail.

/** Wire shape of one profile row. Mirrors backend ``ProfileOut``. */
export interface Profile {
  slug: string;
  display_name: string;
  parent_slug: string | null;
  description: string | null;
  /** ISO-8601 UTC with a ``Z`` suffix. */
  created_at: string;
}

export interface CreateProfileBody {
  slug: string;
  display_name?: string;
  /** Slug of a parent profile to clone SOUL/MEMORY/USER/skills from. */
  clone_from?: string;
  description?: string;
}

export interface UpdateProfileBody {
  display_name?: string;
  description?: string;
}

/** List every profile. */
export async function listProfiles(): Promise<{ profiles: Profile[] }> {
  // Backend returns a bare list — wrap into ``{profiles}`` envelope so
  // the rest of the app can treat the response uniformly.
  const profiles = await apiFetch<Profile[]>("/admin/profiles");
  return { profiles };
}

/** Create one profile (optionally cloning a parent). */
export function createProfile(body: CreateProfileBody): Promise<Profile> {
  return apiFetch<Profile>("/admin/profiles", {
    method: "POST",
    body,
  });
}

/** Fetch one profile by slug. */
export function getProfile(slug: string): Promise<Profile> {
  return apiFetch<Profile>(`/admin/profiles/${encodeURIComponent(slug)}`);
}

/** Partial update — pass only the fields you want to change. */
export function updateProfile(
  slug: string,
  patch: UpdateProfileBody,
): Promise<Profile> {
  return apiFetch<Profile>(`/admin/profiles/${encodeURIComponent(slug)}`, {
    method: "PATCH",
    body: patch,
  });
}

/** Delete one profile. Throws 409 ``profile_protected`` for ``default``. */
export function deleteProfile(slug: string): Promise<void> {
  return apiFetch<void>(`/admin/profiles/${encodeURIComponent(slug)}`, {
    method: "DELETE",
  });
}

/** Read the persona markdown. Empty string when the file is missing. */
export function getProfileSoul(
  slug: string,
): Promise<{ content: string }> {
  return apiFetch<{ content: string }>(
    `/admin/profiles/${encodeURIComponent(slug)}/soul`,
  );
}

/** Atomic-write the persona markdown. */
export function setProfileSoul(
  slug: string,
  content: string,
): Promise<{ content: string }> {
  return apiFetch<{ content: string }>(
    `/admin/profiles/${encodeURIComponent(slug)}/soul`,
    {
      method: "PUT",
      body: { content },
    },
  );
}

// ---------------------------------------------------------------------------
// Wave 4.6 — Curator UI surface
//
// Mirrors `gateway/routes_admin_b/curator.py`. Seven endpoints behind
// `/admin/curator/*` drive the new evolution / curator surface: list
// profiles + thresholds, preview / run the deterministic lifecycle pass,
// pause / resume, edit thresholds, list skills with state + origin
// badges, pin / unpin. The shapes below mirror the pydantic models
// exactly so the wire stays self-describing.
// ---------------------------------------------------------------------------

export type CuratorSkillState = "active" | "stale" | "archived";
export type CuratorSkillOrigin =
  | "bundled"
  | "user-requested"
  | "agent-created";

/** Wire shape of one transition in a curator report. */
export interface CuratorTransition {
  skill_name: string;
  from_state: string;
  to_state: string;
  /** "stale_threshold" | "archive_threshold" | "reactivated" */
  reason: string;
  days_idle: number;
}

/** Result of a preview / real run — same shape, the dry_run intent is
 * baked into the route, not into the response. */
export interface CuratorReport {
  profile_slug: string;
  /** ISO-8601 UTC */
  started_at: string;
  finished_at: string;
  duration_ms: number;
  transitions: CuratorTransition[];
  marked_stale: number;
  archived: number;
  reactivated: number;
  checked: number;
  skipped: number;
}

export interface ProfileSkillCounts {
  active: number;
  stale: number;
  archived: number;
  total: number;
}

export interface ProfileOriginCounts {
  bundled: number;
  "user-requested": number;
  "agent-created": number;
}

export interface ProfileCuratorState {
  slug: string;
  paused: boolean;
  interval_hours: number;
  stale_after_days: number;
  archive_after_days: number;
  last_review_at: string | null;
  last_review_summary: string | null;
  run_count: number;
  skill_counts: ProfileSkillCounts;
  origin_counts: ProfileOriginCounts;
}

export interface CuratorProfilesResponse {
  profiles: ProfileCuratorState[];
}

/** Slim post-update state returned by /pause + /thresholds. */
export interface CuratorStateUpdate {
  slug: string;
  paused: boolean;
  interval_hours: number;
  stale_after_days: number;
  archive_after_days: number;
  last_review_at: string | null;
  last_review_summary: string | null;
  run_count: number;
}

export interface SkillSummary {
  name: string;
  description: string;
  version: string;
  state: CuratorSkillState;
  origin: CuratorSkillOrigin;
  pinned: boolean;
  use_count: number;
  last_used_at: string | null;
  created_at: string | null;
}

export interface SkillsListResponse {
  skills: SkillSummary[];
}

export interface SkillFilters {
  state?: CuratorSkillState;
  origin?: CuratorSkillOrigin;
  search?: string;
}

export interface CuratorThresholdsPatch {
  interval_hours?: number;
  stale_after_days?: number;
  archive_after_days?: number;
}

/** GET /admin/curator/profiles → list every profile + thresholds + counts. */
export function listCuratorProfiles(): Promise<CuratorProfilesResponse> {
  return apiFetch<CuratorProfilesResponse>("/admin/curator/profiles");
}

/** POST /admin/curator/{slug}/preview → dry-run pass. */
export function previewCuratorRun(slug: string): Promise<CuratorReport> {
  return apiFetch<CuratorReport>(
    `/admin/curator/${encodeURIComponent(slug)}/preview`,
    { method: "POST", body: {} },
  );
}

/** POST /admin/curator/{slug}/run → real run, persists transitions. */
export function runCuratorNow(slug: string): Promise<CuratorReport> {
  return apiFetch<CuratorReport>(
    `/admin/curator/${encodeURIComponent(slug)}/run`,
    { method: "POST", body: {} },
  );
}

/** POST /admin/curator/{slug}/pause → flip the per-profile pause flag. */
export function pauseCurator(
  slug: string,
  paused: boolean,
): Promise<CuratorStateUpdate> {
  return apiFetch<CuratorStateUpdate>(
    `/admin/curator/${encodeURIComponent(slug)}/pause`,
    { method: "POST", body: { paused } },
  );
}

/** PATCH /admin/curator/{slug}/thresholds → tune any subset of the three
 * thresholds. The backend enforces `archive > stale` and `interval >= 1`. */
export function updateCuratorThresholds(
  slug: string,
  patch: CuratorThresholdsPatch,
): Promise<CuratorStateUpdate> {
  return apiFetch<CuratorStateUpdate>(
    `/admin/curator/${encodeURIComponent(slug)}/thresholds`,
    { method: "PATCH", body: patch },
  );
}

/** GET /admin/curator/{slug}/skills → filterable skill list. */
export function listProfileSkills(
  slug: string,
  filters: SkillFilters = {},
): Promise<SkillsListResponse> {
  const params = new URLSearchParams();
  if (filters.state) params.set("state", filters.state);
  if (filters.origin) params.set("origin", filters.origin);
  if (filters.search) params.set("search", filters.search);
  const qs = params.toString();
  const path = `/admin/curator/${encodeURIComponent(slug)}/skills${
    qs ? `?${qs}` : ""
  }`;
  return apiFetch<SkillsListResponse>(path);
}

/** POST /admin/curator/{slug}/skills/{name}/pin → toggle Skill.pinned. */
export function pinSkill(
  slug: string,
  name: string,
  pinned: boolean,
): Promise<SkillSummary> {
  return apiFetch<SkillSummary>(
    `/admin/curator/${encodeURIComponent(slug)}/skills/${encodeURIComponent(
      name,
    )}/pin`,
    { method: "POST", body: { pinned } },
  );
}

// === W-A2 oauth (do not edit other blocks) ===
// Frontend client for the W-A1 OAuth surface
// (`gateway/routes_admin_b/oauth.py`). Anthropic PKCE today; Codex /
// Gemini / xAI come in a later wave. Keep this block self-contained so
// W-D2 and W-B2 can append independently. Tokens never leave the
// gateway — these helpers only ferry the PKCE handshake state
// (session_id, paste code, paste state) plus status/disconnect actions.

/**
 * One row in `GET /admin/oauth/status`. `source` describes where the
 * gateway currently resolves a credential from for this provider:
 *   - "pkce"        — interactive PKCE login token in
 *                     `<data_dir>/.oauth/<id>.json`
 *   - "claude-code" — imported from `~/.claude/.credentials.json`
 *   - "env"         — env-var override
 *   - "api-key"     — plain api_key in providers TOML / credentials store
 *   - "none"        — nothing configured
 */
export type OAuthSource =
  | "pkce"
  | "claude-code"
  | "env"
  | "api-key"
  | "external-cli"
  | "none";

export interface OAuthProviderStatus {
  id: string;
  source: OAuthSource;
  expires_in_seconds: number | null;
  username: string | null;
}

export interface OAuthStatusResponse {
  providers: OAuthProviderStatus[];
}

export interface OAuthStartResponse {
  session_id: string;
  auth_url: string;
  expires_at_ms: number;
}

export interface OAuthSubmitRequest {
  session_id: string;
  code: string;
  state: string;
}

export interface OAuthSubmitResponse {
  ok: true;
  expires_at_ms: number;
}

export interface OAuthRefreshResponse {
  expires_at_ms: number;
}

export interface ClaudeCodeImportResponse {
  imported: true;
  expires_at_ms: number;
}

/**
 * GET /admin/oauth/status — every supported OAuth provider with the
 * gateway-resolved credential source + token expiry (if any).
 */
export function getOAuthStatus(
  opts: { signal?: AbortSignal } = {},
): Promise<OAuthStatusResponse> {
  return apiFetch<OAuthStatusResponse>("/admin/oauth/status", {
    signal: opts.signal,
  });
}

/**
 * POST /admin/oauth/anthropic/start — open a PKCE session. The
 * returned `auth_url` is what the user opens in a new tab; the
 * `session_id` rides through to the submit call so the gateway can
 * pair the paste-back with its stored code_verifier.
 */
export function startAnthropicOAuth(
  opts: { signal?: AbortSignal } = {},
): Promise<OAuthStartResponse> {
  return apiFetch<OAuthStartResponse>("/admin/oauth/anthropic/start", {
    method: "POST",
    body: {},
    signal: opts.signal,
  });
}

/**
 * POST /admin/oauth/anthropic/submit — paste-back step of PKCE. The
 * caller must trim whitespace around `code` and `state` itself.
 */
export function submitAnthropicOAuthCode(
  req: OAuthSubmitRequest,
  opts: { signal?: AbortSignal } = {},
): Promise<OAuthSubmitResponse> {
  return apiFetch<OAuthSubmitResponse>("/admin/oauth/anthropic/submit", {
    method: "POST",
    body: req,
    signal: opts.signal,
  });
}

/** POST /admin/oauth/anthropic/refresh — manual refresh trigger. */
export function refreshAnthropicOAuth(
  opts: { signal?: AbortSignal } = {},
): Promise<OAuthRefreshResponse> {
  return apiFetch<OAuthRefreshResponse>("/admin/oauth/anthropic/refresh", {
    method: "POST",
    body: {},
    signal: opts.signal,
  });
}

/** DELETE /admin/oauth/anthropic — wipe the stored OAuth token file. */
export function disconnectAnthropicOAuth(
  opts: { signal?: AbortSignal } = {},
): Promise<void> {
  return apiFetch<void>("/admin/oauth/anthropic", {
    method: "DELETE",
    signal: opts.signal,
  });
}

/**
 * POST /admin/oauth/claude-code/import — one-shot import of
 * `~/.claude/.credentials.json` from disk. 404 if the file isn't
 * present; callers should surface that as "not detected".
 */
export function importClaudeCodeCredentials(
  opts: { signal?: AbortSignal } = {},
): Promise<ClaudeCodeImportResponse> {
  return apiFetch<ClaudeCodeImportResponse>(
    "/admin/oauth/claude-code/import",
    { method: "POST", body: {}, signal: opts.signal },
  );
}

/**
 * POST /admin/oauth/claude-code/launch — spawn `claude auth login` on
 * the gateway host, return its OAuth URL + a session id. The CLI
 * remains parked on stdin until either `submitClaudeCodeLogin` pushes a
 * code or `cancelClaudeCodeLogin` kills it.
 */
export interface ClaudeCodeLoginLaunchResponse {
  session_id: string;
  auth_url: string;
}

export function launchClaudeCodeLogin(
  opts: { signal?: AbortSignal } = {},
): Promise<ClaudeCodeLoginLaunchResponse> {
  return apiFetch<ClaudeCodeLoginLaunchResponse>(
    "/admin/oauth/claude-code/launch",
    { method: "POST", body: {}, signal: opts.signal },
  );
}

/**
 * POST /admin/oauth/claude-code/submit — paste the code back to the
 * parked subprocess. On clean exit the gateway re-imports the freshly
 * written ~/.claude/.credentials.json into the anthropic slot.
 */
export function submitClaudeCodeLogin(
  body: { session_id: string; code: string },
  opts: { signal?: AbortSignal } = {},
): Promise<ClaudeCodeImportResponse> {
  return apiFetch<ClaudeCodeImportResponse>(
    "/admin/oauth/claude-code/submit",
    { method: "POST", body, signal: opts.signal },
  );
}

/**
 * POST /admin/oauth/claude-code/cancel — kill an abandoned login
 * subprocess. Idempotent; 204 even if the session was already gone.
 */
export function cancelClaudeCodeLogin(
  body: { session_id: string },
  opts: { signal?: AbortSignal } = {},
): Promise<void> {
  return apiFetch<void>("/admin/oauth/claude-code/cancel", {
    method: "POST",
    body,
    signal: opts.signal,
  });
}
// === end W-A2 ===

// === W-B2 custom provider (do not edit other blocks) ===
//
// Mirrors `gateway/routes_admin_b/providers.py` custom-provider CRUD that
// landed in W-B1. These wrappers drive the new "Custom providers" section
// on /admin/providers. Slug regex (server-enforced):
// `^[a-z0-9][a-z0-9_-]{0,31}$`. Built-in slot collisions return 409.

/** GET /admin/providers/kinds → every `ProviderKind` enum value (alphabetised). */
export interface ProviderKindsResponse {
  kinds: string[];
}

/** One row in `GET /admin/providers/custom`. `params.custom = true` is the
 * UI marker that distinguishes custom-registered providers from the
 * built-in `[providers.*]` blocks. */
export interface CustomProviderRow {
  slug: string;
  kind: string;
  base_url: string | null;
  has_api_key: boolean;
  params: Record<string, unknown>;
}

export interface CustomProvidersResponse {
  providers: CustomProviderRow[];
}

/** Body for POST /admin/providers/custom. `api_key` is plaintext when
 * present (the server writes a literal into config); pass `null` (or omit
 * via `undefined`) to leave the slot unset. */
export interface CustomProviderCreateBody {
  slug: string;
  kind: string;
  base_url?: string | null;
  api_key?: { value: string } | null;
  params?: Record<string, unknown>;
}

/** Body for PATCH /admin/providers/custom/{slug}. All fields optional. */
export interface CustomProviderPatchBody {
  kind?: string;
  base_url?: string | null;
  api_key?: { value: string } | null;
  params?: Record<string, unknown>;
}

/** GET /admin/providers/kinds */
export async function listProviderKinds(): Promise<string[]> {
  const res = await apiFetch<ProviderKindsResponse>("/admin/providers/kinds");
  return res.kinds ?? [];
}

/** GET /admin/providers/custom */
export async function listCustomProviders(): Promise<CustomProviderRow[]> {
  const res = await apiFetch<CustomProvidersResponse>(
    "/admin/providers/custom",
  );
  return res.providers ?? [];
}

/** POST /admin/providers/custom → 201 */
export function createCustomProvider(
  body: CustomProviderCreateBody,
): Promise<CustomProviderRow> {
  return apiFetch<CustomProviderRow>("/admin/providers/custom", {
    method: "POST",
    body,
  });
}

/** PATCH /admin/providers/custom/{slug} → 200 */
export function patchCustomProvider(
  slug: string,
  body: CustomProviderPatchBody,
): Promise<CustomProviderRow> {
  return apiFetch<CustomProviderRow>(
    `/admin/providers/custom/${encodeURIComponent(slug)}`,
    { method: "PATCH", body },
  );
}

/** DELETE /admin/providers/custom/{slug} → 204 */
export function deleteCustomProvider(slug: string): Promise<void> {
  return apiFetch<void>(
    `/admin/providers/custom/${encodeURIComponent(slug)}`,
    { method: "DELETE" },
  );
}

/** Slug validator mirrored from the backend: `^[a-z0-9][a-z0-9_-]{0,31}$`. */
export const CUSTOM_PROVIDER_SLUG_RE = /^[a-z0-9][a-z0-9_-]{0,31}$/;
// === end W-B2 ===

// === W-D2 agent model binding (do not edit other blocks) ===
//
// Surface for the per-agent model+provider binding stored in
// `<data_dir>/agents/<name>.yaml`. Pairs with the Python admin route
// `routes_admin_b/agents.py` which mounts under
// `/admin/agents/bindings*` (the bare `/admin/agents` path is owned by
// `routes_admin_a` for the Monaco-editor file scan; we deliberately
// pick a distinct suffix here so both surfaces stay reachable).

/** One row in `GET /admin/agents/bindings`. */
export interface AgentBinding {
  /** Filename stem — matches the yaml's `name:` field. */
  name: string;
  /** Operator-facing summary; rendered as a column tooltip. */
  description: string;
  /** Bound upstream model id (or alias). Null = inherit global default. */
  model: string | null;
  /** Pinned provider slot name. Null = let the resolver pick. */
  provider: string | null;
  /** Whether chat should show reasoning/tool/subagent trajectory. */
  show_action_trace: boolean;
}

export interface AgentBindingsResponse {
  agents: AgentBinding[];
}

/** Body for `PATCH /admin/agents/{name}/binding`. Either field nulled
 * (or empty-string) means "clear the slot, revert to fallback chain". */
export interface AgentBindingPatch {
  model: string | null;
  provider: string | null;
  show_action_trace: boolean;
}

/** GET — list every agent's parsed model+provider binding. */
export async function listAgentBindings(): Promise<AgentBindingsResponse> {
  return apiFetch<AgentBindingsResponse>("/admin/agent-bindings");
}

/** PATCH — overwrite an agent's model+provider binding. The endpoint
 * round-trips the yaml file, preserving unrecognised top-level keys
 * and field order. */
export async function setAgentModelBinding(
  name: string,
  patch: AgentBindingPatch,
): Promise<{
  status: string;
  name: string;
  model: string | null;
  provider: string | null;
  show_action_trace: boolean;
}> {
  return apiFetch(`/admin/agent-bindings/${encodeURIComponent(name)}`, {
    method: "PATCH",
    body: patch,
  });
}
// === end W-D2 ===

// === W-A3 oauth (do not edit other blocks) ===
//
// W-A3 fans out the W-A1 / W-A2 OAuth surface to three more providers:
//
//   * Codex   — read-only; the gateway shells out to `codex login` and
//               reads the resulting `~/.codex/auth.json`. No interactive
//               PKCE handshake is offered; the tile is detection-only.
//   * Gemini  — read-only; same pattern as Codex via `gemini auth`.
//   * xAI     — full PKCE flow, same paste-back UX as Anthropic.
//
// The umbrella `getOAuthStatus()` lives in the W-A2 block above and is
// reused unchanged: its response now lists rows for `anthropic`, `codex`,
// `gemini`, and `xai`. We only expose per-provider helpers here.
//
// Wire shape additions (relative to W-A2's OAuthProviderStatus):
//   - `account_id` is an optional bag carried through from the external
//     CLI's stored credentials (Codex / Gemini may surface it; xAI may
//     not). It's tagged `?` so the W-A2 callsite that didn't read it
//     keeps type-checking.
//   - The detection-only endpoints return `expires_at_ms` (epoch-ms)
//     rather than the umbrella's `expires_in_seconds` because they read
//     a static file on disk and we let the UI compute the delta at
//     render time. Both shapes are kept distinct on the wire so the
//     calling tile picks the right one.

/** Source tag added by W-A3 — emitted alongside the W-A2 values. */
export type OAuthSourceExtended = OAuthSource | "external-cli";

/**
 * Detection-only status returned by `GET /admin/oauth/codex/status` and
 * `GET /admin/oauth/gemini/status`. `detected = false` means the CLI
 * hasn't been logged into yet; the UI hints the operator to run the
 * appropriate `<cli> login` command.
 */
export interface OAuthDetectStatus {
  detected: boolean;
  account_id: string | null;
  expires_at_ms: number | null;
}

/** GET /admin/oauth/codex/status — detection-only, no login flow. */
export function getCodexStatus(
  opts: { signal?: AbortSignal } = {},
): Promise<OAuthDetectStatus> {
  return apiFetch<OAuthDetectStatus>("/admin/oauth/codex/status", {
    signal: opts.signal,
  });
}

/** GET /admin/oauth/gemini/status — detection-only, no login flow. */
export function getGeminiStatus(
  opts: { signal?: AbortSignal } = {},
): Promise<OAuthDetectStatus> {
  return apiFetch<OAuthDetectStatus>("/admin/oauth/gemini/status", {
    signal: opts.signal,
  });
}

/**
 * POST /admin/oauth/xai/start — open a PKCE session for xAI. The
 * returned `auth_url` is what the user opens in a new tab; the
 * `session_id` is paired with the paste-back submit call so the
 * gateway can match its stored code_verifier.
 *
 * Wire shape mirrors `OAuthStartResponse` exactly so the modal can
 * stay provider-agnostic.
 */
export function startXaiOAuth(
  opts: { signal?: AbortSignal } = {},
): Promise<OAuthStartResponse> {
  return apiFetch<OAuthStartResponse>("/admin/oauth/xai/start", {
    method: "POST",
    body: {},
    signal: opts.signal,
  });
}

/**
 * POST /admin/oauth/xai/submit — paste-back step of PKCE. Same shape
 * as the Anthropic submit; caller trims whitespace itself.
 */
export function submitXaiOAuthCode(
  req: OAuthSubmitRequest,
  opts: { signal?: AbortSignal } = {},
): Promise<OAuthSubmitResponse> {
  return apiFetch<OAuthSubmitResponse>("/admin/oauth/xai/submit", {
    method: "POST",
    body: req,
    signal: opts.signal,
  });
}

/** POST /admin/oauth/xai/refresh — manual refresh trigger. */
export function refreshXaiOAuth(
  opts: { signal?: AbortSignal } = {},
): Promise<OAuthRefreshResponse> {
  return apiFetch<OAuthRefreshResponse>("/admin/oauth/xai/refresh", {
    method: "POST",
    body: {},
    signal: opts.signal,
  });
}

/** DELETE /admin/oauth/xai — wipe the stored xAI OAuth token file. */
export function disconnectXaiOAuth(
  opts: { signal?: AbortSignal } = {},
): Promise<void> {
  return apiFetch<void>("/admin/oauth/xai", {
    method: "DELETE",
    signal: opts.signal,
  });
}
// === end W-A3 ===

// === W-A4 codex + gemini PKCE ===
// Mirror xAI/anthropic PKCE shape so OAuthLoginModal can pick these up
// by adding "codex" / "gemini" to its provider table.

export function startCodexOAuth(
  opts: { signal?: AbortSignal } = {},
): Promise<OAuthStartResponse> {
  return apiFetch<OAuthStartResponse>("/admin/oauth/codex/start", {
    method: "POST",
    body: {},
    signal: opts.signal,
  });
}

export function submitCodexOAuthCode(
  req: OAuthSubmitRequest,
  opts: { signal?: AbortSignal } = {},
): Promise<OAuthSubmitResponse> {
  return apiFetch<OAuthSubmitResponse>("/admin/oauth/codex/submit", {
    method: "POST",
    body: req,
    signal: opts.signal,
  });
}

export function refreshCodexOAuth(
  opts: { signal?: AbortSignal } = {},
): Promise<OAuthRefreshResponse> {
  return apiFetch<OAuthRefreshResponse>("/admin/oauth/codex/refresh", {
    method: "POST",
    body: {},
    signal: opts.signal,
  });
}

export function disconnectCodexOAuth(
  opts: { signal?: AbortSignal } = {},
): Promise<void> {
  return apiFetch<void>("/admin/oauth/codex", {
    method: "DELETE",
    signal: opts.signal,
  });
}

export function startGeminiOAuth(
  opts: { signal?: AbortSignal } = {},
): Promise<OAuthStartResponse> {
  return apiFetch<OAuthStartResponse>("/admin/oauth/gemini/start", {
    method: "POST",
    body: {},
    signal: opts.signal,
  });
}

export function submitGeminiOAuthCode(
  req: OAuthSubmitRequest,
  opts: { signal?: AbortSignal } = {},
): Promise<OAuthSubmitResponse> {
  return apiFetch<OAuthSubmitResponse>("/admin/oauth/gemini/submit", {
    method: "POST",
    body: req,
    signal: opts.signal,
  });
}

export function refreshGeminiOAuth(
  opts: { signal?: AbortSignal } = {},
): Promise<OAuthRefreshResponse> {
  return apiFetch<OAuthRefreshResponse>("/admin/oauth/gemini/refresh", {
    method: "POST",
    body: {},
    signal: opts.signal,
  });
}

export function disconnectGeminiOAuth(
  opts: { signal?: AbortSignal } = {},
): Promise<void> {
  return apiFetch<void>("/admin/oauth/gemini", {
    method: "DELETE",
    signal: opts.signal,
  });
}
// === end W-A4 ===

// === W2.1 — Session observability (replay + cost summary) =================
//
// Live SSE stream is opened via `lib/sessions/event-stream.ts`; these two
// helpers cover the JSON cursor-replay and the cost-summary endpoints.

export interface TurnEventsPage {
  events: _W21LiveEvent[];
  next_cursor: number | null;
}

/**
 * `GET /admin/sessions/{key}/turns/{turn_id}/events?after_sequence=N&limit=500`
 *
 * Cursor-paginated replay for a single turn. Used for backfill on
 * reconnect / `?turn_id=` deep-links.
 */
export async function loadTurnEvents(
  sessionKey: string,
  turnId: string,
  opts: { afterSequence?: number; limit?: number; signal?: AbortSignal } = {},
): Promise<TurnEventsPage> {
  const params = new URLSearchParams();
  if (opts.afterSequence !== undefined) {
    params.set("after_sequence", String(opts.afterSequence));
  }
  if (opts.limit !== undefined) params.set("limit", String(opts.limit));
  const qs = params.toString();
  const path = `/admin/sessions/${encodeURIComponent(sessionKey)}/turns/${encodeURIComponent(
    turnId,
  )}/events${qs ? `?${qs}` : ""}`;
  return apiFetch<TurnEventsPage>(path, { signal: opts.signal });
}

export interface SessionCostSummary {
  turn_count: number;
  total_elapsed_ms: number;
  total_cost_usd: number;
  total_input_tokens?: number;
  total_output_tokens?: number;
}

/** `GET /admin/sessions/{key}/cost` */
export async function loadSessionCost(
  sessionKey: string,
  opts: { signal?: AbortSignal } = {},
): Promise<SessionCostSummary> {
  return apiFetch<SessionCostSummary>(
    `/admin/sessions/${encodeURIComponent(sessionKey)}/cost`,
    { signal: opts.signal },
  );
}

/**
 * Re-export of the SSE opener so callers can `import { streamSessionEvents }
 * from "@/lib/api"` alongside the JSON helpers above. Thin alias so we can
 * swap the underlying transport without rippling through the components.
 */
export { openLiveEventStream as streamSessionEvents } from "@/lib/sessions/event-stream";
export type { LiveEvent } from "@/lib/sessions/event-stream";
// === end W2.1 ===

// === W2.3 — past-turns listing + provider test/models + credential reveal ===
//
// Four small helpers wired to backend endpoints that the W1.x backports
// landed:
//
//   * `GET  /admin/sessions/{key}/turns`                       — W1.2
//   * `POST /admin/providers/{name}/test`                      — W1.1
//   * `GET  /admin/providers/{name}/models`                    — W1.1
//   * `GET  /admin/credentials/{provider}/{key}/reveal`        — W2.1 (cred reveal)
//
// Kept in one block so the surface stays grep-able. The W1.1 kinds
// descriptor reshape (`/admin/providers/kinds` now returns
// `{kinds: [{kind, label, description, params_schema}]}`) is handled by a
// new `ProviderKindDescriptor` type + the existing `listProviderKinds`
// helper at the W-B2 block; consumers should migrate to that shape.

/** One row in `GET /admin/sessions/{key}/turns`. Mirrors the journal's
 * per-turn aggregate columns surfaced by `sessions_events.list_session_turns`. */
export interface SessionTurnRow {
  turn_id: string;
  started_at_ms: number;
  ended_at_ms?: number | null;
  status: string;
  finish_reason?: string | null;
  elapsed_ms?: number | null;
  estimated_cost_usd?: number | null;
  cost_status?: string | null;
  tool_call_count?: number | null;
  reasoning_token_count?: number | null;
  user_text_preview?: string | null;
}

export interface SessionTurnsResponse {
  session_key: string;
  turns: SessionTurnRow[];
  next_cursor: string | null;
}

/** GET /admin/sessions/{key}/turns — cursor-paginated past-turns listing. */
export async function listSessionTurns(
  sessionKey: string,
  opts: { limit?: number; before_turn_id?: string } = {},
): Promise<SessionTurnsResponse> {
  const params = new URLSearchParams();
  if (opts.limit !== undefined) params.set("limit", String(opts.limit));
  if (opts.before_turn_id) params.set("before_turn_id", opts.before_turn_id);
  const qs = params.toString();
  const path = `/admin/sessions/${encodeURIComponent(sessionKey)}/turns${
    qs ? `?${qs}` : ""
  }`;
  return apiFetch<SessionTurnsResponse>(path);
}

/** Response shape for `POST /admin/providers/{name}/test`. */
export interface ProviderTestResponse {
  ok: boolean;
  latency_ms: number;
  models_count?: number;
  error?: string;
  note?: string;
}

/** POST /admin/providers/{name}/test — zero-cost connectivity probe. */
export async function testProvider(
  name: string,
): Promise<ProviderTestResponse> {
  return apiFetch<ProviderTestResponse>(
    `/admin/providers/${encodeURIComponent(name)}/test`,
    { method: "POST", body: {} },
  );
}

/** One row in `GET /admin/providers/{name}/models`. */
export interface ProviderModel {
  id: string;
  display_name?: string;
  created_at?: string;
}

export interface ProviderModelProbeRequest {
  kind: ProviderKind;
  base_url?: string;
  api_key?: { env: string } | { value: string } | null;
  existing_name?: string;
  params?: Record<string, unknown>;
}

/** GET /admin/providers/{name}/models — proxy/canned model catalog. */
export async function getProviderModels(
  name: string,
  opts: { signal?: AbortSignal } = {},
): Promise<{ models: ProviderModel[]; error?: string }> {
  return apiFetch<{ models: ProviderModel[]; error?: string }>(
    `/admin/providers/${encodeURIComponent(name)}/models`,
    { signal: opts.signal },
  );
}

/** POST /admin/providers/probe-models — model catalog for an unsaved draft. */
export async function probeProviderModels(
  body: ProviderModelProbeRequest,
  opts: { signal?: AbortSignal } = {},
): Promise<{ models: ProviderModel[]; error?: string }> {
  return apiFetch<{ models: ProviderModel[]; error?: string }>(
    "/admin/providers/probe-models",
    { method: "POST", body, signal: opts.signal },
  );
}

/** Wire shape of `GET /admin/credentials/{provider}/{key}/reveal`. */
interface CredentialRevealResponse {
  value: string;
}

/** GET /admin/credentials/{provider}/{key}/reveal — returns the bare
 * cleartext value. Consumers wrap the reveal/hide UI themselves. */
export async function revealCredential(
  provider: string,
  key: string,
): Promise<string> {
  const res = await apiFetch<CredentialRevealResponse>(
    `/admin/credentials/${encodeURIComponent(provider)}/${encodeURIComponent(
      key,
    )}/reveal`,
  );
  return res.value;
}

/** W1.1 — descriptor row for one provider kind. Mirrors the response shape
 * of `GET /admin/providers/kinds`. */
export interface ProviderKindDescriptor {
  kind: string;
  label?: string;
  description?: string;
  params_schema?: JSONSchema;
}

/** GET /admin/providers/kinds → descriptor list (W1.1 shape). Coexists with
 * the legacy `listProviderKinds()` above which flattens to bare strings. */
export async function listProviderKindDescriptors(): Promise<
  ProviderKindDescriptor[]
> {
  const res = await apiFetch<{ kinds: unknown }>("/admin/providers/kinds");
  const kinds = Array.isArray(res.kinds) ? res.kinds : [];
  return kinds.map((row) => {
    // Tolerate both the legacy `[string]` shape and the new descriptor
    // shape so a gateway mid-rollout doesn't break the UI.
    if (typeof row === "string") return { kind: row };
    const r = row as ProviderKindDescriptor;
    return {
      kind: String(r.kind ?? ""),
      label: r.label,
      description: r.description,
      params_schema: r.params_schema,
    };
  });
}
// === end W2.3 ===

// === W1.2 system / update checker (do not edit other blocks) ===
//
// Mirrors `routes_admin_b/system.py`. Three endpoints behind `/admin/system/*`:
//
//   * GET  /admin/system/info             → UpdateStatus (cached)
//   * POST /admin/system/check-updates    → UpdateStatus (force refresh)
//   * GET  /admin/system/upgrade-commands → UpgradeCommands
//
// The TopNav `<UpdateBubble>` polls `/info` every 30s; the `/admin/system`
// page also drives the force-refresh button + reads the upgrade commands.

/** Status payload returned by `/admin/system/info` and
 * `/admin/system/check-updates`. Both endpoints share the same shape — the
 * POST variant simply bypasses the 6h backend TTL. */
export interface UpdateStatus {
  /** Currently-installed corlinman version (semver, no leading `v`). */
  current: string;
  /** Latest published release tag (e.g. `v1.4.2`), or null when the upstream
   * check has never succeeded. */
  latest: string | null;
  /** True iff `latest` is strictly newer than `current`. */
  available: boolean;
  /** GitHub HTML URL for the release page; null when no release seen yet. */
  release_url: string | null;
  /** Raw GitHub markdown for the release notes; null when not fetched. */
  release_notes_md: string | null;
  /** epoch-ms publish timestamp of the latest release. */
  published_at: number | null;
  /** epoch-ms of the last successful poll. */
  last_checked_at: number | null;
  /** Prerelease tags the checker has seen but suppressed (per config). */
  prerelease_seen: string[];
}

/** Copy-pasta blobs surfaced on `/admin/system`. Strings only — no
 * structured fields — because each variant is a single multi-line bash
 * command the operator runs as-is on their VPS. */
export interface UpgradeCommands {
  native: string;
  docker: string;
  docker_with_qq: string;
}

/** GET /admin/system/info — cached UpdateStatus (cheap; backend serves
 * from the 6h on-disk cache). */
export function fetchSystemInfo(): Promise<UpdateStatus> {
  return apiFetch<UpdateStatus>("/admin/system/info");
}

/** POST /admin/system/check-updates — force-refresh the GitHub poll
 * bypassing the TTL. Returns the same shape as `fetchSystemInfo`. */
export function checkForUpdates(): Promise<UpdateStatus> {
  return apiFetch<UpdateStatus>("/admin/system/check-updates", {
    method: "POST",
    body: {},
  });
}

/** GET /admin/system/upgrade-commands — the three copy-pasta upgrade
 * recipes (native systemd / docker / docker+QQ). Read-only. */
export function fetchUpgradeCommands(): Promise<UpgradeCommands> {
  return apiFetch<UpgradeCommands>("/admin/system/upgrade-commands");
}
// === end W1.2 ===

// === W2.1 — skill hub installed surface (do not edit other blocks) ===
//
// Mirrors `gateway/routes_admin_b/skills.py`. Three endpoints behind
// `/admin/skills` drive the Installed tab on `/admin/skills`:
//
//   * GET    /admin/skills?profile=default       → InstalledSkillsResponse
//   * POST   /admin/skills/{name}/pin            → InstalledSkillRow
//   * DELETE /admin/skills/{name}?profile=…      → 204
//
// The names below are deliberately distinct from the older curator
// surface (`listProfileSkills`, `pinSkill`, `SkillSummary`,
// `SkillsListResponse`) so both clients keep type-checking while the
// curator endpoints linger. The wire shape is the gateway's
// `InstalledSkillOut` 1:1.
//
// `origin` is a free-form string: `"bundled" | "user" | "hub:<slug>@<ver>"`.
// A 409 `bundled_protected` from DELETE means the row ships in-wheel and
// is intentionally read-only.

export interface InstalledSkillRow {
  name: string;
  description: string;
  version: string;
  /** Curator state — "active" | "stale" | "archived" today. */
  state: string;
  /** Free-form origin tag: `"bundled" | "user" | "hub:<slug>@<ver>"`. */
  origin: string;
  pinned: boolean;
  use_count: number;
  last_used_at: string | null;
  created_at: string | null;
  // Editor-facing fields — mirror the writable keys on `SkillUpdateBody`.
  // The gateway populates these when its skill-registry factory is wired
  // (the disk-only fallback leaves them at the defaults below). The edit
  // drawer seeds its form from these and round-trips via
  // `updateInstalledSkill`.
  /** Raw SKILL.md prose injected into the assembler verbatim. */
  body_markdown: string;
  /** Model-selection hint parsed off the frontmatter; `null` when absent. */
  when_to_use: string | null;
  /** Tool allowlist scoped to this skill's turns. */
  allowed_tools: string[];
  /** When `true` the model can't auto-invoke the skill (manual-only). */
  disable_model_invocation: boolean;
}

export interface InstalledSkillsResponse {
  profile: string;
  rows: InstalledSkillRow[];
}

/** GET /admin/skills?profile=… — full row list for one profile. */
export function listInstalledSkills(
  profile: string = "default",
): Promise<InstalledSkillsResponse> {
  const qs = new URLSearchParams({ profile }).toString();
  return apiFetch<InstalledSkillsResponse>(`/admin/skills?${qs}`);
}

/** POST /admin/skills/{name}/pin — toggle the pinned flag. */
export function pinInstalledSkill(
  name: string,
  pinned: boolean,
  profile: string = "default",
): Promise<InstalledSkillRow> {
  const qs = new URLSearchParams({ profile }).toString();
  return apiFetch<InstalledSkillRow>(
    `/admin/skills/${encodeURIComponent(name)}/pin?${qs}`,
    { method: "POST", body: { pinned } },
  );
}

/**
 * Partial patch for `PUT /admin/skills/{name}`. Every field is optional —
 * only keys present in the object are written back to the SKILL.md. The
 * five fields below are runtime-consumed (the registry parses them off
 * frontmatter/body and the context assembler honours
 * `disable_model_invocation` / `allowed_tools` / `when_to_use`).
 */
export interface SkillUpdateBody {
  description?: string;
  body_markdown?: string;
  disable_model_invocation?: boolean;
  allowed_tools?: string[];
  when_to_use?: string;
}

/** PUT /admin/skills/{name}?profile=… — edit body + runtime metadata. */
export function updateInstalledSkill(
  name: string,
  body: SkillUpdateBody,
  profile: string = "default",
): Promise<InstalledSkillRow> {
  const qs = new URLSearchParams({ profile }).toString();
  return apiFetch<InstalledSkillRow>(
    `/admin/skills/${encodeURIComponent(name)}?${qs}`,
    { method: "PUT", body },
  );
}

/** DELETE /admin/skills/{name}?profile=… — uninstall. 409
 * `bundled_protected` when the row ships with corlinman. */
export function deleteInstalledSkill(
  name: string,
  profile: string = "default",
): Promise<void> {
  const qs = new URLSearchParams({ profile }).toString();
  return apiFetch<void>(
    `/admin/skills/${encodeURIComponent(name)}?${qs}`,
    { method: "DELETE" },
  );
}
// === end W2.1 — skill hub installed surface ===

// === One-click upgrade (Wave 2 of PLAN_ONE_CLICK_UPGRADE) ===

export interface UpgradeStartResponse {
  request_id: string;
  state: string;
  mode: string;
  tag: string;
}

export interface UpgradeStatusResponse {
  request_id: string;
  tag: string;
  state:
    | "queued"
    | "running"
    | "succeeded"
    | "failed"
    | "stalled"
    | "cancelled";
  phase: string;
  started_at: number | null;
  finished_at: number | null;
  log_excerpt: string;
  error: string | null;
}

/** POST /admin/system/upgrade — body `{tag, typed_confirmation,
 * allow_downgrade?}`. Backend enforces typed_confirmation === tag,
 * tag-in-releases-whitelist, no-downgrade, single-flight (409). */
export function startSystemUpgrade(
  tag: string,
  typed_confirmation: string,
  opts?: { allow_downgrade?: boolean },
): Promise<UpgradeStartResponse> {
  return apiFetch<UpgradeStartResponse>("/admin/system/upgrade", {
    method: "POST",
    body: { tag, typed_confirmation, allow_downgrade: opts?.allow_downgrade },
  });
}

/** GET /admin/system/upgrade/{request_id}/status — read-once snapshot. */
export function fetchUpgradeStatus(
  request_id: string,
): Promise<UpgradeStatusResponse> {
  return apiFetch<UpgradeStatusResponse>(
    `/admin/system/upgrade/${encodeURIComponent(request_id)}/status`,
  );
}

/** GET /admin/system/upgrade/{request_id}/events — SSE stream. Frames:
 *   `event: status\ndata: <UpgradeStatusResponse JSON>\n\n`
 * Terminates when state ∈ {succeeded, failed, stalled, cancelled}.
 * 10s keepalive comment frames in between. */
export function streamUpgradeEvents(
  request_id: string,
  opts?: { lastEventId?: string },
): EventSource {
  const qs = new URLSearchParams();
  if (opts?.lastEventId) qs.set("last_event_id", opts.lastEventId);
  const suffix = qs.toString() ? `?${qs}` : "";
  const url = `${GATEWAY_BASE_URL}/admin/system/upgrade/${encodeURIComponent(request_id)}/events${suffix}`;
  return new EventSource(url, { withCredentials: true });
}

export interface AuditEntry {
  ts: string; // ISO 8601 UTC
  event: string; // e.g. "system.upgrade.requested"
  request_id?: string | null;
  tag?: string | null;
  actor?: string | null;
  details: Record<string, unknown>;
}

export interface AuditTailResponse {
  entries: AuditEntry[];
  next_before_ts?: string | null;
}

/** GET /admin/system/audit — paginated audit log (newest first).
 * `before_ts` is the cursor returned in `next_before_ts`. */
export function listSystemAudit(opts?: {
  limit?: number;
  before_ts?: string;
}): Promise<AuditTailResponse> {
  const qs = new URLSearchParams();
  if (opts?.limit !== undefined) qs.set("limit", String(opts.limit));
  if (opts?.before_ts) qs.set("before_ts", opts.before_ts);
  const suffix = qs.toString() ? `?${qs}` : "";
  return apiFetch<AuditTailResponse>(`/admin/system/audit${suffix}`);
}

// ---------------------------------------------------------------------------
// W2.2 — Skill hub (ClawHub browse + install)
//
// Wire shapes mirror `gateway/routes_admin_b/skill_hub.py` (W1.3-frozen
// contract). Endpoints under `/admin/skills/hub/*` proxy ClawHub's anonymous
// read API + drive an async install pipeline with SSE progress.
//
// `{offline: true, rows: []}` indicates ClawHub is unreachable — UI surfaces
// a banner + retry rather than throwing.
// ---------------------------------------------------------------------------

export interface HubSkillSummary {
  slug: string;
  name: string;
  description: string;
  emoji?: string;
  stars: number;
  downloads: number;
  latest_version: string;
  /** ISO-8601 UTC. */
  updated_at: string;
}

export interface HubSearchResponse {
  rows: HubSkillSummary[];
  offline: boolean;
}

export interface HubListResponse extends HubSearchResponse {
  next_cursor: string | null;
}

export interface HubSkillDetail extends HubSkillSummary {
  homepage?: string;
  versions: string[];
  scan_summary?: "pass" | "warn" | "fail";
  readme_excerpt: string;
}

export type HubSortKey = "trending" | "downloads" | "stars" | "updated";

export interface HubInstallStatusOut {
  request_id: string;
  slug: string;
  version: string;
  profile: string;
  state: "queued" | "running" | "installed" | "failed";
  phase: string;
  /** epoch-ms */
  started_at?: number;
  /** epoch-ms */
  finished_at?: number;
  name?: string;
  error?: string;
  message?: string;
}

/** GET /admin/skills/hub/search?q=&limit= */
export function searchHubSkills(
  q: string,
  limit?: number,
): Promise<HubSearchResponse> {
  const params = new URLSearchParams();
  params.set("q", q);
  if (limit !== undefined) params.set("limit", String(limit));
  return apiFetch<HubSearchResponse>(
    `/admin/skills/hub/search?${params.toString()}`,
  );
}

/** GET /admin/skills/hub/featured?sort=&cursor=&limit= */
export function listHubFeatured(
  sort: HubSortKey,
  cursor?: string | null,
  limit?: number,
): Promise<HubListResponse> {
  const params = new URLSearchParams();
  params.set("sort", sort);
  if (cursor) params.set("cursor", cursor);
  if (limit !== undefined) params.set("limit", String(limit));
  return apiFetch<HubListResponse>(
    `/admin/skills/hub/featured?${params.toString()}`,
  );
}

/** GET /admin/skills/hub/skills/{slug} */
export function getHubSkill(slug: string): Promise<HubSkillDetail> {
  return apiFetch<HubSkillDetail>(
    `/admin/skills/hub/skills/${encodeURIComponent(slug)}`,
  );
}

/** GET /admin/skills/hub/skills/{slug}/file?path=SKILL.md → raw file body. */
export function getHubSkillFile(
  slug: string,
  path: string,
): Promise<{ content: string }> {
  const params = new URLSearchParams({ path });
  return apiFetch<{ content: string }>(
    `/admin/skills/hub/skills/${encodeURIComponent(slug)}/file?${params.toString()}`,
  );
}

/** POST /admin/skills/hub/install → 202 + request_id. */
export function postHubInstall(body: {
  slug: string;
  version?: string;
  profile?: string;
  force?: boolean;
}): Promise<{ request_id: string }> {
  return apiFetch<{ request_id: string }>("/admin/skills/hub/install", {
    method: "POST",
    body,
  });
}

/** GET /admin/skills/hub/install/{request_id} → read-once snapshot. */
export function getHubInstallStatus(
  request_id: string,
): Promise<HubInstallStatusOut> {
  return apiFetch<HubInstallStatusOut>(
    `/admin/skills/hub/install/${encodeURIComponent(request_id)}`,
  );
}

/** GET /admin/skills/hub/install/{request_id}/events/live → SSE.
 * Frames: `event: phase\ndata: <HubInstallStatusOut JSON>\n\n`.
 * Stream closes when state ∈ {"installed", "failed"}. Callers attach
 * `addEventListener("phase", …)` and own cleanup via `.close()`. */
export function streamHubInstallEvents(
  request_id: string,
  onMessage: (frame: HubInstallStatusOut) => void,
): EventSource {
  const url = `${GATEWAY_BASE_URL}/admin/skills/hub/install/${encodeURIComponent(
    request_id,
  )}/events/live`;
  const es = new EventSource(url, { withCredentials: true });
  es.addEventListener("phase", (ev) => {
    try {
      const data = JSON.parse((ev as MessageEvent).data);
      onMessage(data as HubInstallStatusOut);
    } catch {
      /* malformed frame — ignore; stream will recover or close on terminal */
    }
  });
  return es;
}

// ===========================================================================
// Marketplace — MCP servers + Plugin market + GitHub-acceleration settings.
//
// Backend is the frozen contract documented in the Marketplace plan. All
// endpoints live behind `/admin/*` admin auth and reuse the same
// `credentials: "include"` cookie the rest of this client sends.
//
//   MCP market      → /admin/mcp/market, /admin/mcp/market/{slug}
//   MCP install     → /admin/mcp/install
//   MCP servers     → /admin/mcp/servers, DELETE /admin/mcp/{name}
//   MCP lifecycle   → POST /admin/plugins/{name}/{enable,disable,restart}
//   Plugin market   → /admin/plugins/market(/{slug})(/install|enable|disable)
//   Accel settings  → /admin/marketplace/settings, /admin/marketplace/accel/test
//
// The MCP-market `rows` array carries the same summary shape used by both
// the MCP and Plugin browse grids, so both reuse `<MarketCard>`.
// ===========================================================================

/** One row in the MCP market grid. The Plugin market reuses this shape
 * minus `transport` / `requires_env` (which are MCP-only fields). */
export interface McpMarketItem {
  slug: string;
  name: string;
  description: string;
  latest_version: string;
  emoji: string | null;
  /** MCP transport ("stdio" | "http" | …). Plugin rows leave this null. */
  transport: string | null;
  stars: number;
  downloads: number;
  /** ISO-8601 UTC. */
  updated_at: string;
  tags: string[];
  /** Env var names the server requires at install time. */
  requires_env: string[];
}

/** Plugin market row — same shape as `McpMarketItem`; `transport` /
 * `requires_env` are present on the wire but not meaningful for plugins. */
export type PluginMarketItem = McpMarketItem;

export interface McpMarketResponse {
  rows: McpMarketItem[];
  next_cursor: string | null;
  offline: boolean;
  error: string | null;
}

export interface PluginMarketResponse {
  rows: PluginMarketItem[];
  next_cursor: string | null;
  offline: boolean;
  error: string | null;
}

/** A staged/installed MCP server with live status. */
export interface InstalledMcpServer {
  name: string;
  source: string;
  version: string;
  enabled: boolean;
  transport: string | null;
  status: "ready" | "error" | "pending" | "stopped";
  tools: number;
  error: string | null;
  installed_at: string;
  updated_at: string;
}

/** A staged/installed plugin-market row. */
export interface InstalledPluginRow {
  slug: string;
  version: string;
  source: string;
  enabled: boolean;
  installed_at: string;
  updated_at: string;
}

// ---- MCP market ------------------------------------------------------------

/** GET /admin/mcp/market?cursor=&limit= */
export function listMcpMarket(opts?: {
  cursor?: string | null;
  limit?: number;
}): Promise<McpMarketResponse> {
  const params = new URLSearchParams();
  if (opts?.cursor) params.set("cursor", opts.cursor);
  if (opts?.limit !== undefined) params.set("limit", String(opts.limit));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return apiFetch<McpMarketResponse>(`/admin/mcp/market${suffix}`);
}

/** GET /admin/mcp/market/{slug} — detail with `requires_env` populated. */
export function getMcpMarketItem(slug: string): Promise<McpMarketItem> {
  return apiFetch<McpMarketItem>(
    `/admin/mcp/market/${encodeURIComponent(slug)}`,
  );
}

/** POST /admin/mcp/install — stages the server (installed, disabled). When
 * the market item declares `requires_env`, the caller MUST collect those
 * values and pass them in `env`. */
export function installMcpServer(body: {
  slug: string;
  version?: string;
  env?: Record<string, string>;
}): Promise<InstalledMcpServer> {
  return apiFetch<InstalledMcpServer>("/admin/mcp/install", {
    method: "POST",
    body,
  });
}

/** GET /admin/mcp/servers — installed servers with live status. */
export function listMcpServers(): Promise<InstalledMcpServer[]> {
  return apiFetch<InstalledMcpServer[]>("/admin/mcp/servers");
}

/** DELETE /admin/mcp/{name} — uninstall a server. */
export function deleteMcpServer(
  name: string,
): Promise<{ ok: boolean; name: string; removed: boolean }> {
  return apiFetch<{ ok: boolean; name: string; removed: boolean }>(
    `/admin/mcp/${encodeURIComponent(name)}`,
    { method: "DELETE" },
  );
}

/** Editable launch-spec fields for {@link reconfigureMcpServer}. An absent
 * field leaves that part of the spec unchanged; a present `env`/`headers`
 * replaces the stored map wholesale. `enabled` is NOT editable here —
 * toggling stays on enable/disable. */
export interface McpReconfigureBody {
  transport?: string;
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  url?: string;
  headers?: Record<string, string>;
  version?: string;
}

/** PUT /admin/mcp/{name} — edit a server's launch spec in place (env,
 * secrets, version, command, url) without a delete + reinstall. An enabled
 * server is hot-reconnected so the new env takes effect. */
export function reconfigureMcpServer(
  name: string,
  body: McpReconfigureBody,
): Promise<InstalledMcpServer> {
  return apiFetch<InstalledMcpServer>(
    `/admin/mcp/${encodeURIComponent(name)}`,
    { method: "PUT", body },
  );
}

// MCP lifecycle is served by the existing plugins seam (hot-connect).

/** POST /admin/plugins/{name}/enable — hot-connects the MCP server. */
export function enableMcpServer(
  name: string,
): Promise<{ name: string; disabled: false }> {
  return apiFetch<{ name: string; disabled: false }>(
    `/admin/plugins/${encodeURIComponent(name)}/enable`,
    { method: "POST" },
  );
}

/** POST /admin/plugins/{name}/disable — stops + disconnects the server. */
export function disableMcpServer(
  name: string,
): Promise<{ name: string; disabled: true; stopped: true }> {
  return apiFetch<{ name: string; disabled: true; stopped: true }>(
    `/admin/plugins/${encodeURIComponent(name)}/disable`,
    { method: "POST" },
  );
}

/** POST /admin/plugins/{name}/restart — restarts the server connection. */
export function restartMcpServer(
  name: string,
): Promise<{ name: string; status: "restarted" }> {
  return apiFetch<{ name: string; status: "restarted" }>(
    `/admin/plugins/${encodeURIComponent(name)}/restart`,
    { method: "POST" },
  );
}

// ---- Plugin market ---------------------------------------------------------

/** GET /admin/plugins/market */
export function listPluginMarket(opts?: {
  cursor?: string | null;
  limit?: number;
}): Promise<PluginMarketResponse> {
  const params = new URLSearchParams();
  if (opts?.cursor) params.set("cursor", opts.cursor);
  if (opts?.limit !== undefined) params.set("limit", String(opts.limit));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return apiFetch<PluginMarketResponse>(`/admin/plugins/market${suffix}`);
}

/** GET /admin/plugins/market/{slug} */
export function getPluginMarketItem(slug: string): Promise<PluginMarketItem> {
  return apiFetch<PluginMarketItem>(
    `/admin/plugins/market/${encodeURIComponent(slug)}`,
  );
}

/** POST /admin/plugins/market/install — stages the plugin (HTTP 201). */
export function installPluginMarket(body: {
  slug: string;
  version?: string;
}): Promise<InstalledPluginRow> {
  return apiFetch<InstalledPluginRow>("/admin/plugins/market/install", {
    method: "POST",
    body,
  });
}

/** POST /admin/plugins/market/{slug}/enable — `applies` is "now" or
 * "next_restart"; callers SHOULD surface the latter to the operator. */
export function enablePluginMarket(slug: string): Promise<{
  slug: string;
  enabled: true;
  applies: "now" | "next_restart";
  row: InstalledPluginRow;
}> {
  return apiFetch(
    `/admin/plugins/market/${encodeURIComponent(slug)}/enable`,
    { method: "POST" },
  );
}

/** POST /admin/plugins/market/{slug}/disable */
export function disablePluginMarket(
  slug: string,
): Promise<InstalledPluginRow> {
  return apiFetch<InstalledPluginRow>(
    `/admin/plugins/market/${encodeURIComponent(slug)}/disable`,
    { method: "POST" },
  );
}

/** DELETE /admin/plugins/market/{slug} */
export function deletePluginMarket(slug: string): Promise<{
  ok: boolean;
  slug: string;
  bundle_removed: boolean;
  index_removed: boolean;
}> {
  return apiFetch(`/admin/plugins/market/${encodeURIComponent(slug)}`, {
    method: "DELETE",
  });
}

// ---- Acceleration settings (read-only) -------------------------------------

export interface MarketplaceAccel {
  mode: "off" | "auto" | "on";
  preset: "ghproxy" | "jsdelivr" | "mirror" | "custom";
  base: string;
  mirror_host: string;
  assume_region: string;
  enabled: boolean;
}

export interface MarketplaceSettings {
  registry_repo: string;
  registry_ref: string;
  default_source: string;
  clawhub_enabled: boolean;
  github_token_set: boolean;
  index_url: string;
  accelerated_index_url: string;
  accel: MarketplaceAccel;
}

/** GET /admin/marketplace/settings — read-only; editing is done via the
 * Config TOML editor under `[marketplace.github_proxy]`. */
export function getMarketplaceSettings(): Promise<MarketplaceSettings> {
  return apiFetch<MarketplaceSettings>("/admin/marketplace/settings");
}

/** One leg of an acceleration probe (direct or accelerated). */
export interface ProbeLeg {
  url: string;
  ok: boolean;
  status: number | null;
  ms: number | null;
  error: string | null;
}

export interface AccelTestResult {
  enabled: boolean;
  direct: ProbeLeg;
  accelerated: ProbeLeg;
}

/** POST /admin/marketplace/accel/test — probe direct vs accelerated. */
export function testMarketplaceAccel(): Promise<AccelTestResult> {
  return apiFetch<AccelTestResult>("/admin/marketplace/accel/test", {
    method: "POST",
  });
}
