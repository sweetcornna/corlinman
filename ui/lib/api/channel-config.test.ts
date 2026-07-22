/**
 * Tests for the corrected channel-config client. The earlier (rejected)
 * version sent a FLAT body that the backend's structured `ChannelConfigBody`
 * silently discarded — these tests pin the STRUCTURED `{secrets,urls,ids,
 * filters,flags}` wire shape and the "only changed fields" semantics.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  CHANNEL_CONFIG_SPEC,
  buildChannelConfigBody,
  isEmptyConfigBody,
  parseList,
  putChannelConfig,
  seedDraft,
  type ChannelConfigDraft,
} from "./channel-config";

interface RecordedCall {
  url: string;
  init: RequestInit & { method?: string; body?: BodyInit | null };
}

function stubFetch(status = 200, payload: unknown = { status: "ok", wrote: [], config_keys: {} }) {
  const calls: RecordedCall[] = [];
  const fn = vi.fn(async (url: string, init: RequestInit = {}) => {
    calls.push({ url: String(url), init });
    return new Response(JSON.stringify(payload), {
      status,
      headers: { "content-type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fn);
  return { calls };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("parseList", () => {
  it("splits on commas and newlines, trims, drops empties", () => {
    expect(parseList("a, b\nc ,, ")).toEqual(["a", "b", "c"]);
    expect(parseList("")).toEqual([]);
    expect(parseList(undefined)).toEqual([]);
  });
});

const keys = (fields: { key: string }[]) => fields.map((f) => f.key);

describe("CHANNEL_CONFIG_SPEC", () => {
  it("treats QQ NapCat URL as editable and WebUI token as secret", () => {
    expect(keys(CHANNEL_CONFIG_SPEC.qq.secrets)).toContain("napcat_access_token");
    expect(keys(CHANNEL_CONFIG_SPEC.qq.urls)).toContain("napcat_url");
  });

  it("marks every endpoint-override URL advanced (expert-only)", () => {
    const advancedUrls = Object.values(CHANNEL_CONFIG_SPEC).flatMap((spec) =>
      spec.urls.filter((f) => f.advanced).map((f) => f.key),
    );
    for (const key of [
      "ws_url",
      "napcat_url",
      "base_url",
      "webhook_url",
      "gateway_url",
      "rest_base",
      "api_base",
    ]) {
      expect(advancedUrls).toContain(key);
    }
    // app_id is a public client id, not an endpoint — stays visible.
    for (const ch of ["feishu", "wechat_official", "qq_official"] as const) {
      expect(
        CHANNEL_CONFIG_SPEC[ch].urls.find((f) => f.key === "app_id")?.advanced,
      ).toBeUndefined();
    }
  });

  it("surfaces the QQ group-behaviour keys with the right kinds", () => {
    const qq = CHANNEL_CONFIG_SPEC.qq;
    expect(keys(qq.flags)).toEqual([
      "group_replies_enabled",
      "freeze_risk_topic_blocking",
      "proactive_enabled",
    ]);
    expect(
      qq.flags.find((f) => f.key === "freeze_risk_topic_blocking")?.defaultValue,
    ).toBe(true);
    expect(keys(qq.ids)).toEqual(["self_ids", "group_whitelist", "proactive_groups"]);
    expect(qq.ids.find((f) => f.key === "self_ids")?.managed).toBe(true);
    expect(keys(qq.numbers)).toEqual([
      "group_reply_cooldown_secs",
      "proactive_min_gap_minutes",
      "proactive_max_gap_minutes",
      "proactive_daily_max",
      "proactive_active_start_hour",
      "proactive_active_end_hour",
    ]);
    // Every tuning number is expert-only.
    expect(qq.numbers.every((f) => f.advanced)).toBe(true);
    const policy = qq.urls.find((f) => f.key === "group_reply_policy");
    expect(policy?.input).toBe("select");
    expect(policy?.options).toEqual(["mention_or_keyword", "all"]);
    expect(policy?.advanced).toBeUndefined(); // basic field
    const prompt = qq.urls.find((f) => f.key === "proactive_prompt");
    expect(prompt?.input).toBe("textarea");
    expect(prompt?.advanced).toBe(true);
  });
});

describe("buildChannelConfigBody", () => {
  it("never emits the runtime-managed QQ self id", () => {
    const initial = seedDraft("qq", { self_ids: ["10001"] });
    const draft = {
      ...initial,
      ids: { ...initial.ids, self_ids: "20002", group_whitelist: "123" },
    };
    expect(buildChannelConfigBody("qq", draft, initial).ids).toEqual({
      group_whitelist: ["123"],
    });
  });

  it("seeds the QQ freeze-risk protection on when absent", () => {
    const draft = seedDraft("qq", {});
    expect(draft.flags.freeze_risk_topic_blocking).toBe(true);
    const off = { ...draft, flags: { ...draft.flags, freeze_risk_topic_blocking: false } };
    expect(buildChannelConfigBody("qq", off, draft).flags).toEqual({
      freeze_risk_topic_blocking: false,
    });
  });

  it("emits an empty body when nothing was edited", () => {
    const initial = seedDraft("telegram", {
      base_url: "https://api.telegram.org",
      allowed_chat_ids: ["1", "2"],
      keyword_filter: ["hi"],
      require_mention_in_groups: "true",
    });
    const body = buildChannelConfigBody("telegram", initial, initial);
    expect(isEmptyConfigBody(body)).toBe(true);
    expect(body).toEqual({});
  });

  it("includes only changed fields, grouped structurally", () => {
    const initial = seedDraft("telegram", {
      base_url: "https://api.telegram.org",
      allowed_chat_ids: ["1"],
      keyword_filter: [],
      require_mention_in_groups: "false",
    });
    const draft: ChannelConfigDraft = {
      secrets: { bot_token: "123:abc", secret_token: "" }, // typed one, blank one
      urls: { base_url: "https://proxy.example", webhook_url: "" },
      ids: { allowed_chat_ids: "1, 2, 3" },
      filters: { keyword_filter: "" },
      flags: { require_mention_in_groups: true, drop_pending_updates: false },
      numbers: {},
    };
    const body = buildChannelConfigBody("telegram", draft, initial);
    // structured groups
    expect(body.secrets).toEqual({ bot_token: "123:abc" }); // blank secret omitted
    expect(body.urls).toEqual({ base_url: "https://proxy.example" }); // webhook_url unchanged ("")
    expect(body.ids).toEqual({ allowed_chat_ids: ["1", "2", "3"] });
    expect(body.filters).toBeUndefined(); // unchanged
    expect(body.flags).toEqual({ require_mention_in_groups: true }); // only the toggled one
  });

  it("omits a secret left blank (keeps current on-disk value)", () => {
    const initial = seedDraft("discord", {});
    const draft = seedDraft("discord", {});
    const body = buildChannelConfigBody("discord", draft, initial);
    expect(body.secrets).toBeUndefined();
  });
});

describe("putChannelConfig", () => {
  it("PUTs the structured body to /admin/channels/{channel}/config", async () => {
    const { calls } = stubFetch(200, { status: "ok", wrote: ["urls.base_url"], config_keys: {} });
    const out = await putChannelConfig("telegram", {
      urls: { base_url: "https://proxy.example" },
    });
    expect(out.status).toBe("ok");
    expect(calls[0]?.url).toContain("/admin/channels/telegram/config");
    expect(calls[0]?.init.method).toBe("PUT");
    const sent = JSON.parse(String(calls[0]?.init.body ?? "{}"));
    expect(sent).toEqual({ urls: { base_url: "https://proxy.example" } });
  });

  it("rethrows on a non-2xx (e.g. unknown_field 400)", async () => {
    stubFetch(400, { error: "unknown_field" });
    await expect(
      putChannelConfig("telegram", { urls: { nope: "x" } as Record<string, string> }),
    ).rejects.toThrow();
  });
});
