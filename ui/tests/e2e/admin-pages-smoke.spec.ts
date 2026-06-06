/**
 * Admin pages smoke — Wave 3 W3.1.
 *
 * Stubs-only smoke test that drives every "basically unusable" admin
 * surface so a deploy doesn't regress the contracts between UI and
 * gateway. The plan calls out six pages plus the per-turn drill-down:
 *
 *   /admin/sessions                                  — list
 *   /admin/sessions/{key}                            — detail (timeline)
 *   /admin/sessions/{key}/turns/{turn_id}            — drill-down replay
 *   /admin/logs                                      — log stream
 *   /admin/providers                                 — provider table
 *   /admin/credentials                               — credentials manager
 *   /admin/models                                    — alias editor
 *
 * Each test follows the same shape:
 *
 *   1. Console + network-failure listeners attached. Any `error`-level
 *      console message or any failed XHR under `/admin/*` fails the
 *      test (these are the regressions we care about — "UI calls
 *      endpoint that doesn't exist" surfaces as a 404 → JS error).
 *   2. The endpoints the page hits on mount are stubbed with sensible
 *      fixtures so the rest of the page renders. We deliberately do not
 *      depend on a real gateway: the goal is to assert the UI contract,
 *      not to exercise the backend.
 *   3. Navigate, then assert at least one expected element renders.
 *
 * The full-stack version of these specs (which would exercise the
 * actual SSE / cost / turns endpoints) is gated on `CORLINMAN_E2E=1`
 * and lives in the existing observability harness — see
 * task-observability.spec.ts.
 */

import { expect, test, type Page, type Route } from "@playwright/test";

import { pinLocaleEn } from "./helpers/auth";
import {
  buildSseLogsBody,
  buildSseTurnEventsBody,
  COST_RESPONSE,
  CREDENTIALS_RESPONSE,
  FIXTURE_TURN_EVENTS,
  MODELS_V2_RESPONSE,
  OAUTH_STATUS_EMPTY,
  PERSONAS_RESPONSE,
  PROVIDER_KINDS_RESPONSE,
  PROVIDER_MODELS_RESPONSE,
  PROVIDER_TEST_OK,
  PROVIDERS_RESPONSE,
  REVEAL_VALUE,
  SESSION_KEY,
  SESSIONS_LIST_RESPONSE,
  TURN_ID,
  TURNS_LIST_RESPONSE,
} from "./admin-pages-smoke._fixtures";

const TEST_TIMEOUT_MS = 10_000;

const INFO_NO_UPDATE_RESPONSE = {
  current: "1.1.1",
  latest: "1.1.1",
  available: false,
  release_url: null,
  release_notes_md: null,
  published_at: null,
  last_checked_at: 1_716_540_000_000,
  prerelease_seen: [] as string[],
} as const;

// ---------------------------------------------------------------------------
// Shared listeners + auth stubs
// ---------------------------------------------------------------------------

/**
 * Wire the console-error + requestfailed listeners. Returns a `verify()`
 * closure that the test calls at the end so the assertion text shows up
 * in the right place in the trace.
 *
 * Console errors that originate from React's dev-mode key warnings are
 * still real bugs worth chasing — we don't filter them.
 */
function attachStrictListeners(page: Page): () => void {
  const consoleErrors: string[] = [];
  const requestFailures: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") {
      consoleErrors.push(msg.text());
    }
  });
  page.on("requestfailed", (req) => {
    const url = req.url();
    if (url.includes("/admin/")) {
      // EventSource auto-reconnect generates "net::ERR_ABORTED" when the
      // page navigates while the stream is still open — we ignore the
      // unmount-time aborts so a clean navigation doesn't fail the test.
      const failure = req.failure();
      const text = failure?.errorText ?? "";
      if (
        text.includes("net::ERR_ABORTED") ||
        text.includes("net::ERR_CACHE_MISS")
      ) {
        return;
      }
      requestFailures.push(`${req.method()} ${url} — ${text}`);
    }
  });
  return () => {
    expect(consoleErrors, "no console errors").toEqual([]);
    expect(requestFailures, "no failed XHR under /admin/*").toEqual([]);
  };
}

/**
 * Install the auth stubs every admin page needs on mount:
 *
 *   - `POST /admin/login`  — not navigated to in these specs, but harmless
 *                            to install in case any helper calls into it.
 *   - `GET  /admin/me`     — what `AdminLayout` uses to gate render. We
 *                            return a session with `must_change_password:
 *                            false` so the guard doesn't bounce us to
 *                            `/account/security`.
 */
async function installAuthStubs(page: Page): Promise<void> {
  await page.route("**/admin/login", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "set-cookie": "corlinman_session=stub; Path=/; HttpOnly" },
      body: JSON.stringify({ expires_in: 3600 }),
    });
  });
  await page.route("**/admin/me", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        user: "ops",
        created_at: new Date().toISOString(),
        expires_at: new Date(Date.now() + 3600_000).toISOString(),
        must_change_password: false,
      }),
    });
  });
}

// ---------------------------------------------------------------------------
// Per-page stubs — each helper installs the minimum surface the page
// touches on mount. Order matters: more-specific routes must be
// registered before catch-alls so Playwright's first-match resolution
// picks the right body.
// ---------------------------------------------------------------------------

/**
 * Common health stub — `DefaultPasswordBanner` and a couple of layout
 * widgets poll `/health` opportunistically. The default-mode response
 * is enough; we just need a 200 so nothing throws.
 */
async function installHealthStub(page: Page): Promise<void> {
  await page.route("**/health", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "ok",
        version: "test",
        checks: [],
      }),
    });
  });
}

/** Stubs for the `/admin/sessions` list page. */
async function installSessionsListStubs(page: Page): Promise<void> {
  await page.route(
    `**/admin/sessions/*/cost`,
    async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(COST_RESPONSE),
      });
    },
  );
  // The bare list — match strictly so the per-key /cost above isn't
  // shadowed. Playwright route ordering is "most-recently-added wins";
  // the catch-all goes after the specifics for that reason.
  await page.route("**/admin/sessions*", async (route: Route) => {
    const url = route.request().url();
    // Fall through for nested paths the specific routes own.
    if (
      url.includes("/events/live") ||
      url.includes("/turns") ||
      url.includes("/cost")
    ) {
      return route.fallback();
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(SESSIONS_LIST_RESPONSE),
    });
  });
}

/** Stubs for the `/admin/sessions/{key}` detail page. */
async function installSessionDetailStubs(page: Page): Promise<void> {
  const encodedKey = encodeURIComponent(SESSION_KEY);
  // W1.2 — past-turns listing endpoint.
  await page.route(
    `**/admin/sessions/${encodedKey}/turns*`,
    async (route: Route) => {
      const url = route.request().url();
      // The drill-down events endpoint owns this URL prefix too; let
      // its specific handler claim it first.
      if (url.includes("/turns/") && url.includes("/events")) {
        return route.fallback();
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(TURNS_LIST_RESPONSE),
      });
    },
  );
  // W2.1 — SSE live stream. Fulfilled with a complete body up-front;
  // EventSource drains it the same as a streamed response.
  await page.route(
    `**/admin/sessions/${encodedKey}/events/live*`,
    async (route: Route) => {
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream",
          "cache-control": "no-cache",
        },
        body: buildSseTurnEventsBody(),
      });
    },
  );
  await page.route(
    `**/admin/sessions/${encodedKey}/cost`,
    async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(COST_RESPONSE),
      });
    },
  );
}

/** Stubs for the `/admin/sessions/{key}/turns/{turn_id}` drill-down page. */
async function installTurnDrilldownStubs(page: Page): Promise<void> {
  const encodedKey = encodeURIComponent(SESSION_KEY);
  const encodedTurn = encodeURIComponent(TURN_ID);
  await page.route(
    `**/admin/sessions/${encodedKey}/turns/${encodedTurn}/events*`,
    async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          events: FIXTURE_TURN_EVENTS,
          next_cursor: null,
        }),
      });
    },
  );
}

/** Stubs for the `/admin/logs` page. */
async function installLogsStubs(page: Page): Promise<void> {
  await page.route("**/admin/logs/stream*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      headers: {
        "content-type": "text/event-stream",
        "cache-control": "no-cache",
      },
      body: buildSseLogsBody(),
    });
  });
}

/** Stubs for the `/admin/providers` page. */
async function installProvidersStubs(page: Page): Promise<void> {
  await page.route("**/admin/providers/custom*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ providers: [] }),
    });
  });
  await page.route(
    "**/admin/providers/openai/test",
    async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(PROVIDER_TEST_OK),
      });
    },
  );
  // Catch-all — list endpoint, also covers `/admin/providers?...`
  // refetches and the kinds descriptor the editor might pull.
  await page.route("**/admin/providers*", async (route: Route) => {
    const url = route.request().url();
    if (url.includes("/test") || url.includes("/models") || url.includes("/custom")) {
      return route.fallback();
    }
    if (url.includes("/kinds")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(PROVIDER_KINDS_RESPONSE),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(PROVIDERS_RESPONSE),
    });
  });
}

/** Stubs for the `/admin/credentials` page. */
async function installCredentialsStubs(page: Page): Promise<void> {
  await page.route("**/admin/oauth/status*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(OAUTH_STATUS_EMPTY),
    });
  });
  await page.route(
    "**/admin/credentials/openai/api_key/reveal",
    async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ value: REVEAL_VALUE }),
      });
    },
  );
  await page.route("**/admin/credentials*", async (route: Route) => {
    const url = route.request().url();
    if (url.includes("/reveal")) {
      return route.fallback();
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(CREDENTIALS_RESPONSE),
    });
  });
}

/** Stubs for the `/admin/models` page. */
async function installModelsStubs(page: Page): Promise<void> {
  await page.route(
    "**/admin/providers/openai/models",
    async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(PROVIDER_MODELS_RESPONSE),
      });
    },
  );
  await page.route("**/admin/providers/kinds", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(PROVIDER_KINDS_RESPONSE),
    });
  });
  await page.route("**/admin/models*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(MODELS_V2_RESPONSE),
    });
  });
  // The picker also resolves `/admin/providers` when the caller doesn't
  // pass a `providers` prop. The models page does pass one, but install
  // the catch-all anyway in case a re-render briefly drops it.
  await page.route("**/admin/providers", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(PROVIDERS_RESPONSE),
    });
  });
}

/** Shared layout queries mounted outside the page under test. */
async function installAdminLayoutStubs(page: Page): Promise<void> {
  await page.route("**/admin/system/info*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(INFO_NO_UPDATE_RESPONSE),
    });
  });
  await page.route("**/admin/profiles*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([]),
    });
  });
  await page.route("**/admin/tenants*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ tenants: [], active: null }),
    });
  });
}

/** Stubs for the `/admin/persona` page and its nested model picker. */
async function installPersonaStubs(page: Page): Promise<void> {
  await installModelsStubs(page);
  await page.route("**/admin/channels/*/humanlike", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ enabled: false, persona_id: null }),
    });
  });
  await page.route("**/admin/personas*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(PERSONAS_RESPONSE),
    });
  });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test.describe("admin pages smoke — stubs only", () => {
  test.beforeEach(async ({ page }) => {
    await pinLocaleEn(page);
    await installAuthStubs(page);
    await installHealthStub(page);
    await installAdminLayoutStubs(page);
  });

  test("sessions list renders a row and clear-all button", async ({
    page,
  }) => {
    test.setTimeout(TEST_TIMEOUT_MS);
    const verify = attachStrictListeners(page);
    await installSessionsListStubs(page);

    await page.goto("/admin/sessions");

    // Header button.
    await expect(page.getByTestId("sessions-clear-all")).toBeVisible();
    // At least one stubbed row.
    const row = page.getByTestId(
      `session-row-${SESSIONS_LIST_RESPONSE.sessions[0]!.session_key}`,
    );
    await expect(row).toBeVisible();
    verify();
  });

  test("session detail renders past-turns + timeline + cost footer", async ({
    page,
  }) => {
    test.setTimeout(TEST_TIMEOUT_MS);
    const verify = attachStrictListeners(page);
    // Order: more-specific routes first so the catch-all sessions
    // list doesn't shadow them.
    await installSessionDetailStubs(page);
    await installSessionsListStubs(page);

    await page.goto(`/admin/sessions/detail?key=${encodeURIComponent(SESSION_KEY)}`);

    // Past-turns pill row — W1.2 + W2.3 wiring.
    const pills = page.getByTestId("past-turns-pills");
    await expect(pills).toBeVisible({ timeout: 10_000 });
    const firstPill = page.getByTestId(
      `past-turn-pill-${TURNS_LIST_RESPONSE.turns[0]!.turn_id}`,
    );
    await expect(firstPill).toBeVisible();

    // Live timeline mounts from the SSE stub.
    await expect(page.getByTestId("event-timeline")).toBeVisible();
    await expect(
      page.getByTestId("timeline-turn-card").first(),
    ).toBeVisible({ timeout: 10_000 });

    // Cost footer pills.
    await expect(page.getByTestId("cost-footer")).toBeVisible({
      timeout: 10_000,
    });
    verify();
  });

  test("turn drill-down renders summary + replay timeline body", async ({
    page,
  }) => {
    test.setTimeout(TEST_TIMEOUT_MS);
    const verify = attachStrictListeners(page);
    await installTurnDrilldownStubs(page);

    await page.goto(
      `/admin/sessions/turn?key=${encodeURIComponent(SESSION_KEY)}&turn=${encodeURIComponent(TURN_ID)}`,
    );

    await expect(page.getByTestId("turn-summary-card")).toBeVisible({
      timeout: 10_000,
    });
    const body = page.getByTestId("event-timeline-body");
    await expect(body).toBeVisible();
    await expect(body).toHaveAttribute("data-mode", "replay");
    verify();
  });

  test("logs page mounts the stream + control bar", async ({ page }) => {
    test.setTimeout(TEST_TIMEOUT_MS);
    const verify = attachStrictListeners(page);
    await installLogsStubs(page);

    await page.goto("/admin/logs");

    // The log pane is the `role="log"` region with an aria-label —
    // a stable anchor across visual redesigns.
    const logPane = page.getByRole("log");
    await expect(logPane).toBeVisible({ timeout: 10_000 });

    // Time-range tablist exposes the filter pills.
    const tablist = page.getByRole("tablist").first();
    await expect(tablist).toBeVisible();
    verify();
  });

  test("providers list renders row + test-connection toast on click", async ({
    page,
  }) => {
    test.setTimeout(TEST_TIMEOUT_MS);
    const verify = attachStrictListeners(page);
    await installProvidersStubs(page);

    await page.goto("/admin/providers");

    const row = page.getByTestId("provider-row-openai");
    await expect(row).toBeVisible({ timeout: 10_000 });

    // The W2.3 test-connection button posts to /admin/providers/{name}/test.
    const testBtn = page.getByTestId("provider-test-btn-openai");
    await expect(testBtn).toBeVisible();
    await testBtn.click();

    // Sonner mounts toasts under [data-sonner-toast]; the success path
    // also flashes the button into `data-test-state="success"`. Either
    // signal is enough to prove the network call resolved.
    await expect(testBtn).toHaveAttribute("data-test-state", "success", {
      timeout: 10_000,
    });
    verify();
  });

  test("credentials page renders provider group + reveal cleartext", async ({
    page,
  }) => {
    test.setTimeout(TEST_TIMEOUT_MS);
    const verify = attachStrictListeners(page);
    await installCredentialsStubs(page);

    await page.goto("/admin/credentials");

    const card = page.getByTestId("credentials-provider-openai");
    await expect(card).toBeVisible({ timeout: 10_000 });

    // The eye-icon reveal hits W2.1's
    // `GET /admin/credentials/openai/api_key/reveal`. Click → cleartext
    // span renders.
    const revealBtn = page.getByTestId("cred-openai-api_key-reveal");
    await expect(revealBtn).toBeVisible();
    await revealBtn.click();
    const cleartext = page.getByTestId("cred-openai-api_key-preview-cleartext");
    await expect(cleartext).toBeVisible({ timeout: 10_000 });
    await expect(cleartext).toHaveText(REVEAL_VALUE);
    verify();
  });

  test("models page renders alias picker and opens it on click", async ({
    page,
  }) => {
    test.setTimeout(TEST_TIMEOUT_MS);
    const verify = attachStrictListeners(page);
    await installModelsStubs(page);

    await page.goto("/admin/models");

    // The "Change model…" toolbar button opens ModelPickerDialog.
    const pick = page.getByTestId("models-pick-btn");
    await expect(pick).toBeVisible({ timeout: 10_000 });
    await pick.click();
    await expect(page.getByTestId("model-picker-dialog")).toBeVisible();
    verify();
  });

  test("persona editor model picker options are clickable", async ({ page }) => {
    test.setTimeout(TEST_TIMEOUT_MS);
    const verify = attachStrictListeners(page);
    await installPersonaStubs(page);

    await page.goto("/persona");

    const newPersona = page.getByTestId("persona-new");
    await expect(newPersona).toBeVisible({ timeout: 10_000 });
    await newPersona.click();

    const editor = page.getByTestId("persona-editor");
    await expect(editor).toBeVisible();

    await page.getByTestId("persona-model-pick-text").click();
    const picker = page.getByTestId("model-picker-dialog");
    await expect(picker).toBeVisible();

    await page.getByTestId("model-picker-provider-openai").click();
    await page.getByTestId("model-picker-model-gpt-4o-mini").click();

    await expect(picker).toBeHidden();
    await expect(page.getByTestId("persona-model-binding-text")).toContainText(
      "openai / gpt-4o-mini",
    );
    verify();
  });
});
