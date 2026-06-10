/**
 * Admin client for the per-channel config-WRITE route:
 *
 *   PUT /admin/channels/{channel}/config  → ChannelConfigOut
 *
 * The backend (`routes_admin_a/channels.py`) expects a STRUCTURED body —
 * `{ secrets, urls, ids, filters, flags }` — validated against a per-channel
 * editable-field spec; any key not in the spec is rejected `unknown_field`.
 * Secrets honour the `***REDACTED***`-or-omit convention: an omitted secret
 * key keeps the current on-disk value, so the editor simply leaves blank
 * secret inputs out of the payload.
 *
 * `CHANNEL_CONFIG_SPEC` mirrors `_CHANNEL_EDITABLE` field-for-field (the
 * backend's `int_list_keys` + `str_list_keys` are merged here into `ids`,
 * since the wire shape groups both under `ids` and the backend coerces
 * int lists from string values).
 */

import { apiFetch } from "@/lib/api";
import type { ChannelConfigKeys } from "@/lib/api/full-inbox-channel";

export type ConfigEditableChannel =
  | "qq"
  | "telegram"
  | "discord"
  | "slack"
  | "feishu"
  | "wechat_official"
  | "qq_official";

export interface ChannelConfigSpec {
  secrets: string[];
  urls: string[];
  ids: string[];
  filters: string[];
  flags: string[];
}

/** Mirror of the backend `_CHANNEL_EDITABLE` spec. Keep in sync. */
export const CHANNEL_CONFIG_SPEC: Record<ConfigEditableChannel, ChannelConfigSpec> = {
  qq: {
    secrets: ["access_token", "napcat_access_token"],
    urls: ["ws_url", "napcat_url"],
    ids: ["self_ids"],
    filters: [],
    flags: [],
  },
  telegram: {
    secrets: ["bot_token", "secret_token"],
    urls: ["base_url", "webhook_url"],
    ids: ["allowed_chat_ids"],
    filters: ["keyword_filter"],
    flags: ["require_mention_in_groups", "drop_pending_updates"],
  },
  discord: {
    secrets: ["bot_token"],
    urls: ["gateway_url", "rest_base"],
    ids: ["allowed_channel_ids"],
    filters: ["keyword_filter"],
    flags: ["respond_to_all"],
  },
  slack: {
    secrets: ["app_token", "bot_token"],
    urls: ["api_base"],
    ids: ["allowed_channel_ids"],
    filters: ["keyword_filter"],
    flags: ["respond_to_all"],
  },
  feishu: {
    secrets: ["app_secret"],
    urls: ["app_id", "api_base"],
    ids: ["allowed_chat_ids"],
    filters: ["keyword_filter"],
    flags: ["respond_to_all"],
  },
  wechat_official: {
    secrets: ["app_secret", "token"],
    urls: ["app_id", "api_base"],
    ids: [],
    filters: [],
    flags: [],
  },
  qq_official: {
    secrets: ["app_secret"],
    urls: ["app_id", "api_base"],
    ids: ["intents"],
    filters: [],
    flags: ["sandbox"],
  },
};

export interface ChannelConfigBody {
  secrets?: Record<string, string>;
  urls?: Record<string, string>;
  ids?: Record<string, string[]>;
  filters?: Record<string, string[]>;
  flags?: Record<string, boolean>;
}

export interface ChannelConfigOut {
  status: string;
  wrote: string[];
  config_keys: ChannelConfigKeys;
}

/** Editor's local draft. `secrets` start blank (blank = keep current). The
 * list-bearing groups (`ids`/`filters`) are held as raw text the operator
 * types (comma/newline separated) and parsed at submit. */
export interface ChannelConfigDraft {
  secrets: Record<string, string>;
  urls: Record<string, string>;
  ids: Record<string, string>;
  filters: Record<string, string>;
  flags: Record<string, boolean>;
}

/** Split a comma/newline-separated text field into a trimmed, non-empty list. */
export function parseList(raw: string | undefined): string[] {
  if (!raw) return [];
  return raw
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

function listEq(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

/** Seed a draft from the channel's current non-secret config keys. Secrets
 * always start blank (the backend never echoes them). */
export function seedDraft(
  channel: ConfigEditableChannel,
  configKeys: ChannelConfigKeys,
): ChannelConfigDraft {
  const spec = CHANNEL_CONFIG_SPEC[channel];
  const asStr = (v: string | string[] | undefined): string =>
    Array.isArray(v) ? v.join(", ") : v == null ? "" : String(v);
  const draft: ChannelConfigDraft = { secrets: {}, urls: {}, ids: {}, filters: {}, flags: {} };
  for (const k of spec.secrets) draft.secrets[k] = "";
  for (const k of spec.urls) draft.urls[k] = asStr(configKeys[k]);
  for (const k of spec.ids) draft.ids[k] = asStr(configKeys[k]);
  for (const k of spec.filters) draft.filters[k] = asStr(configKeys[k]);
  for (const k of spec.flags) {
    // The status route emits bool keys as the string "true"/"false"
    // (str(bool) on the backend), so compare against the string forms.
    const v = configKeys[k];
    draft.flags[k] = v === "true" || v === "True";
  }
  return draft;
}

/**
 * Build the STRUCTURED config body, including ONLY the fields the operator
 * actually changed (so an untouched field is never written). Returns an
 * empty object when nothing was edited.
 */
export function buildChannelConfigBody(
  channel: ConfigEditableChannel,
  draft: ChannelConfigDraft,
  initial: ChannelConfigDraft,
): ChannelConfigBody {
  const spec = CHANNEL_CONFIG_SPEC[channel];
  const body: ChannelConfigBody = {};

  const secrets: Record<string, string> = {};
  for (const k of spec.secrets) {
    const v = (draft.secrets[k] ?? "").trim();
    if (v) secrets[k] = v; // blank = keep current (omit)
  }
  if (Object.keys(secrets).length) body.secrets = secrets;

  const urls: Record<string, string> = {};
  for (const k of spec.urls) {
    if ((draft.urls[k] ?? "") !== (initial.urls[k] ?? "")) urls[k] = draft.urls[k] ?? "";
  }
  if (Object.keys(urls).length) body.urls = urls;

  const ids: Record<string, string[]> = {};
  for (const k of spec.ids) {
    const next = parseList(draft.ids[k]);
    if (!listEq(next, parseList(initial.ids[k]))) ids[k] = next;
  }
  if (Object.keys(ids).length) body.ids = ids;

  const filters: Record<string, string[]> = {};
  for (const k of spec.filters) {
    const next = parseList(draft.filters[k]);
    if (!listEq(next, parseList(initial.filters[k]))) filters[k] = next;
  }
  if (Object.keys(filters).length) body.filters = filters;

  const flags: Record<string, boolean> = {};
  for (const k of spec.flags) {
    if (!!draft.flags[k] !== !!initial.flags[k]) flags[k] = !!draft.flags[k];
  }
  if (Object.keys(flags).length) body.flags = flags;

  return body;
}

/** True when the body carries no changes. */
export function isEmptyConfigBody(body: ChannelConfigBody): boolean {
  return !body.secrets && !body.urls && !body.ids && !body.filters && !body.flags;
}

/** PUT the structured config body. Returns the echoed non-secret keys. */
export async function putChannelConfig(
  channel: ConfigEditableChannel,
  body: ChannelConfigBody,
): Promise<ChannelConfigOut> {
  return apiFetch<ChannelConfigOut>(`/admin/channels/${channel}/config`, {
    method: "PUT",
    body,
  });
}
