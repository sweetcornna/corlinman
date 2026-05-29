"use client";

import { FullInboxChannelPage } from "@/components/channels/full-inbox/FullInboxChannelPage";
import {
  fetchDiscordMessages,
  fetchDiscordStatus,
  sendDiscordTestMessage,
} from "@/lib/api/discord";

/**
 * Discord channel admin — full-inbox surface (status + messages + send).
 * Thin binding over the shared `FullInboxChannelPage` (see Telegram for the
 * design dialect).
 */
export default function DiscordChannelPage() {
  return (
    <FullInboxChannelPage
      channel="discord"
      nsKey="channels.discord.tp"
      testIdPrefix="discord"
      fetchStatus={fetchDiscordStatus}
      fetchMessages={fetchDiscordMessages}
      sendTest={sendDiscordTestMessage}
    />
  );
}
