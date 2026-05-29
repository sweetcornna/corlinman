/**
 * QQ-Official Bot channel admin API client (config + status only — no
 * messages / send). Thin wrapper that binds the `qq_official` slug to the
 * shared config-only transport in `full-inbox-channel.ts`.
 *
 * Hits `/admin/channels/qq_official/status`. `online` is always false (no
 * live runtime); errors propagate so the page can render its offline
 * banner. NON-SECRET config keys surfaced: `app_id`, `intents[]`, `sandbox`.
 */

import {
  fetchConfigOnlyStatus,
  type ConfigOnlyStatusResponse,
} from "./full-inbox-channel";

export type QqOfficialStatusResponse = ConfigOnlyStatusResponse;

export function fetchQqOfficialStatus(): Promise<QqOfficialStatusResponse> {
  return fetchConfigOnlyStatus("qq_official");
}
