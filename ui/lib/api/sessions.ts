/**
 * Sessions admin API client (Phase 4 W2 4-2D — Trajectory replay).
 *
 * Mirrors the Rust HTTP routes at
 * `rust/crates/corlinman-gateway/src/routes/admin/sessions.rs`:
 *
 *   GET  /admin/sessions
 *     → 200 { sessions: SessionSummary[] }
 *     → 503 { error: "sessions_disabled" }
 *
 *   POST /admin/sessions/:key/replay
 *     body (optional): { mode?: "transcript" | "rerun" }
 *     → 200 ReplayResponse
 *     → 404 { error: "not_found", session_key: <key> }
 *     → 503 { error: "sessions_disabled" }
 *
 * `last_message_at` is **unix milliseconds** (the SQLite column is i64) — the
 * UI formats it via `new Date(ms).toLocaleString()`. Transcript message `ts`
 * is RFC-3339 / ISO-8601 (matches the `tenants.created_at` convention).
 *
 * The `rerun` mode replays recorded user turns through the live agent backend.
 * Newer gateways include a `rerun.generated` assistant transcript; older
 * gateways may still return `summary.rerun_diff = "not_implemented_yet"`.
 */

import { CorlinmanApiError, apiFetch } from "@/lib/api";

/* ------------------------------------------------------------------ */
/*                           Public types                             */
/* ------------------------------------------------------------------ */

/** One row in `GET /admin/sessions`. */
export interface SessionSummary {
  /** Composite key — typically `<channel>:<scope>:<id>`, e.g. `qq:1234`. */
  session_key: string;
  /** Unix milliseconds of the most-recent message in the session. */
  last_message_at: number;
  /**
   * Unix milliseconds of the most-recent observed activity (heartbeat,
   * typing, etc.). Optional — older gateways may not emit it; the UI falls
   * back to `last_message_at` in that case.
   */
  last_seen_at_ms?: number;
  /** Total message count across both roles. */
  message_count: number;
}

export type ReplayMode = "transcript" | "rerun";

export type TranscriptRole = "user" | "assistant" | "system";

/** OpenAI-shaped tool_call as it round-trips through the journal. The
 *  optional `result` field is filled in by the replay endpoint by
 *  pairing the matching `role="tool"` row to its originating call so
 *  the chat UI can show both invocation + result on session resume. */
export interface TranscriptToolCall {
  id?: string;
  type?: string;
  function?: { name?: string; arguments?: string };
  result?: string;
}

/** One message in a replay's `transcript` array. */
export interface TranscriptMessage {
  role: TranscriptRole;
  content: string;
  /** RFC-3339 / ISO-8601 string. */
  ts: string;
  /** Present on assistant messages that issued tool calls. Empty / absent
   *  for plain text turns. */
  tool_calls?: TranscriptToolCall[];
  /** W3 — journaled attachment metadata for user messages, so the chat
   *  UI re-renders image/file cards on session resume. */
  attachments?: TranscriptAttachment[];
}

/** Slim journaled attachment reference (no bytes — the UI re-fetches
 *  stored content from `/v1/files/{id}`). */
export interface TranscriptAttachment {
  kind?: string;
  url?: string;
  mime?: string;
  name?: string;
}

/** Summary block on a replay response. */
export interface ReplaySummary {
  message_count: number;
  tenant_id: string;
  /**
   * `"changed"` / `"unchanged"` for live reruns, or the legacy
   * `"not_implemented_yet"` sentinel from older gateways.
   */
  rerun_diff?: string;
}

export interface RerunGeneratedMessage {
  role: "assistant";
  content: string;
}

export interface ReplayRerun {
  generated: RerunGeneratedMessage[];
  finish_reason?: string | null;
}

export interface ReplayResponse {
  session_key: string;
  mode: ReplayMode;
  transcript: TranscriptMessage[];
  summary: ReplaySummary;
  rerun?: ReplayRerun;
}

/** Tagged result so callers can branch on `503 sessions_disabled` without
 *  having to pattern-match on `CorlinmanApiError.message`. */
export type SessionsListResult =
  | { kind: "ok"; sessions: SessionSummary[] }
  | { kind: "disabled" };

export type ReplayResult =
  | { kind: "ok"; replay: ReplayResponse }
  | { kind: "not_found"; session_key: string }
  | { kind: "disabled" }
  | { kind: "rerun_disabled" };

/**
 * Per-key delete result. `not_found` is treated as success-equivalent by
 * the page (the row is removed from the list either way) but kept as a
 * distinct tag so callers can suppress the success toast on idempotent
 * re-deletes if they want.
 */
export type DeleteSessionResult =
  | { kind: "ok"; deleted: number }
  | { kind: "not_found"; session_key: string }
  | { kind: "disabled" };

export type DeleteAllSessionsResult =
  | { kind: "ok"; deleted: number }
  | { kind: "disabled" };

/* ------------------------------------------------------------------ */
/*                          URL builders                              */
/* ------------------------------------------------------------------ */

/** GET path for the list endpoint. */
export const SESSIONS_LIST_PATH = "/admin/sessions";

/**
 * POST path for the replay endpoint. Encodes `session_key` so colons and
 * other punctuation in keys like `qq:group:123` round-trip through the URL.
 */
export function sessionsReplayPath(sessionKey: string): string {
  return `/admin/sessions/${encodeURIComponent(sessionKey)}/replay`;
}

/** DELETE path for a single session. */
export function sessionDeletePath(sessionKey: string): string {
  return `/admin/sessions/${encodeURIComponent(sessionKey)}`;
}

/* ------------------------------------------------------------------ */
/*                          Error helpers                             */
/* ------------------------------------------------------------------ */

function is404(err: unknown): boolean {
  return err instanceof CorlinmanApiError && err.status === 404;
}

function hasErrorCode(err: unknown, status: number, code: string): boolean {
  if (!(err instanceof CorlinmanApiError) || err.status !== status) {
    return false;
  }
  try {
    const body = JSON.parse(err.message) as { error?: unknown };
    return body.error === code;
  } catch {
    return err.message.includes(code);
  }
}

/* ------------------------------------------------------------------ */
/*                           Public fetches                           */
/* ------------------------------------------------------------------ */

/**
 * GET /admin/sessions → list of session keys with last_message_at + count.
 *
 * Returns a tagged `SessionsListResult` so the page can paint the
 * "session storage is off" banner on 503 without inspecting exception
 * messages itself.
 */
export async function fetchSessions(): Promise<SessionsListResult> {
  try {
    const res = await apiFetch<{ sessions: SessionSummary[] }>(
      SESSIONS_LIST_PATH,
    );
    return { kind: "ok", sessions: res.sessions ?? [] };
  } catch (err) {
    if (hasErrorCode(err, 503, "sessions_disabled")) {
      return { kind: "disabled" };
    }
    throw err;
  }
}

/**
 * POST /admin/sessions/:key/replay → transcript dump or live rerun.
 *
 * Defaults `mode` to `"transcript"` when omitted (matches Agent A's Rust
 * route default). Returns a tagged result so the dialog can render an inline
 * "session not found" message on 404 instead of a global error toast.
 */
export async function replaySession(
  sessionKey: string,
  opts?: { mode?: ReplayMode },
): Promise<ReplayResult> {
  const mode: ReplayMode = opts?.mode ?? "transcript";
  try {
    const replay = await apiFetch<ReplayResponse>(
      sessionsReplayPath(sessionKey),
      {
        method: "POST",
        body: { mode },
      },
    );
    return { kind: "ok", replay };
  } catch (err) {
    if (is404(err)) return { kind: "not_found", session_key: sessionKey };
    if (hasErrorCode(err, 503, "sessions_disabled")) {
      return { kind: "disabled" };
    }
    if (hasErrorCode(err, 503, "rerun_disabled")) {
      return { kind: "rerun_disabled" };
    }
    throw err;
  }
}

/**
 * DELETE /admin/sessions/:key — removes a single session from the journal.
 *
 * Idempotent: a 404 is mapped to a tagged `not_found` instead of an
 * exception so the UI can optimistically prune the row either way.
 */
export async function deleteSession(
  sessionKey: string,
): Promise<DeleteSessionResult> {
  try {
    const res = await apiFetch<{ deleted?: number } | null>(
      sessionDeletePath(sessionKey),
      { method: "DELETE" },
    );
    const deleted =
      typeof res === "object" && res !== null && typeof res.deleted === "number"
        ? res.deleted
        : 1;
    return { kind: "ok", deleted };
  } catch (err) {
    if (is404(err)) return { kind: "not_found", session_key: sessionKey };
    if (hasErrorCode(err, 503, "sessions_disabled")) {
      return { kind: "disabled" };
    }
    throw err;
  }
}

/**
 * DELETE /admin/sessions — clears every session in the journal. Server
 * returns `{ deleted: N }` for the count it just wiped.
 */
export async function deleteAllSessions(): Promise<DeleteAllSessionsResult> {
  try {
    const res = await apiFetch<{ deleted?: number } | null>(
      SESSIONS_LIST_PATH,
      { method: "DELETE" },
    );
    const deleted =
      typeof res === "object" && res !== null && typeof res.deleted === "number"
        ? res.deleted
        : 0;
    return { kind: "ok", deleted };
  } catch (err) {
    if (hasErrorCode(err, 503, "sessions_disabled")) {
      return { kind: "disabled" };
    }
    throw err;
  }
}

/** Legacy sentinel value emitted by older gateways for `mode === "rerun"`. */
export const RERUN_NOT_IMPLEMENTED = "not_implemented_yet" as const;
