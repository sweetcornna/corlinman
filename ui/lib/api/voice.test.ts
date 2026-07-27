import { afterEach, describe, expect, it, vi } from "vitest";

import {
  REDACTED_SECRET,
  listVoiceBackends,
  previewVoice,
  putVoiceSettings,
} from "./voice";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("listVoiceBackends", () => {
  it("returns whatever the registry reports, including operator-defined ones", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(200, {
          backends: [
            { id: "gpt_live", label: "OpenAI Realtime", kind: "webrtc_live", custom: false },
            { id: "acme", label: "Acme", kind: "http", custom: true },
          ],
          formats: { mp3: "audio/mpeg" },
          default_backend: "openai",
        }),
      ),
    );
    const res = await listVoiceBackends();
    expect(res.backends.map((b) => b.id)).toEqual(["gpt_live", "acme"]);
    expect(res.backends[1].custom).toBe(true);
  });
});

describe("putVoiceSettings", () => {
  it("PUTs the patch as JSON", async () => {
    const calls: RequestInit[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_i: RequestInfo | URL, init?: RequestInit) => {
        calls.push(init ?? {});
        return jsonResponse(200, { status: "ok" });
      }),
    );
    await putVoiceSettings({ backend: "gpt_live", voice: "marin" });
    expect(calls[0].method).toBe("PUT");
    expect(JSON.parse(String(calls[0].body))).toEqual({
      backend: "gpt_live",
      voice: "marin",
    });
  });

  it("can round-trip the redaction sentinel without leaking a real key", async () => {
    const calls: RequestInit[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_i: RequestInfo | URL, init?: RequestInit) => {
        calls.push(init ?? {});
        return jsonResponse(200, { status: "ok" });
      }),
    );
    await putVoiceSettings({ backends: { fish: { api_key: REDACTED_SECRET } } });
    const sent = JSON.parse(String(calls[0].body));
    expect(sent.backends.fish.api_key).toBe(REDACTED_SECRET);
  });
});

describe("previewVoice", () => {
  it("returns the playable descriptor on success", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(200, {
          ok: true,
          url: "/v1/files/abc",
          mime: "audio/mpeg",
          backend: "openai",
          voice: "nova",
          model: "gpt-4o-mini-tts",
          format: "mp3",
          size_bytes: 1234,
        }),
      ),
    );
    const res = await previewVoice({ voice: "nova" });
    expect(res.ok).toBe(true);
    if (res.ok) {
      expect(res.url).toBe("/v1/files/abc");
      expect(res.voice).toBe("nova");
    }
  });

  it("decodes an upstream failure instead of throwing", async () => {
    // An operator auditioning an unconfigured backend must see the
    // reason inline — a 502 here is a normal outcome, not an exception.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(502, {
          error: "live_attestation_unavailable",
          message: "网关拒绝 Live 会话",
          backend: "gpt_live",
          upstream_status: 503,
        }),
      ),
    );
    const res = await previewVoice({ backend: "gpt_live" });
    expect(res.ok).toBe(false);
    if (!res.ok) {
      expect(res.error).toBe("live_attestation_unavailable");
      expect(res.upstream_status).toBe(503);
      expect(res.backend).toBe("gpt_live");
    }
  });

  it("decodes an unknown-backend 404", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(404, { error: "unknown_backend", message: "nope" }),
      ),
    );
    const res = await previewVoice({ backend: "nope" });
    expect(res.ok).toBe(false);
    if (!res.ok) expect(res.error).toBe("unknown_backend");
  });

  it("rethrows a genuine network fault rather than faking a result", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("network down");
      }),
    );
    await expect(previewVoice({})).rejects.toThrow("network down");
  });
});
