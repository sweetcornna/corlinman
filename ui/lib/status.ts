/**
 * Public **agent status card** client — the unauthenticated counterpart to
 * the admin sessions surface (`lib/api.ts` + `lib/sessions/event-stream.ts`).
 *
 * A status token is a signed, read-only capability scoping access to exactly
 * ONE conversation's status + work trajectory (see
 * `corlinman_server.gateway.status_token`). The agent hands a chat user a
 * clickable link `{public_url}/status/{token}`; this module powers the page
 * that link opens.
 *
 * SHARED CONTRACT (must match the backend exactly):
 *
 *   GET /status/{token}                  → StatusSnapshot JSON (this file's
 *                                          {@link StatusSnapshot}); 403 when
 *                                          the token fails `verify()`.
 *   GET /status/{token}/events/live      → text/event-stream of the SAME
 *                                          LiveEvent envelopes the admin
 *                                          `/admin/sessions/{key}/events/live`
 *                                          route emits, so we can fold them
 *                                          through the existing timeline
 *                                          reducer untouched.
 *
 * Both routes are mounted at ROOT with NO auth (auth only gates /v1 + /admin),
 * so — unlike `apiFetch` — we deliberately do NOT send `credentials:
 * "include"` here. The token in the path IS the capability.
 */

import { GATEWAY_BASE_URL } from "@/lib/api";
import type { LiveEvent } from "@/lib/sessions/event-stream";

/** Coarse run-state of the conversation the token authorizes. Mirrors the
 *  backend's per-session rollup. We keep the union open (`| string`) so a
 *  future backend state doesn't hard-break the render — the pill falls back
 *  to a neutral tone for anything it doesn't recognise. */
export type StatusState =
  | "idle"
  | "running"
  | "streaming"
  | "complete"
  | "errored"
  | "cancelling"
  | (string & {});

/**
 * One past/current turn summary in the snapshot. Optional throughout because
 * the public surface is intentionally lean — the authoritative trajectory is
 * carried by `events` (folded through the shared reducer). `turns` is here so
 * the page can show an at-a-glance count / status without replaying every
 * event, and degrade gracefully when the backend omits it.
 */
export interface StatusTurn {
  turn_id: string;
  status?: string;
  /** epoch-ms */
  started_at_ms?: number;
  /** epoch-ms */
  ended_at_ms?: number;
  elapsed_ms?: number;
}

/**
 * Response shape of `GET /status/{token}`.
 *
 * `events` are raw {@link LiveEvent} envelopes — identical wire shape to the
 * admin SSE / replay surfaces — so `EventTimelineBody mode="replay"` can seed
 * them straight into the `TimelineProvider` reducer with zero translation.
 */
export interface StatusSnapshot {
  /** The conversation the token authorizes (and only that one). */
  session_key: string;
  /** Coarse rollup state for the header pill. */
  status: StatusState;
  /** Lightweight per-turn summaries (count / elapsed). May be empty. */
  turns: StatusTurn[];
  /** Full trajectory as LiveEvent envelopes for the read-only timeline. */
  events: LiveEvent[];
  /**
   * epoch-ms the conversation started. Optional — when present the page
   * renders a live-ticking "elapsed" readout. Older backends omit it; we
   * fall back to the earliest event timestamp.
   */
  started_at_ms?: number;
  /** epoch-ms of the most recent activity, if the backend supplies it. */
  updated_at_ms?: number;
}

/** Thrown by {@link fetchStatusSnapshot} so the page can branch on `expired`
 *  (HTTP 403 — link invalid / expired) versus a transient network error. */
export class StatusFetchError extends Error {
  readonly status?: number;
  /** True iff the token failed verification (403) — a terminal, clean
   *  "link expired or invalid" state rather than a retryable blip. */
  readonly expired: boolean;
  constructor(message: string, status?: number) {
    super(message);
    this.name = "StatusFetchError";
    this.status = status;
    this.expired = status === 403;
  }
}

/** Build the public path for a token. Centralised so the page, fetch, and
 *  SSE helpers can't drift on encoding. */
function statusPath(token: string): string {
  return `/status/${encodeURIComponent(token)}`;
}

/**
 * Fetch the initial snapshot for a token.
 *
 * Sends `Accept: application/json` explicitly: the same `/status/{token}`
 * origin path is what a human clicks in a browser (which Stream A serves as
 * the HTML shell via content negotiation), so we must signal that *this*
 * request wants the JSON capability payload, not the page.
 *
 * Throws {@link StatusFetchError} with `expired === true` on 403.
 */
export async function fetchStatusSnapshot(
  token: string,
  init?: { signal?: AbortSignal },
): Promise<StatusSnapshot> {
  let res: Response;
  try {
    res = await fetch(`${GATEWAY_BASE_URL}${statusPath(token)}/data`, {
      // No credentials — this is an unauthenticated capability URL.
      // `/data` keeps the JSON route from colliding with the static HTML
      // shell the Next export serves at the bare `/status/{token}` path.
      headers: { accept: "application/json" },
      signal: init?.signal,
    });
  } catch (err) {
    // Network / abort — surface as a non-expired (retryable) error.
    throw new StatusFetchError(
      err instanceof Error ? err.message : String(err),
    );
  }

  if (!res.ok) {
    // 403 → expired/invalid token (terminal). Anything else is treated as a
    // transient backend error the page can retry / poll past.
    const text = await res.text().catch(() => "");
    throw new StatusFetchError(
      text || `status request failed: ${res.status}`,
      res.status,
    );
  }

  return (await res.json()) as StatusSnapshot;
}

export interface OpenStatusStreamOptions {
  onEvent: (ev: LiveEvent) => void;
  /** Fired on transport error (before the backed-off reconnect). The page
   *  uses this to flip its connection pill + arm the polling fallback. */
  onError?: (err: Event) => void;
  /** Fired on first successful frame after (re)connect — clears any polling
   *  fallback the page armed while the socket was down. */
  onOpen?: () => void;
}

/** Exponential reconnect schedule (ms) — mirrors `lib/sessions/event-stream`
 *  so a wedged gateway doesn't get hammered. */
const BACKOFF_MS = [3_000, 6_000, 12_000, 30_000] as const;

/**
 * Subscribe to `GET /status/{token}/events/live` (#31 — live updates).
 *
 * Native `EventSource` already reconnects, but with a short fixed delay; we
 * tear down on transport error and reconnect with our own backoff (matching
 * the admin live stream). Frames are the same `{ turn_id, sequence,
 * timestamp_ms, event_type, payload }` envelope shape — JSON-parsed and handed
 * to `onEvent` for the page to fold through the timeline reducer.
 *
 * Returns a disposer that cancels any pending reconnect and closes the live
 * connection.
 */
export function openStatusEventStream(
  token: string,
  opts: OpenStatusStreamOptions,
): () => void {
  const url = `${GATEWAY_BASE_URL}${statusPath(token)}/events/live`;

  let es: EventSource | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let retryIndex = 0;
  let disposed = false;

  const connect = (): void => {
    if (disposed) return;
    // No `withCredentials` — the token in the URL is the capability.
    es = new EventSource(url);

    es.onmessage = (raw: MessageEvent) => {
      let parsed: LiveEvent | null = null;
      try {
        parsed = JSON.parse(raw.data) as LiveEvent;
      } catch {
        return;
      }
      if (!parsed) return;
      // First frame after (re)connect — let the page drop its polling
      // fallback and reset the backoff window.
      if (retryIndex !== 0) retryIndex = 0;
      opts.onOpen?.();
      opts.onEvent(parsed);
    };

    es.onopen = () => {
      retryIndex = 0;
      opts.onOpen?.();
    };

    es.onerror = (err) => {
      opts.onError?.(err);
      if (disposed) return;
      es?.close();
      es = null;
      const delay = BACKOFF_MS[Math.min(retryIndex, BACKOFF_MS.length - 1)];
      retryIndex += 1;
      retryTimer = setTimeout(connect, delay);
    };
  };

  connect();

  return () => {
    disposed = true;
    if (retryTimer) clearTimeout(retryTimer);
    retryTimer = null;
    es?.close();
    es = null;
  };
}
