/**
 * Admin client for the per-channel config-WRITE route:
 *
 *   PUT /admin/channels/{channel}/config  → ChannelConfigOut
 *
 * The backend (`routes_admin_a/channels.py`) expects a STRUCTURED body —
 * `{ secrets, urls, ids, filters, flags, numbers }` — validated against a
 * per-channel editable-field spec; any key not in the spec is rejected
 * `unknown_field`. Secrets honour the `***REDACTED***`-or-omit convention:
 * an omitted secret key keeps the current on-disk value, so the editor
 * simply leaves blank secret inputs out of the payload.
 *
 * `CHANNEL_CONFIG_SPEC` mirrors `_CHANNEL_EDITABLE` field-for-field (the
 * backend's `int_list_keys` + `str_list_keys` are merged here into `ids`,
 * since the wire shape groups both under `ids` and the backend coerces
 * int lists from string values). Wire-group semantics per field kind:
 *
 * - `urls` is the backend's PLAIN-STRING group (`url_keys`) — endpoint
 *   overrides, public client ids (`app_id`), and now also enum/text
 *   strings (`group_reply_policy` / `proactive_prompt`), all written
 *   verbatim as strings.
 * - `numbers` is the TYPED numeric group: values cross the wire as JSON
 *   numbers (never stringified) so the TOML round-trips typed.
 * - `flags` are typed booleans; `ids`/`filters` are string lists (the
 *   backend coerces int lists).
 *
 * Each field is a `ChannelFieldSpec`; `advanced: true` marks expert-only
 * fields the editor folds behind its "advanced" disclosure (endpoint
 * overrides have sane adapter defaults; tuning numbers are niche).
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

/** One editable field. The declarative `advanced` flag drives the editor's
 * disclosure split — no per-channel render logic. */
export interface ChannelFieldSpec {
  key: string;
  /** Expert-only: rendered inside the collapsed "advanced" section. */
  advanced?: boolean;
  /** Widget override for plain-string (`urls` group) fields. */
  input?: "select" | "textarea";
  /** Allowed values for `input: "select"`. FIRST option = adapter default
   * (used to seed the select when the key is absent on disk). */
  options?: readonly string[];
  /** Adapter default, shown as the input placeholder (number fields). */
  placeholder?: string;
}

export interface ChannelConfigSpec {
  secrets: ChannelFieldSpec[];
  /** Plain-string wire group (`url_keys`): endpoints, app_id, enum/text. */
  urls: ChannelFieldSpec[];
  ids: ChannelFieldSpec[];
  filters: ChannelFieldSpec[];
  flags: ChannelFieldSpec[];
  /** Typed numeric wire group — sent as JSON numbers. */
  numbers: ChannelFieldSpec[];
}

/** Mirror of the backend `_CHANNEL_EDITABLE` spec. Keep in sync. */
export const CHANNEL_CONFIG_SPEC: Record<ConfigEditableChannel, ChannelConfigSpec> = {
  qq: {
    // Both tokens only matter when pointing at an EXTERNAL NapCat — the
    // bundled NapCat needs neither, so they fold behind "advanced" next to
    // the endpoint overrides they authenticate (ws_url / napcat_url).
    secrets: [
      { key: "access_token", advanced: true },
      { key: "napcat_access_token", advanced: true },
    ],
    urls: [
      { key: "ws_url", advanced: true },
      { key: "napcat_url", advanced: true },
      {
        key: "group_reply_policy",
        input: "select",
        options: ["mention_or_keyword", "all"],
      },
      { key: "proactive_prompt", input: "textarea", advanced: true },
    ],
    ids: [
      { key: "self_ids" },
      { key: "group_whitelist" },
      { key: "proactive_groups", advanced: true },
    ],
    filters: [],
    flags: [{ key: "group_replies_enabled" }, { key: "proactive_enabled" }],
    numbers: [
      { key: "group_reply_cooldown_secs", advanced: true, placeholder: "20" },
      { key: "proactive_min_gap_minutes", advanced: true, placeholder: "45" },
      { key: "proactive_max_gap_minutes", advanced: true, placeholder: "180" },
      { key: "proactive_daily_max", advanced: true, placeholder: "4" },
      { key: "proactive_active_start_hour", advanced: true, placeholder: "9" },
      { key: "proactive_active_end_hour", advanced: true, placeholder: "23" },
    ],
  },
  telegram: {
    secrets: [{ key: "bot_token" }, { key: "secret_token" }],
    urls: [
      { key: "base_url", advanced: true },
      { key: "webhook_url", advanced: true },
    ],
    ids: [{ key: "allowed_chat_ids" }],
    filters: [{ key: "keyword_filter" }],
    flags: [{ key: "require_mention_in_groups" }, { key: "drop_pending_updates" }],
    numbers: [],
  },
  discord: {
    secrets: [{ key: "bot_token" }],
    urls: [
      { key: "gateway_url", advanced: true },
      { key: "rest_base", advanced: true },
    ],
    ids: [{ key: "allowed_channel_ids" }],
    filters: [{ key: "keyword_filter" }],
    flags: [{ key: "respond_to_all" }],
    numbers: [],
  },
  slack: {
    secrets: [{ key: "app_token" }, { key: "bot_token" }],
    urls: [{ key: "api_base", advanced: true }],
    ids: [{ key: "allowed_channel_ids" }],
    filters: [{ key: "keyword_filter" }],
    flags: [{ key: "respond_to_all" }],
    numbers: [],
  },
  feishu: {
    secrets: [{ key: "app_secret" }],
    urls: [{ key: "app_id" }, { key: "api_base", advanced: true }],
    ids: [{ key: "allowed_chat_ids" }],
    filters: [{ key: "keyword_filter" }],
    flags: [{ key: "respond_to_all" }],
    numbers: [],
  },
  wechat_official: {
    secrets: [{ key: "app_secret" }, { key: "token" }],
    urls: [{ key: "app_id" }, { key: "api_base", advanced: true }],
    ids: [],
    filters: [],
    flags: [],
    numbers: [],
  },
  qq_official: {
    secrets: [{ key: "app_secret" }],
    urls: [{ key: "app_id" }, { key: "api_base", advanced: true }],
    ids: [{ key: "intents" }],
    filters: [],
    flags: [{ key: "sandbox" }],
    numbers: [],
  },
};

/** True when any field of the channel is marked `advanced`. */
export function specHasAdvanced(spec: ChannelConfigSpec): boolean {
  return [
    ...spec.secrets,
    ...spec.urls,
    ...spec.ids,
    ...spec.filters,
    ...spec.flags,
    ...spec.numbers,
  ].some((f) => f.advanced === true);
}

export interface ChannelConfigBody {
  secrets?: Record<string, string>;
  urls?: Record<string, string>;
  ids?: Record<string, string[]>;
  filters?: Record<string, string[]>;
  flags?: Record<string, boolean>;
  /** Typed numbers — JSON numbers on the wire, never stringified. */
  numbers?: Record<string, number>;
}

export interface ChannelConfigOut {
  status: string;
  wrote: string[];
  config_keys: ChannelConfigKeys;
}

/** Editor's local draft. `secrets` start blank (blank = keep current). The
 * list-bearing groups (`ids`/`filters`) are held as raw text the operator
 * types (comma/newline separated) and parsed at submit; `numbers` are held
 * as raw text too and parsed to typed numbers at submit. */
export interface ChannelConfigDraft {
  secrets: Record<string, string>;
  urls: Record<string, string>;
  ids: Record<string, string>;
  filters: Record<string, string>;
  flags: Record<string, boolean>;
  numbers: Record<string, string>;
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
 * always start blank (the backend never echoes them). Select fields with
 * no on-disk value seed to their first option (= the adapter default), so
 * the widget always shows a real value without marking the form dirty. */
export function seedDraft(
  channel: ConfigEditableChannel,
  configKeys: ChannelConfigKeys,
): ChannelConfigDraft {
  const spec = CHANNEL_CONFIG_SPEC[channel];
  const asStr = (v: string | string[] | undefined): string =>
    Array.isArray(v) ? v.join(", ") : v == null ? "" : String(v);
  const draft: ChannelConfigDraft = {
    secrets: {},
    urls: {},
    ids: {},
    filters: {},
    flags: {},
    numbers: {},
  };
  for (const f of spec.secrets) draft.secrets[f.key] = "";
  for (const f of spec.urls) {
    const v = asStr(configKeys[f.key]);
    draft.urls[f.key] = !v && f.options?.length ? f.options[0] : v;
  }
  for (const f of spec.ids) draft.ids[f.key] = asStr(configKeys[f.key]);
  for (const f of spec.filters) draft.filters[f.key] = asStr(configKeys[f.key]);
  for (const f of spec.flags) {
    // The status route emits bool keys as the string "true"/"false"
    // (str(bool) on the backend), so compare against the string forms.
    const v = configKeys[f.key];
    draft.flags[f.key] = v === "true" || v === "True";
  }
  for (const f of spec.numbers) draft.numbers[f.key] = asStr(configKeys[f.key]);
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
  for (const f of spec.secrets) {
    const v = (draft.secrets[f.key] ?? "").trim();
    if (v) secrets[f.key] = v; // blank = keep current (omit)
  }
  if (Object.keys(secrets).length) body.secrets = secrets;

  const urls: Record<string, string> = {};
  for (const f of spec.urls) {
    if ((draft.urls[f.key] ?? "") !== (initial.urls[f.key] ?? "")) {
      urls[f.key] = draft.urls[f.key] ?? "";
    }
  }
  if (Object.keys(urls).length) body.urls = urls;

  const ids: Record<string, string[]> = {};
  for (const f of spec.ids) {
    const next = parseList(draft.ids[f.key]);
    if (!listEq(next, parseList(initial.ids[f.key]))) ids[f.key] = next;
  }
  if (Object.keys(ids).length) body.ids = ids;

  const filters: Record<string, string[]> = {};
  for (const f of spec.filters) {
    const next = parseList(draft.filters[f.key]);
    if (!listEq(next, parseList(initial.filters[f.key]))) filters[f.key] = next;
  }
  if (Object.keys(filters).length) body.filters = filters;

  const flags: Record<string, boolean> = {};
  for (const f of spec.flags) {
    if (!!draft.flags[f.key] !== !!initial.flags[f.key]) {
      flags[f.key] = !!draft.flags[f.key];
    }
  }
  if (Object.keys(flags).length) body.flags = flags;

  const numbers: Record<string, number> = {};
  for (const f of spec.numbers) {
    const raw = (draft.numbers[f.key] ?? "").trim();
    const before = (initial.numbers[f.key] ?? "").trim();
    if (raw === before) continue;
    // Blank = leave the on-disk value alone (this route can't unset keys);
    // non-numeric input never reaches the wire.
    if (!raw) continue;
    const n = Number(raw);
    if (!Number.isFinite(n)) continue;
    numbers[f.key] = n;
  }
  if (Object.keys(numbers).length) body.numbers = numbers;

  return body;
}

/** True when the body carries no changes. */
export function isEmptyConfigBody(body: ChannelConfigBody): boolean {
  return (
    !body.secrets &&
    !body.urls &&
    !body.ids &&
    !body.filters &&
    !body.flags &&
    !body.numbers
  );
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
