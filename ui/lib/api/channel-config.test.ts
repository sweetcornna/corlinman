/**
 * Tests for the corrected channel-config client. The earlier (rejected)
 * version sent a FLAT body that the backend's structured `ChannelConfigBody`
 * silently discarded — these tests pin the STRUCTURED `{secrets,urls,ids,
 * filters,flags}` wire shape and the "only changed fields" semantics.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import {
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

describe("buildChannelConfigBody", () => {
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
