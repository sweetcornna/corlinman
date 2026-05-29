"use client";

import { ConfigOnlyChannelPage } from "@/components/channels/config-only/ConfigOnlyChannelPage";
import { fetchWechatOfficialStatus } from "@/lib/api/wechat_official";

/**
 * WeChat-Official channel admin — config + status only (no messages / send).
 * Thin binding over the shared `ConfigOnlyChannelPage` (see QQ for the
 * ChannelShell layout dialect).
 */
export default function WechatOfficialChannelPage() {
  return (
    <ConfigOnlyChannelPage
      channel="wechat_official"
      nsKey="channels.wechat_official.tp"
      testIdPrefix="wechat_official"
      fetchStatus={fetchWechatOfficialStatus}
    />
  );
}
