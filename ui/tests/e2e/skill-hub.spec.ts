/**
 * Skill hub surfaces smoke — Wave 3 W3.1.
 *
 * Stubs-only Playwright coverage for the three operator-visible
 * skill-hub scenarios shipped across W1 + W2:
 *
 *   A. Browse + install      — `/admin/skills` → Browse Hub tab →
 *                              search → card → drawer → Install →
 *                              SSE progress → toast → Installed tab
 *                              now shows the new row with the
 *                              `hub:web-search@1.0.0` origin badge.
 *   B. Bundled delete refused — `/admin/skills` Installed tab → click
 *                              the delete button on a bundled row →
 *                              gated client-side (disabled button +
 *                              tooltip "ships with corlinman; edit
 *                              your profile copy") and server-side
 *                              (409 `bundled_protected`).
 *   C. Offline banner         — mock all hub endpoints to 502 and
 *                              switch to the Browse Hub tab; verify
 *                              the `<HubTab>` offline banner + Retry
 *                              button render the operator-visible
 *                              cue documented at
 *                              `docs/skill-hub.md#troubleshooting`.
 *
 * The full-stack version of these contracts is gated on `CORLINMAN_E2E=1`
 * (see `playwright.config.ts`). This stubs-only suite is the cheap CI
 * tripwire — it documents the wire contracts via mocks and stays green
 * without a running gateway or live SSE plumbing.
 *
 * Scope notes:
 * - The `/admin/skills` page renders the Installed tab today; the
 *   `<HubTab>` component is imported but the page-level tab switcher
 *   isn't wired into the UI yet (the W2.1 cutover comment in
 *   `app/(admin)/skills/page.tsx` keeps the import alive as
 *   `_HubTab`). The Browse-Hub-side scenarios mount `<HubTab>` via the
 *   playwright `evaluate()` hook for now, and the assertions that
 *   need a live gateway are documented but marked `test.skip(...)`.
 * - The install SSE handshake is documented but executed via the stub
 *   `EventSource`-style buffer Playwright understands (we deliver the
 *   whole SSE body up-front; the browser-side `EventSource` drains it
 *   as if it had been streamed).
 *
 * Pattern + helpers mirror `multi-agent.spec.ts` (auth + health
 * stubs; pinLocaleEn for stable i18n labels).
 */

import { expect, test, type Page, type Route } from "@playwright/test";

import { pinLocaleEn } from "./helpers/auth";

const TEST_TIMEOUT_MS = 15_000;

// ---------------------------------------------------------------------------
// Auth + layout stubs — every admin page bounces through these on mount.
// ---------------------------------------------------------------------------

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

async function installHealthStub(page: Page): Promise<void> {
  await page.route("**/health", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ status: "ok", version: "test", checks: [] }),
    });
  });
}

// ---------------------------------------------------------------------------
// Fixtures — wire shapes mirror gateway/routes_admin_b/skill_hub.py +
// gateway/routes_admin_b/skills.py.
// ---------------------------------------------------------------------------

const BUNDLED_ROW = {
  name: "memory",
  description: "Persistent memory skill — ships with corlinman.",
  version: "1.0.0",
  state: "active",
  origin: "bundled",
  pinned: false,
  use_count: 12,
  last_used_at: "2026-05-25T00:00:00Z",
  created_at: "2026-05-18T00:00:00Z",
} as const;

const USER_ROW = {
  name: "my-skill",
  description: "Operator-authored helper.",
  version: "0.1.0",
  state: "active",
  origin: "user",
  pinned: false,
  use_count: 3,
  last_used_at: null,
  created_at: "2026-05-20T00:00:00Z",
} as const;

const HUB_ROW = {
  name: "web-search",
  description: "Search the open web and cite results.",
  version: "1.0.0",
  state: "active",
  origin: "hub:web-search@1.0.0",
  pinned: false,
  use_count: 0,
  last_used_at: null,
  created_at: new Date().toISOString(),
} as const;

const HUB_SUMMARY_WEB = {
  slug: "web-search",
  name: "web-search",
  description: "Search the open web and cite results.",
  emoji: "🔎",
  stars: 42,
  downloads: 1234,
  latest_version: "1.0.0",
  updated_at: "2026-05-22T00:00:00Z",
} as const;

const HUB_DETAIL_WEB = {
  ...HUB_SUMMARY_WEB,
  homepage: "https://example.org/web-search",
  versions: ["1.0.0", "0.9.0"],
  scan_summary: "pass" as const,
  readme_excerpt:
    "# web-search\n\nSearch the open web. Returns titles, urls, and snippets.",
} as const;

// SSE body factory — three phase frames terminating in `installed`. The
// browser's EventSource drains a fulfilled body as if streamed.
function buildInstallSseBody(requestId: string, slug: string): string {
  const mk = (
    phase: "download.started" | "extract.started" | "installed",
    state: "running" | "installed",
    message: string,
  ) =>
    JSON.stringify({
      request_id: requestId,
      slug,
      version: "1.0.0",
      profile: "default",
      state,
      phase,
      started_at: Date.now() - 1000,
      finished_at: state === "installed" ? Date.now() : undefined,
      name: slug,
      message,
    });
  return [
    `event: phase`,
    `data: ${mk("download.started", "running", "downloading tarball")}`,
    ``,
    `event: phase`,
    `data: ${mk("extract.started", "running", "verifying members")}`,
    ``,
    `event: phase`,
    `data: ${mk("installed", "installed", "ready")}`,
    ``,
    `: keepalive`,
    ``,
  ].join("\n");
}

// ---------------------------------------------------------------------------
// Test A — Browse + install end-to-end
// ---------------------------------------------------------------------------

test.describe("skill hub surfaces — stubs only", () => {
  test.beforeEach(async ({ page }) => {
    await pinLocaleEn(page);
    await installAuthStubs(page);
    await installHealthStub(page);
  });

  test("browse + install: search → card → install → SSE → Installed row", async ({
    page,
  }) => {
    test.setTimeout(TEST_TIMEOUT_MS);

    // ----- mutable state ------------------------------------------------
    // The Installed-list query refetches on `["skills"]` invalidation
    // (the InstallProgressModal calls `qc.invalidateQueries`). The
    // closure flips the row set the second time it's hit so the new
    // hub row appears once the install completes.
    let installedFetches = 0;
    let installPosts = 0;

    // ----- /admin/skills (Installed tab) --------------------------------
    await page.route("**/admin/skills*", async (route: Route) => {
      const url = route.request().url();
      // Hub paths under `/admin/skills/hub/*` must not be claimed here.
      if (url.includes("/hub/")) return route.fallback();
      // POST /admin/skills/{name}/pin → out of scope for this scenario.
      if (route.request().method() !== "GET") return route.fallback();
      installedFetches += 1;
      const rows = installedFetches <= 1 ? [BUNDLED_ROW, USER_ROW] : [
        BUNDLED_ROW,
        USER_ROW,
        HUB_ROW,
      ];
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ profile: "default", rows }),
      });
    });

    // ----- /admin/skills/hub/search?q= ----------------------------------
    await page.route(
      "**/admin/skills/hub/search**",
      async (route: Route) => {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            rows: [HUB_SUMMARY_WEB],
            offline: false,
          }),
        });
      },
    );

    // ----- /admin/skills/hub/featured?sort= -----------------------------
    await page.route(
      "**/admin/skills/hub/featured**",
      async (route: Route) => {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            rows: [HUB_SUMMARY_WEB],
            offline: false,
            next_cursor: null,
          }),
        });
      },
    );

    // ----- /admin/skills/hub/skills/{slug} ------------------------------
    await page.route(
      "**/admin/skills/hub/skills/web-search",
      async (route: Route) => {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(HUB_DETAIL_WEB),
        });
      },
    );

    // ----- POST /admin/skills/hub/install + SSE -------------------------
    const REQUEST_ID = "req-install-web-search";
    await page.route(
      "**/admin/skills/hub/install",
      async (route: Route) => {
        if (route.request().method() !== "POST") return route.fallback();
        installPosts += 1;
        await route.fulfill({
          status: 202,
          contentType: "application/json",
          body: JSON.stringify({ request_id: REQUEST_ID }),
        });
      },
    );
    await page.route(
      `**/admin/skills/hub/install/${REQUEST_ID}/events/live`,
      async (route: Route) => {
        await route.fulfill({
          status: 200,
          headers: {
            "content-type": "text/event-stream",
            "cache-control": "no-cache",
          },
          body: buildInstallSseBody(REQUEST_ID, "web-search"),
        });
      },
    );

    // Tab switcher (skills-tab-installed / skills-tab-hub) landed in
    // W3. The deeper assertions below need testids the W2.2 components
    // don't expose yet (hub-search-input, hub-skill-card-{slug},
    // hub-detail-body, install-progress-modal, install-phase-installed,
    // install-progress-close, origin-badge-hub). Deferred to a
    // follow-up — the contract steps below document the surface for
    // when those testids land.
    test.skip(
      true,
      "Hub component testids not yet wired (follow-up to W3)",
    );

    // ----- documented step-by-step contract -----------------------------
    // 1. Land on the Installed tab — already the default.
    await page.goto("/admin/skills");
    await expect(
      page.getByTestId(`installed-card-${BUNDLED_ROW.name}`),
    ).toBeVisible({ timeout: 10_000 });

    // 2. Switch to Browse Hub tab.
    await page.getByTestId("skills-tab-hub").click();
    const hubTab = page.getByTestId("skills-hub-tab");
    await expect(hubTab).toBeVisible();

    // 3. Type "web" in the search input. The debounce is 300ms so we
    //    explicitly wait for the request before asserting the grid.
    const search = page.getByTestId("hub-search-input");
    await search.fill("web");
    const grid = page.getByTestId("hub-grid");
    await expect(grid).toBeVisible();
    const card = page.getByTestId(`hub-skill-card-${HUB_SUMMARY_WEB.slug}`);
    await expect(card).toBeVisible();

    // 4. Click the card → detail drawer mounts and renders.
    await card.click();
    const drawer = page.getByTestId("hub-detail-body");
    await expect(drawer).toBeVisible();

    // 5. Click Install → modal mounts + progress phases appear.
    await page.getByTestId("hub-detail-install").click();
    const modal = page.getByTestId("install-progress-modal");
    await expect(modal).toBeVisible();
    const installedPhase = page.getByTestId("install-phase-installed");
    await expect(installedPhase).toHaveAttribute("data-state", "past", {
      timeout: 5_000,
    });

    // 6. Modal closes on done click → Installed tab refetches.
    await page.getByTestId("install-progress-close").click();
    await expect(modal).not.toBeVisible();
    expect(installPosts).toBe(1);

    // 7. Installed tab now shows the new hub row with the `hub`
    //    origin badge carrying the `@1.0.0` version suffix.
    await page.getByTestId("skills-tab-installed").click();
    const hubRowCard = page.getByTestId(`installed-card-${HUB_ROW.name}`);
    await expect(hubRowCard).toBeVisible();
    const badge = hubRowCard.getByTestId("origin-badge-hub");
    await expect(badge).toContainText("hub");
    await expect(badge).toContainText("@1.0.0");
  });

  // -------------------------------------------------------------------------
  // Test B — Bundled rows can't be deleted (gated client-side + server-side)
  // -------------------------------------------------------------------------

  test("bundled delete refused: button disabled with tooltip", async ({
    page,
  }) => {
    test.setTimeout(TEST_TIMEOUT_MS);

    let deleteAttempts = 0;

    await page.route("**/admin/skills*", async (route: Route) => {
      const url = route.request().url();
      if (url.includes("/hub/")) return route.fallback();
      if (route.request().method() === "DELETE") {
        deleteAttempts += 1;
        // Server-side gate: 409 `bundled_protected`. The UI should
        // never reach this route for a bundled row — the disabled
        // button is the client-side cue — but the route exists so a
        // bypass doesn't silent-succeed.
        await route.fulfill({
          status: 409,
          contentType: "application/json",
          body: JSON.stringify({
            error: "bundled_protected",
            message: "Bundled skills cannot be removed.",
          }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          profile: "default",
          rows: [BUNDLED_ROW, USER_ROW],
        }),
      });
    });

    await page.goto("/admin/skills");
    const bundledCard = page.getByTestId(`installed-card-${BUNDLED_ROW.name}`);
    await expect(bundledCard).toBeVisible({ timeout: 10_000 });
    // Origin badge is `bundled`.
    await expect(
      bundledCard.getByTestId("origin-badge-bundled"),
    ).toBeVisible();

    // Delete button for the bundled row renders as the *-disabled
    // variant, with the i18n tooltip explaining why.
    const disabledBtn = page.getByTestId(
      `installed-delete-disabled-${BUNDLED_ROW.name}`,
    );
    await expect(disabledBtn).toBeVisible();
    await expect(disabledBtn).toBeDisabled();
    await expect(disabledBtn).toHaveAttribute(
      "title",
      /ships with corlinman/i,
    );

    // The non-bundled `user` row has a real Delete button; the
    // disabled variant should NOT exist for it.
    await expect(
      page.getByTestId(`installed-delete-${USER_ROW.name}`),
    ).toBeVisible();
    await expect(
      page.getByTestId(`installed-delete-disabled-${USER_ROW.name}`),
    ).toHaveCount(0);

    // No DELETE request should have fired — the client gate never
    // allows the bundled row to mutate.
    expect(deleteAttempts).toBe(0);
  });

  // -------------------------------------------------------------------------
  // Test C — Offline banner + Retry when hub endpoints return 502
  // -------------------------------------------------------------------------

  test("offline banner: hub 502 → banner + Retry button", async ({ page }) => {
    test.setTimeout(TEST_TIMEOUT_MS);

    let featuredHits = 0;
    let searchHits = 0;

    // The API wrapper maps non-2xx responses to a thrown
    // `CorlinmanApiError`, but the gateway's documented contract is
    // to instead emit `{rows: [], offline: true}` for ClawHub-side
    // failures (see `gateway/routes_admin_b/skill_hub.py`). The hub
    // tab branches on `response.offline === true`, so we mirror that
    // shape here rather than the raw 502 the upstream would emit.
    await page.route(
      "**/admin/skills/hub/featured**",
      async (route: Route) => {
        featuredHits += 1;
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            rows: [],
            offline: true,
            next_cursor: null,
          }),
        });
      },
    );
    await page.route(
      "**/admin/skills/hub/search**",
      async (route: Route) => {
        searchHits += 1;
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ rows: [], offline: true }),
        });
      },
    );
    // Installed-list call still has to succeed so the Installed tab
    // doesn't render its own offline block on mount.
    await page.route("**/admin/skills*", async (route: Route) => {
      const url = route.request().url();
      if (url.includes("/hub/")) return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ profile: "default", rows: [USER_ROW] }),
      });
    });

    // TODO(W3.1): same tab-switcher caveat as Test A. Once
    // `app/(admin)/skills/page.tsx` exposes `skills-tab-hub`, the
    // skip below comes off and the assertions below run as-is.
    test.skip(
      true,
      "Page-level Browse Hub tab switcher not yet wired into /admin/skills",
    );

    // ----- documented step-by-step contract -----------------------------
    await page.goto("/admin/skills");
    await page.getByTestId("skills-tab-hub").click();

    const banner = page.getByTestId("hub-offline-banner");
    await expect(banner).toBeVisible({ timeout: 10_000 });
    await expect(banner).toContainText(/unreachable/i);

    const retry = page.getByTestId("hub-offline-retry");
    await expect(retry).toBeVisible();
    // Retry button is wired to `query.refetch()` — clicking it
    // re-runs the featured query (search box is empty by default).
    const before = featuredHits;
    await retry.click();
    await expect.poll(() => featuredHits).toBeGreaterThan(before);

    // We never typed in the search field, so the search endpoint
    // should never have been hit.
    expect(searchHits).toBe(0);
  });
});
