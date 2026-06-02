/**
 * API-keys client corpus. Mirrors `routes_admin_a/api_keys.py`.
 *
 * Covers:
 *   - revoke path encodes the key id (colon / slash safe)
 *   - list `200` happy path → tagged `{ kind: "ok", keys }`
 *   - list tolerates a missing `keys` field on 200
 *   - list `503 tenants_disabled` → `{ kind: "disabled" }`
 *   - list other failures (500) → `{ kind: "error" }`
 *   - mint posts the body and returns the one-time cleartext `token`
 *   - mint rethrows on 400 (empty scope)
 *   - revoke issues a DELETE and returns the `{ revoked, key_id }` body
 *   - revoke rethrows on 404
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  apiKeyRevokePath,
  listApiKeys,
  mintApiKey,
  revokeApiKey,
} from "./api-keys";
import { CorlinmanApiError } from "@/lib/api";

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

describe("apiKeyRevokePath", () => {
  it("anchors at /admin/api_keys and encodes the id", () => {
    expect(apiKeyRevokePath("abc123")).toBe("/admin/api_keys/abc123");
  });
  it("percent-encodes colons / slashes in the id", () => {
    expect(apiKeyRevokePath("ten:1/2")).toBe(
      "/admin/api_keys/ten%3A1%2F2",
    );
  });
});

describe("listApiKeys", () => {
  beforeEach(() => vi.unstubAllGlobals());
  afterEach(() => vi.unstubAllGlobals());

  it("returns the key rows on 200", async () => {
    const { fn, calls } = makeFetchStub(() =>
      jsonResponse(200, {
        keys: [
          {
            key_id: "k1",
            tenant_id: "default",
            username: "admin",
            scope: "chat",
            label: "ci",
            created_at_ms: 1,
            last_used_at_ms: null,
          },
        ],
      }),
    );
    vi.stubGlobal("fetch", fn);

    const result = await listApiKeys();
    expect(result.kind).toBe("ok");
    if (result.kind !== "ok") throw new Error("expected ok");
    expect(result.keys).toHaveLength(1);
    expect(result.keys[0]?.key_id).toBe("k1");
    expect(calls[0]?.url).toContain("/admin/api_keys");
    expect(calls[0]?.init.method ?? "GET").toBe("GET");
  });

  it("tolerates a missing `keys` field on 200", async () => {
    const { fn } = makeFetchStub(() => jsonResponse(200, {}));
    vi.stubGlobal("fetch", fn);

    const result = await listApiKeys();
    expect(result.kind).toBe("ok");
    if (result.kind !== "ok") throw new Error("expected ok");
    expect(result.keys).toEqual([]);
  });

  it("maps 503 tenants_disabled to the disabled tag", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(503, { error: "tenants_disabled" }),
    );
    vi.stubGlobal("fetch", fn);

    const result = await listApiKeys();
    expect(result.kind).toBe("disabled");
  });

  it("returns an error tag on 500", async () => {
    const { fn } = makeFetchStub(() => jsonResponse(500, { error: "boom" }));
    vi.stubGlobal("fetch", fn);

    const result = await listApiKeys();
    expect(result.kind).toBe("error");
  });
});

describe("mintApiKey", () => {
  beforeEach(() => vi.unstubAllGlobals());
  afterEach(() => vi.unstubAllGlobals());

  it("POSTs the body and returns the one-time cleartext token", async () => {
    const { fn, calls } = makeFetchStub(() =>
      jsonResponse(201, {
        key_id: "k2",
        tenant_id: "default",
        username: "ci-bot",
        scope: "chat",
        label: "deploy",
        token: "sk-live-secret",
        created_at_ms: 42,
      }),
    );
    vi.stubGlobal("fetch", fn);

    const res = await mintApiKey({
      scope: "chat",
      username: "ci-bot",
      label: "deploy",
    });
    expect(res.token).toBe("sk-live-secret");
    expect(res.key_id).toBe("k2");
    expect(calls[0]?.url).toContain("/admin/api_keys");
    expect(calls[0]?.init.method).toBe("POST");
    expect(JSON.parse(String(calls[0]?.init.body))).toEqual({
      scope: "chat",
      username: "ci-bot",
      label: "deploy",
    });
  });

  it("rethrows CorlinmanApiError on 400 (empty scope)", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(400, { error: "invalid_request" }),
    );
    vi.stubGlobal("fetch", fn);

    await expect(mintApiKey({ scope: "" })).rejects.toBeInstanceOf(
      CorlinmanApiError,
    );
  });
});

describe("revokeApiKey", () => {
  beforeEach(() => vi.unstubAllGlobals());
  afterEach(() => vi.unstubAllGlobals());

  it("issues a DELETE and returns the revoke envelope", async () => {
    const { fn, calls } = makeFetchStub(() =>
      jsonResponse(200, { revoked: true, key_id: "k3" }),
    );
    vi.stubGlobal("fetch", fn);

    const res = await revokeApiKey("k3");
    expect(res).toEqual({ revoked: true, key_id: "k3" });
    expect(calls[0]?.url).toContain("/admin/api_keys/k3");
    expect(calls[0]?.init.method).toBe("DELETE");
  });

  it("rethrows CorlinmanApiError on 404", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(404, { error: "not_found" }),
    );
    vi.stubGlobal("fetch", fn);

    await expect(revokeApiKey("missing")).rejects.toBeInstanceOf(
      CorlinmanApiError,
    );
  });
});
