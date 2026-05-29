/**
 * Discord channel admin API client. Thin wrapper that binds the `discord`
 * slug to the shared full-inbox transport in `full-inbox-channel.ts`.
 *
 * Hits the live gateway at `/admin/channels/discord/*`. Errors (404 / 503 /
 * network) propagate so the page renders its offline / disabled banner.
 */

import {
  fetchChannelMessages,
  fetchChannelStatus,
  sendChannelTestMessage,
  type FullInboxMessage,
  type FullInboxSendRequest,
  type FullInboxSendResponse,
  type FullInboxStatusResponse,
} from "./full-inbox-channel";

export type DiscordMessage = FullInboxMessage;
export type DiscordStatusResponse = FullInboxStatusResponse;
export type DiscordSendRequest = FullInboxSendRequest;
export type DiscordSendResponse = FullInboxSendResponse;

export function fetchDiscordStatus(): Promise<DiscordStatusResponse> {
  return fetchChannelStatus("discord");
}

export function fetchDiscordMessages(opts?: {
  limit?: number;
}): Promise<DiscordMessage[]> {
  return fetchChannelMessages("discord", opts);
}

export function sendDiscordTestMessage(
  body: DiscordSendRequest,
): Promise<DiscordSendResponse> {
  return sendChannelTestMessage("discord", body);
}
