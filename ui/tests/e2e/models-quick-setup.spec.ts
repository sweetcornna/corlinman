/**
 * /models quick-setup dialog — stub-driven happy path (PR5).
 *
 * The full-stack onboarding chain lives in `00-onboard-to-admin.spec.ts`
 * behind `CORLINMAN_E2E=1`; this spec follows the `admin-pages-smoke`
 * pattern instead (stubs only, dev server on :3000) so the guided
 * ProviderSetupFlow's browser contract is exercised without a gateway:
 *
 *   1. /models mounts unconfigured → header "Quick setup" button + the
 *      inline empty-state flow on the providers tab.
 *   2. Dialog flow: preset → API key → probe (POST
 *      /admin/providers/probe-models) → pick all models → confirm
 *      (POST /admin/providers once, then one alias upsert per model)
 *      → save default.
 *   3. THE wire contract this PR exists for: the default write posts
 *      `{"default": …}` with NO `aliases` key — a bulk body here would
 *      wipe every alias name it omitted.
 *
 * Unstubbed /admin/* requests 404 against the dev server and fail the
 * test via the strict listeners; `/admin/profiles` returns a BARE ARRAY
 * (matches the real gateway shape).
 */

import { expect, test, type Page, type Route } from "@playwright/test";

import { pinLocaleEn } from "./helpers/auth";
import {
  OAUTH_STATUS_EMPTY,
  PROVIDER_MODELS_RESPONSE,
} from "./admin-pages-smoke._fixtures";

const TEST_TIMEOUT_MS = 30_000;

/** Console-error + failed-/admin/*-XHR listeners (admin-pages-smoke). */
function attachStrictListeners(page: Page): () => void {
  const consoleErrors: string[] = [];
  const requestFailures: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });
  page.on("requestfailed", (req) => {
    const url = req.url();
    if (url.includes("/admin/")) {
      const text = req.failure()?.errorText ?? "";
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

interface RecordedPosts {
  providers: unknown[];
  aliases: unknown[];
}

/**
 * Stub the full surface the /models page + setup flow touch. Returns the
 * recorded POST bodies so the test can assert the wire contract.
 */
async function installStubs(page: Page): Promise<RecordedPosts> {
  const recorded: RecordedPosts = { providers: [], aliases: [] };

  // ── auth + layout chrome ────────────────────────────────────────
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
  await page.route("**/health", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ status: "ok", version: "test", checks: [] }),
    });
  });
  await page.route("**/admin/system/info*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        current: "1.1.1",
        latest: "1.1.1",
        available: false,
        release_url: null,
        release_notes_md: null,
        published_at: null,
        last_checked_at: 1_716_540_000_000,
        prerelease_seen: [],
      }),
    });
  });
  // BARE ARRAY — the real gateway returns `[]`, not `{profiles: []}`.
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
  await page.route("**/admin/oauth/status*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(OAUTH_STATUS_EMPTY),
    });
  });

  // ── models: GET snapshot + POST alias/default writes ────────────
  await page.route("**/admin/models/aliases", async (route: Route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    recorded.aliases.push(body);
    if ("name" in body) {
      // Single alias upsert → AliasView echo.
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          name: body.name,
          provider: body.provider ?? "",
          model: body.model,
          params: {},
          effective_params_schema: { type: "object", properties: {} },
        }),
      });
      return;
    }
    // Default-only (or bulk) → status echo.
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "ok",
        default: body.default ?? "",
        aliases: {},
      }),
    });
  });
  await page.route("**/admin/models*", async (route: Route) => {
    if (route.request().url().includes("/aliases")) {
      return route.fallback();
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ default: "", aliases: [], providers: [] }),
    });
  });

  // ── providers: probe + upsert + lists ───────────────────────────
  await page.route("**/admin/providers/probe-models", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(PROVIDER_MODELS_RESPONSE),
    });
  });
  await page.route("**/admin/providers/custom*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ providers: [] }),
    });
  });
  await page.route("**/admin/providers*", async (route: Route) => {
    const req = route.request();
    const url = req.url();
    if (url.includes("/probe-models") || url.includes("/custom")) {
      return route.fallback();
    }
    if (req.method() === "POST") {
      const body = req.postDataJSON() as Record<string, unknown>;
      recorded.providers.push(body);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          name: body.name,
          kind: body.kind,
          enabled: body.enabled ?? true,
          base_url: body.base_url ?? null,
          api_key_source: "value",
          api_key_env_name: null,
          params: {},
          params_schema: { type: "object", properties: {} },
        }),
      });
      return;
    }
    // Unconfigured deployment: empty registry (also triggers the inline
    // empty-state flow on the providers tab).
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ providers: [] }),
    });
  });

  return recorded;
}

test.describe("models quick-setup — stubs only", () => {
  test.beforeEach(async ({ page }) => {
    await pinLocaleEn(page);
  });

  test("quick-setup dialog walks key → probe → models → default-only write", async ({
    page,
  }) => {
    test.setTimeout(TEST_TIMEOUT_MS);
    const verify = attachStrictListeners(page);
    const recorded = await installStubs(page);

    await page.goto("/models");

    // Unconfigured → the providers tab leads with the inline flow.
    await expect(page.getByTestId("model-hub-inline-setup")).toBeVisible({
      timeout: 10_000,
    });

    // Header CTA opens the guided flow in a dialog.
    await page.getByTestId("model-hub-quick-setup-btn").click();
    const dialog = page.getByTestId("model-hub-quick-setup-dialog");
    await expect(dialog).toBeVisible();

    // Step 1 → OpenAI preset (dialog-scoped: the inline flow renders the
    // same testids underneath).
    await dialog.getByTestId("setup-preset-openai").click();
    await expect(dialog.getByTestId("setup-name-input")).toHaveValue(
      "openai",
    );

    // Step 2 → literal API key.
    await dialog
      .getByTestId("setup-key-input")
      .fill("sk-e2e-stub-not-a-real-key");
    await dialog.getByTestId("setup-auth-next").click();

    // Step 3 → probe fetches the stubbed two-model catalog.
    await dialog.getByTestId("setup-probe-btn").click();
    await expect(
      dialog.getByTestId("setup-model-checkbox-gpt-4o"),
    ).toBeVisible({ timeout: 10_000 });

    // Step 4 → select all, confirm.
    await dialog.getByTestId("setup-select-all").check();
    await dialog.getByTestId("setup-add-models-btn").click();

    // Step 5 → first added alias preselected; save the default.
    await expect(
      dialog.getByTestId("setup-default-radio-gpt-4o"),
    ).toBeChecked({ timeout: 10_000 });
    await dialog.getByTestId("setup-save-default-btn").click();
    await expect(dialog.getByTestId("setup-done")).toBeVisible({
      timeout: 10_000,
    });

    // ── wire contract ────────────────────────────────────────────
    // Provider persisted exactly once with the typed key.
    expect(recorded.providers).toHaveLength(1);
    expect(recorded.providers[0]).toMatchObject({
      name: "openai",
      kind: "openai",
      api_key: { value: "sk-e2e-stub-not-a-real-key" },
    });
    // One single-shape alias upsert per picked model, bound to the
    // provider — then ONE default-only write carrying NO aliases key.
    expect(recorded.aliases).toHaveLength(3);
    expect(recorded.aliases[0]).toMatchObject({
      name: "gpt-4o",
      provider: "openai",
      model: "gpt-4o",
    });
    expect(recorded.aliases[1]).toMatchObject({
      name: "gpt-4o-mini",
      provider: "openai",
      model: "gpt-4o-mini",
    });
    expect(recorded.aliases[2]).toEqual({ default: "gpt-4o" });
    expect(recorded.aliases[2]).not.toHaveProperty("aliases");

    // Finish closes the dialog.
    await dialog.getByTestId("setup-finish").click();
    await expect(dialog).toBeHidden();
    verify();
  });
});
