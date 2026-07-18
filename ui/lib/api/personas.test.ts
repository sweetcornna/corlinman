/**
 * Personas API client corpus.
 *
 * Covers:
 *   - URL builders (`personaPath`, `PERSONAS_LIST_PATH`, `QQ_HUMANLIKE_PATH`)
 *   - `fetchPersonas` 200 happy path + missing-envelope tolerance + 500 rethrow
 *   - `fetchPersona` 200 / 404 (returns `null`) / 500 rethrow
 *   - `createPersona` 201 happy path + body shape
 *   - `updatePersona` PATCH method + partial body shape
 *   - `deletePersona` 204 / 404 (returns `"builtin_protected"`) / 500 rethrow
 *   - `fetchQqHumanlike` 200 happy path
 *   - `setQqHumanlike` PUT method + body round-trip
 *
 * Mirrors the discipline of `sessions.test.ts`: stub `globalThis.fetch`
 * with a recorder so we can both inspect what the client *sent* and
 * stage what the server replied with.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ASSET_LABEL_RE,
  AssetUploadError,
  PERSONAS_LIST_PATH,
  QQ_HUMANLIKE_PATH,
  SUPPORTED_HUMANLIKE_CHANNELS,
  createPersona,
  deletePersona,
  fetchDiary,
  fetchHumanlike,
  fetchLifeSeeds,
  fetchLifeState,
  fetchPersona,
  fetchPersonas,
  fetchQqHumanlike,
  humanlikePath,
  patchLifeState,
  personaPath,
  putLifeSeeds,
  renameAsset,
  resetPersonaToDefault,
  runPersonaDecay,
  setHumanlike,
  setQqHumanlike,
  slugifyAssetLabel,
  updatePersona,
} from "./personas";

type FetchInit = RequestInit & { method?: string; body?: BodyInit | null };

interface RecordedCall {
  url: string;
  init: FetchInit;
}

function makeFetchStub(
  responder: (init: FetchInit) => Response | Promise<Response>,
): { fn: ReturnType<typeof vi.fn>; calls: RecordedCall[] } {
  const calls: RecordedCall[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const safeInit = (init ?? {}) as FetchInit;
    calls.push({ url, init: safeInit });
    return responder(safeInit);
  });
  return { fn, calls };
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function noContent(): Response {
  return new Response(null, { status: 204 });
}

/** Stable sample row — all the shape tests reuse this so a contract drift
 * shows up in one place. */
const SAMPLE_PERSONA = {
  id: "grantley",
  display_name: "格兰特利·贝尔",
  short_summary: "贝尔家的次子。语气直白、会自嘲。",
  system_prompt: "# Grantley\nYou are Grantley Bell …",
  is_builtin: true,
  created_at_ms: 1_777_500_000_000,
  updated_at_ms: 1_777_593_600_000,
  avatar_url: "/admin/personas/grantley/assets/ast_123",
} as const;

beforeEach(() => {
  vi.unstubAllGlobals();
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("URL builders", () => {
  it("anchors the collection path at /admin/personas", () => {
    expect(PERSONAS_LIST_PATH).toBe("/admin/personas");
  });

  it("anchors the humanlike toggle path at /admin/channels/qq/humanlike", () => {
    expect(QQ_HUMANLIKE_PATH).toBe("/admin/channels/qq/humanlike");
  });

  it("encodes per-id paths so slugs round-trip", () => {
    expect(personaPath("grantley")).toBe("/admin/personas/grantley");
  });

  it("encodes non-ASCII / punctuation in slugs", () => {
    expect(personaPath("foo/bar baz")).toBe("/admin/personas/foo%2Fbar%20baz");
  });
});

describe("slugifyAssetLabel", () => {
  it("strips the extension, lowercases, and hyphenates whitespace", () => {
    expect(slugifyAssetLabel("Happy Face.PNG")).toBe("happy-face");
  });

  it("drops out-of-alphabet characters and collapses hyphen runs", () => {
    expect(slugifyAssetLabel("秋日 街景 (v2).jpeg")).toBe("v2");
    expect(slugifyAssetLabel("a___b")).toBe("a___b");
    expect(slugifyAssetLabel("a / b / c")).toBe("a-b-c");
  });

  it("trims leading/trailing hyphens and only strips the final extension", () => {
    expect(slugifyAssetLabel("--edge--.png")).toBe("edge");
    expect(slugifyAssetLabel("my.photo.final.png")).toBe("my-photo-final");
  });

  it("caps the result at 64 chars", () => {
    const out = slugifyAssetLabel(`${"x".repeat(100)}.png`);
    expect(out).toHaveLength(64);
    expect(out).toBe("x".repeat(64));
  });

  it("returns an empty string when nothing survives (caller falls back)", () => {
    expect(slugifyAssetLabel("图片.png")).toBe("");
    expect(slugifyAssetLabel(".gitignore")).toBe("");
  });

  it("produces labels that satisfy ASSET_LABEL_RE (when non-empty)", () => {
    for (const raw of ["Happy Face.PNG", "a / b / c", "--edge--.png"]) {
      const out = slugifyAssetLabel(raw);
      expect(out).not.toBe("");
      expect(ASSET_LABEL_RE.test(out)).toBe(true);
    }
  });
});

describe("fetchPersonas", () => {
  it("unwraps the { personas } envelope on 200", async () => {
    const { fn, calls } = makeFetchStub(() =>
      jsonResponse(200, { personas: [SAMPLE_PERSONA] }),
    );
    vi.stubGlobal("fetch", fn);

    const personas = await fetchPersonas();
    expect(personas).toHaveLength(1);
    expect(personas[0]?.id).toBe("grantley");
    expect(personas[0]?.is_builtin).toBe(true);
    expect(calls[0]?.url).toContain("/admin/personas");
    expect(calls[0]?.init.method ?? "GET").toBe("GET");
  });

  it("tolerates a missing `personas` field on 200", async () => {
    const { fn } = makeFetchStub(() => jsonResponse(200, {}));
    vi.stubGlobal("fetch", fn);

    const personas = await fetchPersonas();
    expect(personas).toEqual([]);
  });

  it("rethrows on 5xx", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(500, { error: "boom" }),
    );
    vi.stubGlobal("fetch", fn);
    await expect(fetchPersonas()).rejects.toThrow();
  });
});

describe("fetchPersona", () => {
  it("returns the persona on 200", async () => {
    const { fn, calls } = makeFetchStub(() => jsonResponse(200, SAMPLE_PERSONA));
    vi.stubGlobal("fetch", fn);

    const persona = await fetchPersona("grantley");
    expect(persona).not.toBeNull();
    expect(persona?.id).toBe("grantley");
    expect(calls[0]?.url).toContain("/admin/personas/grantley");
  });

  it("returns null on 404", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(404, { error: "not_found" }),
    );
    vi.stubGlobal("fetch", fn);

    const persona = await fetchPersona("missing");
    expect(persona).toBeNull();
  });

  it("rethrows on 5xx", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(500, { error: "boom" }),
    );
    vi.stubGlobal("fetch", fn);
    await expect(fetchPersona("grantley")).rejects.toThrow();
  });
});

describe("createPersona", () => {
  it("POSTs to /admin/personas with the full body", async () => {
    const { fn, calls } = makeFetchStub(() => jsonResponse(201, SAMPLE_PERSONA));
    vi.stubGlobal("fetch", fn);

    const created = await createPersona({
      id: "grantley",
      display_name: "格兰特利·贝尔",
      short_summary: "贝尔家的次子。",
      system_prompt: "# Grantley\n…",
    });
    expect(created.id).toBe("grantley");
    expect(calls[0]?.init.method).toBe("POST");
    const body = JSON.parse(String(calls[0]?.init.body ?? "{}"));
    expect(body).toEqual({
      id: "grantley",
      display_name: "格兰特利·贝尔",
      short_summary: "贝尔家的次子。",
      system_prompt: "# Grantley\n…",
    });
  });

  it("propagates errors verbatim", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(409, { error: "duplicate" }),
    );
    vi.stubGlobal("fetch", fn);
    await expect(
      createPersona({
        id: "grantley",
        display_name: "x",
        short_summary: "y",
        system_prompt: "z",
      }),
    ).rejects.toThrow();
  });
});

describe("updatePersona", () => {
  it("PATCHes /admin/personas/{id} with the partial body", async () => {
    const { fn, calls } = makeFetchStub(() => jsonResponse(200, SAMPLE_PERSONA));
    vi.stubGlobal("fetch", fn);

    await updatePersona("grantley", { display_name: "Grantley v2" });
    expect(calls[0]?.init.method).toBe("PATCH");
    expect(calls[0]?.url).toContain("/admin/personas/grantley");
    const body = JSON.parse(String(calls[0]?.init.body ?? "{}"));
    expect(body).toEqual({ display_name: "Grantley v2" });
  });

  it("forwards empty patches without dropping the body", async () => {
    const { fn, calls } = makeFetchStub(() => jsonResponse(200, SAMPLE_PERSONA));
    vi.stubGlobal("fetch", fn);

    await updatePersona("grantley", {});
    const body = JSON.parse(String(calls[0]?.init.body ?? "{}"));
    expect(body).toEqual({});
  });

  it("rethrows 404 (caller decides what to do)", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(404, { error: "not_found" }),
    );
    vi.stubGlobal("fetch", fn);
    await expect(
      updatePersona("missing", { display_name: "x" }),
    ).rejects.toThrow();
  });
});

describe("deletePersona", () => {
  it("returns undefined on 204", async () => {
    const { fn, calls } = makeFetchStub(() => noContent());
    vi.stubGlobal("fetch", fn);

    const result = await deletePersona("custom-grantley");
    expect(result).toBeUndefined();
    expect(calls[0]?.init.method).toBe("DELETE");
    expect(calls[0]?.url).toContain("/admin/personas/custom-grantley");
  });

  it("returns the builtin-protected sentinel on 404", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(404, { error: "builtin_protected" }),
    );
    vi.stubGlobal("fetch", fn);

    const result = await deletePersona("grantley");
    expect(result).toBe("builtin_protected");
  });

  it("returns the builtin-protected sentinel on 404 with any body", async () => {
    // Server emits 404 for both builtin-protected and unknown-id; we collapse
    // both into the sentinel — either way the row is already gone.
    const { fn } = makeFetchStub(() =>
      jsonResponse(404, { error: "not_found" }),
    );
    vi.stubGlobal("fetch", fn);
    expect(await deletePersona("missing")).toBe("builtin_protected");
  });

  it("rethrows on 5xx", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(500, { error: "boom" }),
    );
    vi.stubGlobal("fetch", fn);
    await expect(deletePersona("grantley")).rejects.toThrow();
  });
});

describe("fetchQqHumanlike", () => {
  it("returns the wire shape verbatim on 200", async () => {
    const { fn, calls } = makeFetchStub(() =>
      jsonResponse(200, { enabled: true, persona_id: "grantley" }),
    );
    vi.stubGlobal("fetch", fn);

    const state = await fetchQqHumanlike();
    expect(state).toEqual({ enabled: true, persona_id: "grantley" });
    expect(calls[0]?.url).toContain("/admin/channels/qq/humanlike");
  });

  it("preserves the null persona_id sentinel", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(200, { enabled: false, persona_id: null }),
    );
    vi.stubGlobal("fetch", fn);
    const state = await fetchQqHumanlike();
    expect(state.persona_id).toBeNull();
    expect(state.enabled).toBe(false);
  });
});

describe("setQqHumanlike", () => {
  it("PUTs the payload and echoes the response", async () => {
    const { fn, calls } = makeFetchStub((init) => {
      // Server echoes back what we sent.
      const echoed =
        init.body !== undefined && init.body !== null
          ? JSON.parse(String(init.body))
          : { enabled: false, persona_id: null };
      return jsonResponse(200, echoed);
    });
    vi.stubGlobal("fetch", fn);

    const state = await setQqHumanlike({
      enabled: true,
      persona_id: "grantley",
    });
    expect(state).toEqual({ enabled: true, persona_id: "grantley" });
    expect(calls[0]?.init.method).toBe("PUT");
    const body = JSON.parse(String(calls[0]?.init.body ?? "{}"));
    expect(body).toEqual({ enabled: true, persona_id: "grantley" });
  });

  it("rethrows non-2xx", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(400, { error: "persona_required_when_enabled" }),
    );
    vi.stubGlobal("fetch", fn);
    await expect(
      setQqHumanlike({ enabled: true, persona_id: null }),
    ).rejects.toThrow();
  });
});

describe("parameterized humanlike (all channels)", () => {
  it("exposes the supported channels (incl. the two official platforms)", () => {
    expect([...SUPPORTED_HUMANLIKE_CHANNELS]).toEqual([
      "qq",
      "telegram",
      "discord",
      "slack",
      "feishu",
      "qq_official",
      "wechat_official",
    ]);
  });

  it("builds /admin/channels/{channel}/humanlike per channel", () => {
    expect(humanlikePath("telegram")).toBe("/admin/channels/telegram/humanlike");
    expect(humanlikePath("feishu")).toBe("/admin/channels/feishu/humanlike");
    // qq wrapper still anchors at the same path
    expect(humanlikePath("qq")).toBe(QQ_HUMANLIKE_PATH);
  });

  it("fetchHumanlike hits the channel-specific path", async () => {
    const { fn, calls } = makeFetchStub(() =>
      jsonResponse(200, { enabled: true, persona_id: "grantley" }),
    );
    vi.stubGlobal("fetch", fn);
    const state = await fetchHumanlike("telegram");
    expect(state).toEqual({ enabled: true, persona_id: "grantley" });
    expect(calls[0]?.url).toContain("/admin/channels/telegram/humanlike");
  });

  it("setHumanlike PUTs to the channel-specific path", async () => {
    const { fn, calls } = makeFetchStub((init) =>
      jsonResponse(
        200,
        init.body ? JSON.parse(String(init.body)) : { enabled: false, persona_id: null },
      ),
    );
    vi.stubGlobal("fetch", fn);
    await setHumanlike("discord", { enabled: true, persona_id: "grantley" });
    expect(calls[0]?.url).toContain("/admin/channels/discord/humanlike");
    expect(calls[0]?.init.method).toBe("PUT");
  });
});

describe("persona life layer", () => {
  it("fetchLifeState GETs the life-state path", async () => {
    const { fn, calls } = makeFetchStub(() =>
      jsonResponse(200, {
        mood: "neutral",
        fatigue: 0,
        recent_topics: [],
        state_json: {},
        updated_at_ms: 0,
      }),
    );
    vi.stubGlobal("fetch", fn);
    const state = await fetchLifeState("grantley");
    expect(state.mood).toBe("neutral");
    expect(state.recent_topics).toEqual([]);
    expect(calls[0]?.url).toContain("/admin/personas/grantley/life-state");
    expect(calls[0]?.init.method ?? "GET").toBe("GET");
  });

  it("patchLifeState PATCHes only the supplied fields", async () => {
    const { fn, calls } = makeFetchStub((init) =>
      jsonResponse(200, {
        mood: "tired",
        fatigue: 0.7,
        recent_topics: ["weather"],
        state_json: {},
        updated_at_ms: 1_777_600_000_000,
        ...(init.body ? JSON.parse(String(init.body)) : {}),
      }),
    );
    vi.stubGlobal("fetch", fn);
    const updated = await patchLifeState("grantley", { mood: "tired", fatigue: 0.7 });
    expect(updated.mood).toBe("tired");
    expect(calls[0]?.init.method).toBe("PATCH");
    const body = JSON.parse(String(calls[0]?.init.body ?? "{}"));
    expect(body).toEqual({ mood: "tired", fatigue: 0.7 });
  });

  it("fetchDiary unwraps the { entries } envelope + forwards the limit", async () => {
    const { fn, calls } = makeFetchStub(() =>
      jsonResponse(200, { entries: [{ ts: 1, text: "hello" }] }),
    );
    vi.stubGlobal("fetch", fn);
    const entries = await fetchDiary("grantley", 25);
    expect(entries).toHaveLength(1);
    expect(entries[0]?.text).toBe("hello");
    expect(calls[0]?.url).toContain("/admin/personas/grantley/diary?limit=25");
  });

  it("fetchDiary tolerates a missing entries field", async () => {
    const { fn } = makeFetchStub(() => jsonResponse(200, {}));
    vi.stubGlobal("fetch", fn);
    expect(await fetchDiary("grantley")).toEqual([]);
  });

  it("fetchLifeSeeds GETs the life-seeds path", async () => {
    const { fn, calls } = makeFetchStub(() =>
      jsonResponse(200, { yaml: "events: []\n", source: "bundled" }),
    );
    vi.stubGlobal("fetch", fn);
    const seeds = await fetchLifeSeeds("grantley");
    expect(seeds.source).toBe("bundled");
    expect(calls[0]?.url).toContain("/admin/personas/grantley/life-seeds");
  });

  it("putLifeSeeds PUTs the yaml body", async () => {
    const { fn, calls } = makeFetchStub(() => jsonResponse(200, { ok: true }));
    vi.stubGlobal("fetch", fn);
    const res = await putLifeSeeds("grantley", "events: []\n");
    expect(res.ok).toBe(true);
    expect(calls[0]?.init.method).toBe("PUT");
    const body = JSON.parse(String(calls[0]?.init.body ?? "{}"));
    expect(body).toEqual({ yaml: "events: []\n" });
  });

  it("resetPersonaToDefault POSTs to reset-to-default", async () => {
    const { fn, calls } = makeFetchStub(() => jsonResponse(200, { ok: true }));
    vi.stubGlobal("fetch", fn);
    const res = await resetPersonaToDefault("grantley");
    expect(res.ok).toBe(true);
    expect(calls[0]?.url).toContain("/admin/personas/grantley/reset-to-default");
    expect(calls[0]?.init.method).toBe("POST");
  });

  it("runPersonaDecay POSTs to decay + returns rows_changed", async () => {
    const { fn, calls } = makeFetchStub(() =>
      jsonResponse(200, { rows_changed: 1 }),
    );
    vi.stubGlobal("fetch", fn);
    const res = await runPersonaDecay("grantley");
    expect(res.rows_changed).toBe(1);
    expect(calls[0]?.url).toContain("/admin/personas/grantley/decay");
    expect(calls[0]?.init.method).toBe("POST");
  });
});

describe("renameAsset", () => {
  it("PATCHes the asset item path with the new label", async () => {
    const { fn, calls } = makeFetchStub(() =>
      jsonResponse(200, {
        id: "ast_1",
        persona_id: "grantley",
        kind: "reference",
        label: "new-label",
        file_name: "a.png",
        mime: "image/png",
        size_bytes: 10,
        sha256: "x",
        created_at_ms: 1,
        url: "/admin/personas/grantley/assets/ast_1",
      }),
    );
    vi.stubGlobal("fetch", fn);
    const updated = await renameAsset("grantley", "ast_1", "new-label");
    expect(updated.label).toBe("new-label");
    expect(calls[0]?.url).toContain(
      "/admin/personas/grantley/assets/ast_1",
    );
    expect(calls[0]?.init.method).toBe("PATCH");
    const body = JSON.parse(String(calls[0]?.init.body ?? "{}"));
    expect(body).toEqual({ label: "new-label" });
  });

  it("maps a 409 collision into an AssetUploadError", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(409, { detail: { error: "duplicate_label" } }),
    );
    vi.stubGlobal("fetch", fn);
    await expect(renameAsset("grantley", "ast_1", "dup")).rejects.toBeInstanceOf(
      AssetUploadError,
    );
  });
});
