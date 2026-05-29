/**
 * Gateway-base-URL prefix tests for the inline cost fetchers (G11).
 *
 * Both `CostFooter` and `SessionCostCells` fall back to an inline
 * `_loadCostInline` that hits `/admin/sessions/{key}/cost`. When the UI is
 * served from a *different* origin than the gateway, the deployer sets
 * `NEXT_PUBLIC_GATEWAY_URL`; the inline fetch MUST prefix it the same way
 * `apiFetch` and the `streamX` EventSource factories do — otherwise the
 * request hits the UI origin, 404s, and the cells render "—".
 *
 * `GATEWAY_BASE_URL` is captured from `process.env` at module-load time, so
 * we stub the env var and re-import the component fresh per case.
 */

import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, waitFor } from "@testing-library/react";
import { I18nextProvider } from "react-i18next";

import { i18next, initI18n } from "@/lib/i18n";
import type { SessionCostResponse } from "@/components/sessions/cost-footer";

const GATEWAY = "https://gw.example.test";

const NORMAL: SessionCostResponse = {
  session_key: "qq:1234",
  turn_count: 12,
  total_elapsed_ms: 145_000,
  total_cost_usd: 0.087,
  cost_status_breakdown: { estimated: 0, billed: 12, unknown: 0 },
  total_tool_calls: 47,
  last_turn_at_ms: Date.now() - 2 * 60_000,
  avg_turn_ms: 12_083,
};

function Harness({ children }: { children: React.ReactNode }) {
  return <I18nextProvider i18n={i18next}>{children}</I18nextProvider>;
}

/** Stub fetch; return the recorded request URL. */
function installFetchStub(): { calls: string[] } {
  const calls: string[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    calls.push(typeof input === "string" ? input : String(input));
    return new Response(JSON.stringify(NORMAL), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fn);
  return { calls };
}

beforeEach(() => {
  initI18n();
  void i18next.changeLanguage("en");
  vi.resetModules();
  vi.stubEnv("NEXT_PUBLIC_GATEWAY_URL", GATEWAY);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("cost inline fetchers — GATEWAY_BASE_URL prefix", () => {
  it("CostFooter._loadCostInline prefixes GATEWAY_BASE_URL", async () => {
    const { calls } = installFetchStub();
    const { CostFooter } = await import("@/components/sessions/cost-footer");

    render(
      <Harness>
        <CostFooter sessionKey="qq:1234" />
      </Harness>,
    );

    await waitFor(() => {
      expect(calls.length).toBeGreaterThan(0);
    });
    expect(calls[0]).toBe(`${GATEWAY}/admin/sessions/qq%3A1234/cost`);
    expect(calls[0]!.startsWith(GATEWAY)).toBe(true);
  });

  it("SessionCostCells._loadCostInline prefixes GATEWAY_BASE_URL", async () => {
    const { calls } = installFetchStub();
    const mod = await import("@/components/sessions/session-cost-cells");
    mod.__resetCostCache();
    const { SessionCostCells } = mod;

    render(
      <Harness>
        <table>
          <tbody>
            <tr>
              <SessionCostCells sessionKey="qq:1234" />
            </tr>
          </tbody>
        </table>
      </Harness>,
    );

    await waitFor(() => {
      expect(calls.length).toBeGreaterThan(0);
    });
    expect(calls[0]).toBe(`${GATEWAY}/admin/sessions/qq%3A1234/cost`);
    expect(calls[0]!.startsWith(GATEWAY)).toBe(true);
  });
});
