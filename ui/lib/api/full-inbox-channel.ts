/**
 * Shared admin API client for the "full-inbox" channels — Discord, Slack,
 * Feishu. These three expose a uniform surface behind admin auth:
 *
 *   GET  /admin/channels/{ch}/status            → FullInboxStatusResponse
 *   GET  /admin/channels/{ch}/messages?limit=N  → FullInboxMessage[]
 *   POST /admin/channels/{ch}/send              → FullInboxSendResponse
 *
 * The per-channel modules (`discord.ts` / `slack.ts` / `feishu.ts`) are thin
 * wrappers that bind the channel slug — mirroring how `telegram.ts` exposes
 * named fetchers, but factored to avoid copy-pasting the transport three
 * times. Types match `ChannelStatusOut` / `ChannelMessagesOut` /
 * `ChannelSendBody` in `routes_admin_a/channels.py` field-for-field.
 *
 * Errors (404 / 503 / network) propagate so the page renders its offline /
 * disabled banner against `statusQuery.isError`.
 */

import { apiFetch } from "@/lib/api";

/* ------------------------------------------------------------------ */
/*                           Public types                             */
/* ------------------------------------------------------------------ */

export type FullInboxChannel = "discord" | "slack" | "feishu";

/**
 * NON-SECRET config projection surfaced by the backend. Values are either a
 * plain string (e.g. `app_id`, `respond_to_all`) or a list of strings (e.g.
 * `allowed_channel_ids`, `keyword_filter`). Tokens / secrets are never here.
 */
export type ChannelConfigKeys = Record<string, string | string[]>;

export interface FullInboxStatusResponse {
  configured: boolean;
  enabled: boolean;
  online: boolean;
  last_event_at_ms: number | null;
  received: number;
  sent: number;
  errors: number;
  error_message: string | null;
  config_keys: ChannelConfigKeys;
}

export interface FullInboxMessage {
  id: string;
  kind: "group" | "mention";
  chat_id: string;
  chat_title: string | null;
  from_username?: string;
  content?: string;
  /** Epoch ms when the message landed. */
  timestamp_ms: number;
  routing: "queued" | "responded";
  mention_reason: "mention" | "none";
}

export interface FullInboxSendRequest {
  /** Any one of these may carry the target id; the backend resolves them. */
  target_id?: string;
  chat_id?: string;
  channel_id?: string;
  text: string;
}

export interface FullInboxSendResponse {
  ok: boolean;
  message_id: string;
}

/* ------------------------------------------------------------------ */
/*                            Public fetches                          */
/* ------------------------------------------------------------------ */

/** Fetches gateway status + non-secret config for the given channel. */
export async function fetchChannelStatus(
  channel: FullInboxChannel,
): Promise<FullInboxStatusResponse> {
  return apiFetch<FullInboxStatusResponse>(
    `/admin/channels/${channel}/status`,
  );
}

/**
 * Fetches the recent-messages list (newest first), capped at `limit`
 * (1..200). The backend returns `{ messages: [...] }`; we unwrap to the
 * array so callers get the same shape as `fetchTelegramMessages`.
 */
export async function fetchChannelMessages(
  channel: FullInboxChannel,
  opts?: { limit?: number },
): Promise<FullInboxMessage[]> {
  const limit = opts?.limit ?? 20;
  const qs = new URLSearchParams({ limit: String(limit) }).toString();
  const res = await apiFetch<{ messages: FullInboxMessage[] }>(
    `/admin/channels/${channel}/messages?${qs}`,
  );
  return res.messages ?? [];
}

/**
 * Sends a test message via the gateway. Errors propagate; the caller is
 * expected to toast the failure (the `SendTestDrawer` handles this).
 */
export async function sendChannelTestMessage(
  channel: FullInboxChannel,
  body: FullInboxSendRequest,
): Promise<FullInboxSendResponse> {
  return apiFetch<FullInboxSendResponse>(`/admin/channels/${channel}/send`, {
    method: "POST",
    body,
  });
}

/* ------------------------------------------------------------------ */
/*                       Config-only channels                         */
/* ------------------------------------------------------------------ */

export type ConfigOnlyChannel = "wechat_official" | "qq_official";

/**
 * Status envelope for the config-only channels (WeChat-Official /
 * QQ-Official). `online` is always false (no live runtime) and
 * `last_event_at_ms` is always null. Matches `ChannelConfigStatusOut`.
 */
export interface ConfigOnlyStatusResponse {
  configured: boolean;
  enabled: boolean;
  online: boolean;
  last_event_at_ms: number | null;
  error_message: string | null;
  config_keys: ChannelConfigKeys;
}

/** Fetches the config + status envelope for a config-only channel. */
export async function fetchConfigOnlyStatus(
  channel: ConfigOnlyChannel,
): Promise<ConfigOnlyStatusResponse> {
  return apiFetch<ConfigOnlyStatusResponse>(
    `/admin/channels/${channel}/status`,
  );
}
