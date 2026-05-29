/**
 * Feishu (Lark) channel admin API client. Thin wrapper that binds the
 * `feishu` slug to the shared full-inbox transport in
 * `full-inbox-channel.ts`.
 *
 * Hits the live gateway at `/admin/channels/feishu/*`. Errors (404 / 503 /
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

export type FeishuMessage = FullInboxMessage;
export type FeishuStatusResponse = FullInboxStatusResponse;
export type FeishuSendRequest = FullInboxSendRequest;
export type FeishuSendResponse = FullInboxSendResponse;

export function fetchFeishuStatus(): Promise<FeishuStatusResponse> {
  return fetchChannelStatus("feishu");
}

export function fetchFeishuMessages(opts?: {
  limit?: number;
}): Promise<FeishuMessage[]> {
  return fetchChannelMessages("feishu", opts);
}

export function sendFeishuTestMessage(
  body: FeishuSendRequest,
): Promise<FeishuSendResponse> {
  return sendChannelTestMessage("feishu", body);
}
