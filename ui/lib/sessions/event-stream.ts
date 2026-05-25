/**
 * Live event stream client for `/admin/sessions/{key}/events/live`.
 *
 * Wraps native EventSource with two behaviours the default doesn't give us:
 *
 *   1. `Last-Event-ID` resume — composite event ids are `<turn_id>:<sequence>`.
 *      Browsers send `Last-Event-ID` automatically on auto-reconnect, but we
 *      tear the ES down on transport error so we can apply our own backoff;
 *      that means we have to set the header ourselves on the *new* ES. The
 *      native EventSource constructor doesn't accept custom headers — and we
 *      can't use it anyway because we also want to pass the last id as a
 *      query param fallback for proxies that strip the header. So the URL
 *      gets `?last_event_id=...` appended whenever we have one.
 *
 *   2. Exponential reconnect (3 → 6 → 12 → 30s) — matches `lib/sse.ts` style
 *      so a dead gateway doesn't hammer itself.
 *
 * Event payloads are JSON with shape
 *   { turn_id, sequence, timestamp_ms, event_type, payload }
 * matching the Wave 1 backend.
 */

import { GATEWAY_BASE_URL } from "@/lib/api";

/** Event types emitted by the gateway. Mirrors the 14 variants in
 *  `corlinman_gateway::observability::events::EventType`. */
export type LiveEventType =
  | "TurnStart"
  | "BlockStart"
  | "TextDelta"
  | "ReasoningDelta"
  | "ToolInputDelta"
  | "BlockStop"
  | "ToolStateRunning"
  | "ToolStateHeartbeat"
  | "ToolStateCompleted"
  | "SubagentSpawned"
  | "SubagentEvent"
  | "SubagentCompleted"
  | "Cancelling"
  | "TurnComplete"
  | "TurnErrored";

export interface LiveEvent<P = unknown> {
  turn_id: string;
  sequence: number;
  timestamp_ms: number;
  event_type: LiveEventType;
  payload: P;
}

export interface OpenLiveStreamOptions {
  onEvent: (ev: LiveEvent) => void;
  onError?: (err: Event) => void;
  /** When set, sent on the FIRST connect; later reconnects use whatever id
   *  we observed most recently. */
  initialLastEventId?: string;
}

const BACKOFF_MS = [3_000, 6_000, 12_000, 30_000] as const;

export function openLiveEventStream(
  sessionKey: string,
  opts: OpenLiveStreamOptions,
): () => void {
  const basePath = `/admin/sessions/${encodeURIComponent(sessionKey)}/events/live`;

  let es: EventSource | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let retryIndex = 0;
  let disposed = false;
  let lastEventId: string | undefined = opts.initialLastEventId;

  const connect = (): void => {
    if (disposed) return;

    const params = new URLSearchParams();
    if (lastEventId) params.set("last_event_id", lastEventId);
    const qs = params.toString();
    const url = `${GATEWAY_BASE_URL}${basePath}${qs ? `?${qs}` : ""}`;

    es = new EventSource(url, { withCredentials: true });

    es.onmessage = (raw: MessageEvent) => {
      let parsed: LiveEvent | null = null;
      try {
        parsed = JSON.parse(raw.data) as LiveEvent;
      } catch {
        return;
      }
      if (!parsed) return;
      lastEventId = raw.lastEventId || `${parsed.turn_id}:${parsed.sequence}`;
      retryIndex = 0;
      opts.onEvent(parsed);
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
