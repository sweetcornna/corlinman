/**
 * Slack channel admin API client. Thin wrapper that binds the `slack` slug
 * to the shared full-inbox transport in `full-inbox-channel.ts`.
 *
 * Hits the live gateway at `/admin/channels/slack/*`. Errors (404 / 503 /
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

export type SlackMessage = FullInboxMessage;
export type SlackStatusResponse = FullInboxStatusResponse;
export type SlackSendRequest = FullInboxSendRequest;
export type SlackSendResponse = FullInboxSendResponse;

export function fetchSlackStatus(): Promise<SlackStatusResponse> {
  return fetchChannelStatus("slack");
}

export function fetchSlackMessages(opts?: {
  limit?: number;
}): Promise<SlackMessage[]> {
  return fetchChannelMessages("slack", opts);
}

export function sendSlackTestMessage(
  body: SlackSendRequest,
): Promise<SlackSendResponse> {
  return sendChannelTestMessage("slack", body);
}
