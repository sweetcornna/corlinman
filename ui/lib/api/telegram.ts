/**
 * Telegram channel admin API client.
 *
 * Hits the live gateway at `/admin/channels/telegram/*` — the three
 * routes are now shipped by `routes_admin_a/channels.py` and back the
 * page's stat chips with real numbers (W4-FE F2).
 *
 * The `TelegramMessage` shape mirrors what the backend returns: `kind`,
 * `routing`, `mention_reason`, optional media descriptor.
 *
 * `TelegramConfig` / `TelegramStats` are re-exported from the legacy
 * mock module (`@/lib/mocks/telegram`) ONLY for the shared type
 * definitions — no mock data is fetched at runtime. A 404 / 503 from
 * the gateway propagates to the caller so the page's existing offline
 * banner renders (`statusQuery.isError` → the "Webhook is offline" path
 * in `telegram/page.tsx`).
 */

import { apiFetch } from "@/lib/api";
import type {
  TelegramConfig as MockTelegramConfig,
  TelegramStats as MockTelegramStats,
} from "@/lib/mocks/telegram";

/* ------------------------------------------------------------------ */
/*                           Public types                             */
/* ------------------------------------------------------------------ */

export type TelegramConfig = MockTelegramConfig;
export type TelegramStats = MockTelegramStats;

export interface TelegramMedia {
  kind: "photo" | "voice" | "document";
  /** Path under the gateway media cache. UI renders a thumbnail / preview. */
  local_path: string;
  mime?: string;
  size_bytes?: number;
  /** Voice clips: playback duration in seconds (used for the waveform chip). */
  duration_sec?: number;
  /** Documents: original filename (used in the row label). */
  filename?: string;
}

export interface TelegramMessage {
  id: string;
  kind: "private" | "group";
  chat_id: string;
  chat_title?: string;
  from_username?: string;
  content?: string;
  media?: TelegramMedia;
  /** Epoch ms when the message landed. */
  timestamp_ms: number;
  /** Countdown to reply SLA, if a reply is still in flight. */
  reply_deadline_ms?: number;
  reply_total_ms?: number;
  routing: "responded" | "ignored" | "queued";
  mention_reason?: "dm" | "mention" | "reply_to_bot" | "none";
}

export interface TelegramStatusResponse {
  config: TelegramConfig;
  stats: TelegramStats;
  connected: boolean;
  runtime?: "connected" | "disconnected" | "unknown";
  last_error?: string | null;
  last_webhook_payload?: Record<string, unknown> | null;
}

export interface TelegramSendRequest {
  chat_id: string;
  text: string;
}

export interface TelegramSendResponse {
  status: "ok" | "error";
  message_id?: number;
  error?: string;
}

/* ------------------------------------------------------------------ */
/*                            Public fetches                          */
/* ------------------------------------------------------------------ */

/**
 * Fetches gateway status + config for the Telegram channel.
 * Errors (404 / 503 / network) propagate — the page renders its
 * existing offline panel against `statusQuery.isError`.
 */
export async function fetchTelegramStatus(): Promise<TelegramStatusResponse> {
  return apiFetch<TelegramStatusResponse>("/admin/channels/telegram/status");
}

/**
 * Fetches the recent-messages list. Returns up to `limit` entries
 * (newest first).
 */
export async function fetchTelegramMessages(opts?: {
  limit?: number;
}): Promise<TelegramMessage[]> {
  const limit = opts?.limit ?? 20;
  const qs = new URLSearchParams({ limit: String(limit) }).toString();
  return apiFetch<TelegramMessage[]>(
    `/admin/channels/telegram/messages?${qs}`,
  );
}

/**
 * Sends a test message via the gateway. Errors propagate; the caller
 * is expected to toast the failure (the `SendTestDrawer` handles this).
 */
export async function sendTelegramTestMessage(
  body: TelegramSendRequest,
): Promise<TelegramSendResponse> {
  return apiFetch<TelegramSendResponse>("/admin/channels/telegram/send", {
    method: "POST",
    body,
  });
}

/**
 * Legacy test-suite hook — the 404→mock fallback path was removed in
 * W4-FE F2 so there's no log state to reset, but `page.test.tsx`
 * imports this in `beforeEach` and we keep the export as a no-op to
 * avoid churning the test file.
 */
export function __resetTelegramFallbackLog(): void {
  // intentionally empty — the fallback path no longer exists.
}
