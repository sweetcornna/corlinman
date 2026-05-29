"use client";

import { ConfigOnlyChannelPage } from "@/components/channels/config-only/ConfigOnlyChannelPage";
import { fetchQqOfficialStatus } from "@/lib/api/qq_official";

/**
 * QQ-Official Bot channel admin — config + status only (no messages / send).
 * Thin binding over the shared `ConfigOnlyChannelPage`. Non-secret config
 * surfaced: app_id · intents[] · sandbox.
 */
export default function QqOfficialChannelPage() {
  return (
    <ConfigOnlyChannelPage
      channel="qq_official"
      nsKey="channels.qq_official.tp"
      testIdPrefix="qq_official"
      fetchStatus={fetchQqOfficialStatus}
    />
  );
}
