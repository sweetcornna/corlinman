/**
 * WeChat-Official channel admin API client (config + status only — no
 * messages / send). Thin wrapper that binds the `wechat_official` slug to
 * the shared config-only transport in `full-inbox-channel.ts`.
 *
 * Hits `/admin/channels/wechat_official/status`. `online` is always false
 * (no live runtime); errors propagate so the page can render its offline
 * banner.
 */

import {
  fetchConfigOnlyStatus,
  type ConfigOnlyStatusResponse,
} from "./full-inbox-channel";

export type WechatOfficialStatusResponse = ConfigOnlyStatusResponse;

export function fetchWechatOfficialStatus(): Promise<WechatOfficialStatusResponse> {
  return fetchConfigOnlyStatus("wechat_official");
}
