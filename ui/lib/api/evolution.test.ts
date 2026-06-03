/**
 * Evolution settings client corpus. Mirrors `routes_admin_b/evolution.py`
 * `/admin/evolution/settings` GET/PUT.
 *
 * Covers:
 *   - normalizeEvolutionSettings fills defaults for a partial snapshot
 *   - fetch `200` happy path → tagged `{ kind: "ok", settings }`
 *   - fetch normalises a partial body (missing budget / auto_rollback)
 *   - fetch `503 config_path_unset` → `{ kind: "disabled" }`
 *   - fetch other failures (500) → `{ kind: "error" }`
 *   - save issues a PUT with the JSON body and returns the echo envelope
 *   - save rethrows CorlinmanApiError on 503 (config_path_unset)
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  fetchEvolutionSettings,
  normalizeEvolutionSettings,
  saveEvolutionSettings,
  type EvolutionSettings,
} from "./evolution";
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

const FULL_SETTINGS: EvolutionSettings = {
  meta_approver_users: ["admin", "alice"],
  budget: {
    enabled: true,
    weekly_total: 20,
    per_kind: { engine_prompt: 5 },
  },
  auto_rollback: {
    enabled: true,
    grace_window_hours: 48,
    thresholds: {
      default_err_rate_delta_pct: 2.5,
      default_p95_latency_delta_pct: 10,
      signal_window_secs: 3600,
      min_baseline_signals: 30,
    },
  },
};

describe("normalizeEvolutionSettings", () => {
  it("fills defaults for an empty/partial snapshot", () => {
    const norm = normalizeEvolutionSettings({});
    expect(norm.meta_approver_users).toEqual([]);
    expect(norm.budget.enabled).toBe(false);
    expect(norm.budget.weekly_total).toBe(0);
    expect(norm.budget.per_kind).toEqual({});
    expect(norm.auto_rollback.enabled).toBe(false);
    expect(norm.auto_rollback.grace_window_hours).toBe(72);
    expect(norm.auto_rollback.thresholds.signal_window_secs).toBe(0);
  });

  it("preserves provided values and coerces approver ids to strings", () => {
    const norm = normalizeEvolutionSettings({
      meta_approver_users: ["admin", 42 as unknown as string],
      budget: { enabled: true, weekly_total: 7, per_kind: { x: 1 } },
    });
    expect(norm.meta_approver_users).toEqual(["admin", "42"]);
    expect(norm.budget.weekly_total).toBe(7);
    expect(norm.budget.per_kind).toEqual({ x: 1 });
    // untouched section still defaults
    expect(norm.auto_rollback.grace_window_hours).toBe(72);
  });
});

describe("fetchEvolutionSettings", () => {
  beforeEach(() => vi.unstubAllGlobals());
  afterEach(() => vi.unstubAllGlobals());

  it("returns normalised settings on 200", async () => {
    const { fn, calls } = makeFetchStub(() =>
      jsonResponse(200, FULL_SETTINGS),
    );
    vi.stubGlobal("fetch", fn);

    const result = await fetchEvolutionSettings();
    expect(result.kind).toBe("ok");
    if (result.kind !== "ok") throw new Error("expected ok");
    expect(result.settings.meta_approver_users).toEqual(["admin", "alice"]);
    expect(result.settings.budget.weekly_total).toBe(20);
    expect(result.settings.auto_rollback.thresholds.min_baseline_signals).toBe(
      30,
    );
    expect(calls[0]?.url).toContain("/admin/evolution/settings");
    expect(calls[0]?.init.method ?? "GET").toBe("GET");
  });

  it("normalises a partial body (missing budget / auto_rollback)", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(200, { meta_approver_users: ["admin"] }),
    );
    vi.stubGlobal("fetch", fn);

    const result = await fetchEvolutionSettings();
    expect(result.kind).toBe("ok");
    if (result.kind !== "ok") throw new Error("expected ok");
    expect(result.settings.budget.weekly_total).toBe(0);
    expect(result.settings.auto_rollback.grace_window_hours).toBe(72);
  });

  it("maps 503 config_path_unset to the disabled tag", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(503, { error: "config_path_unset" }),
    );
    vi.stubGlobal("fetch", fn);

    const result = await fetchEvolutionSettings();
    expect(result.kind).toBe("disabled");
  });

  it("returns an error tag on 500", async () => {
    const { fn } = makeFetchStub(() => jsonResponse(500, { error: "boom" }));
    vi.stubGlobal("fetch", fn);

    const result = await fetchEvolutionSettings();
    expect(result.kind).toBe("error");
  });
});

describe("saveEvolutionSettings", () => {
  beforeEach(() => vi.unstubAllGlobals());
  afterEach(() => vi.unstubAllGlobals());

  it("PUTs the JSON body and returns the echo envelope", async () => {
    const { fn, calls } = makeFetchStub(() =>
      jsonResponse(200, { status: "ok", settings: FULL_SETTINGS }),
    );
    vi.stubGlobal("fetch", fn);

    const res = await saveEvolutionSettings(FULL_SETTINGS);
    expect(res.status).toBe("ok");
    expect(res.settings.meta_approver_users).toEqual(["admin", "alice"]);
    expect(calls[0]?.url).toContain("/admin/evolution/settings");
    expect(calls[0]?.init.method).toBe("PUT");
    expect(JSON.parse(String(calls[0]?.init.body))).toEqual(FULL_SETTINGS);
  });

  it("rethrows CorlinmanApiError on 503 (config_path_unset)", async () => {
    const { fn } = makeFetchStub(() =>
      jsonResponse(503, { error: "config_path_unset" }),
    );
    vi.stubGlobal("fetch", fn);

    await expect(saveEvolutionSettings(FULL_SETTINGS)).rejects.toBeInstanceOf(
      CorlinmanApiError,
    );
  });
});
