"use client";

import { FullInboxChannelPage } from "@/components/channels/full-inbox/FullInboxChannelPage";
import {
  fetchFeishuMessages,
  fetchFeishuStatus,
  sendFeishuTestMessage,
} from "@/lib/api/feishu";

/**
 * Feishu (Lark) channel admin — full-inbox surface (status + messages +
 * send). Thin binding over the shared `FullInboxChannelPage`.
 */
export default function FeishuChannelPage() {
  return (
    <FullInboxChannelPage
      channel="feishu"
      nsKey="channels.feishu.tp"
      testIdPrefix="feishu"
      fetchStatus={fetchFeishuStatus}
      fetchMessages={fetchFeishuMessages}
      sendTest={sendFeishuTestMessage}
    />
  );
}
