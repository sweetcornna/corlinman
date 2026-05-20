import { defineConfig, devices } from "@playwright/test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

/**
 * Playwright config — Wave 5.1.
 *
 * Two `webServer` entries are conditionally enabled when
 * `CORLINMAN_E2E=1` is set. They start:
 *
 *   1. The Python gateway (`uv run corlinman-gateway`) on port 6005. Specs
 *      that mutate state (onboarding, profile lifecycle, curator) need
 *      a real backend — mocking these flows would defeat the purpose
 *      of W5.1 (which is to exercise the whole stack).
 *   2. The Next.js dev server on port 3000.
 *
 * Without `CORLINMAN_E2E=1`, Playwright assumes both services are
 * already running (this is the common local-dev case) and the affected
 * specs `test.skip()` themselves at suite level — keeping `pnpm
 * playwright test` cheap and predictable for contributors who only
 * have the UI dev server up.
 *
 * Env vars consumed:
 *   - `PLAYWRIGHT_BASE_URL`     — UI base (default http://localhost:3000)
 *   - `PLAYWRIGHT_GATEWAY_URL`  — gateway base (default http://localhost:6005)
 *   - `CORLINMAN_E2E=1`         — gate full-stack E2E specs on
 *   - `CI`                      — Playwright's standard CI tweaks
 */

const wantsFullStack = process.env.CORLINMAN_E2E === "1";

const baseURL =
  process.env.PLAYWRIGHT_BASE_URL ?? "http://localhost:3000";
const gatewayURL =
  process.env.PLAYWRIGHT_GATEWAY_URL ?? "http://localhost:6005";
const uiOrigin = new URL(baseURL).origin;
const e2eDataDir =
  process.env.CORLINMAN_DATA_DIR ??
  mkdtempSync(join(tmpdir(), "corlinman-e2e-"));

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  // Default to a generous timeout — full-stack specs need ~30s to spin
  // up the gateway lifecycle on cold start, and the existing spec dir
  // budget was the framework default (30s). Make it explicit.
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"]],
  use: {
    baseURL,
    trace: "on-first-retry",
    extraHTTPHeaders: {
      // Surfaces the spec name in gateway access logs so failures are
      // easier to triage.
      "x-corlinman-source": "playwright",
    },
  },
  webServer: wantsFullStack
    ? [
        {
          // Gateway first — the UI dev server expects /admin/* to
          // proxy through Next's rewrites once the gateway is alive.
          command: "uv run corlinman-gateway",
          url: `${gatewayURL}/health`,
          reuseExistingServer: false,
          timeout: 60_000,
          env: {
            // Use a throwaway data dir per run so admin/root seed is
            // re-installed cleanly. A fixed /tmp path lets failed runs
            // leak rotated credentials into the next run.
            CORLINMAN_DATA_DIR: e2eDataDir,
            CORLINMAN_CORS_ORIGINS: uiOrigin,
          },
        },
        {
          command: "pnpm dev",
          url: baseURL,
          reuseExistingServer: false,
          timeout: 120_000,
          env: {
            NEXT_PUBLIC_GATEWAY_URL: gatewayURL,
          },
        },
      ]
    : undefined,
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
