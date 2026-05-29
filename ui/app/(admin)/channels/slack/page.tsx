"use client";

import { FullInboxChannelPage } from "@/components/channels/full-inbox/FullInboxChannelPage";
import {
  fetchSlackMessages,
  fetchSlackStatus,
  sendSlackTestMessage,
} from "@/lib/api/slack";

/**
 * Slack channel admin — full-inbox surface (status + messages + send).
 * Thin binding over the shared `FullInboxChannelPage`.
 */
export default function SlackChannelPage() {
  return (
    <FullInboxChannelPage
      channel="slack"
      nsKey="channels.slack.tp"
      testIdPrefix="slack"
      fetchStatus={fetchSlackStatus}
      fetchMessages={fetchSlackMessages}
      sendTest={sendSlackTestMessage}
    />
  );
}
