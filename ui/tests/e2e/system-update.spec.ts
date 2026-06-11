/**
 * Auto-update flow E2E — Wave 3 W3.1 (auto-update plan).
 *
 * Stubs-only end-to-end coverage for the W1.2/W2.1 auto-update surface:
 *
 *   - `<UpdateBubble>` in the TopNav (polls `GET /admin/system/info`).
 *   - `/admin/system` page (version card + update banner + release notes
 *     + upgrade-commands tabs).
 *   - localStorage dismiss slot (`corlinman_update_dismissed_tag`).
 *
 * Three scenarios:
 *
 *   1. No update available — the bubble stays silent and the system page
 *      renders the "up to date" callout (no update-banner, no release
 *      notes). The upgrade-commands tabs are still rendered because the
 *      command blobs are deterministic and useful independent of an
 *      upstream poll.
 *
 *   2. Update available — the bubble lights up; clicking it navigates to
 *      `/admin/system`, which paints the amber update-banner, the
 *      sanitized release-notes markdown (heading, list, fenced bash
 *      block), and never injects a `<script>` (paranoid sanitization
 *      check). Switching the upgrade-commands tab to "Docker" updates
 *      the visible CopyUpgradeCommand `<pre>` body.
 *
 *   3. Dismissed-via-localStorage — even with the gateway reporting an
 *      update, a pre-existing `corlinman_update_dismissed_tag` matching
 *      the latest tag keeps the bubble silent across reloads. Clearing
 *      the slot + reloading brings the bubble back.
 *
 * Like `admin-pages-smoke.spec.ts`, every endpoint that the UI might
 * touch on mount is stubbed: any unmatched `/admin/*` XHR is a regression
 * (the strict-listener helper turns 404s into test failures).
 */

import { expect, test, type Page, type Route } from "@playwright/test";

import { pinLocaleEn } from "./helpers/auth";

const TEST_TIMEOUT_MS = 15_000;

// ---------------------------------------------------------------------------
// Fixtures — wire shape mirrors `UpdateStatus` / `UpgradeCommands` in
// `ui/lib/api.ts` (UpdateStatus, UpgradeCommands).
// ---------------------------------------------------------------------------

const UPGRADE_COMMANDS = {
  native: "bash deploy/install.sh --upgrade",
  docker: "bash deploy/install.sh --upgrade --mode docker",
  docker_with_qq: "bash deploy/install.sh --upgrade --mode docker --with-qq",
} as const;

const INFO_NO_UPDATE = {
  current: "1.1.1",
  latest: "1.1.1",
  available: false,
  release_url: null,
  release_notes_md: null,
  published_at: null,
  last_checked_at: 1_716_540_000_000,
  prerelease_seen: [] as string[],
} as const;

/**
 * Release notes deliberately exercise the surface we care about:
 *   - `## Highlights` heading + a list (markdown sanity)
 *   - A fenced bash code block (renders as `<pre>` + `<code>`)
 *   - Inline `code` (renders as `<code>`)
 *
 * The sanitization assertion is paranoid: the test verifies that no
 * `<script>` element ends up in the DOM. We don't put a literal
 * `<script>` in the markdown because `rehype-sanitize`'s default schema
 * blocks raw HTML — the assertion guards against accidental regressions
 * in the pipeline (e.g. someone swapping the plugin for `rehype-raw`).
 */
const RELEASE_NOTES_MD =
  "## Highlights\n\n" +
  "- Fix long-reply truncation\n" +
  "- Add `/admin/system` page\n\n" +
  "```bash\n" +
  "echo safe\n" +
  "```\n";

const INFO_UPDATE_AVAILABLE = {
  current: "1.1.1",
  latest: "1.1.2",
  available: true,
  release_url: "https://github.com/ymylive/corlinman/releases/tag/v1.1.2",
  release_notes_md: RELEASE_NOTES_MD,
  published_at: 1_716_530_000_000,
  last_checked_at: 1_716_540_000_000,
  prerelease_seen: [] as string[],
} as const;

// ---------------------------------------------------------------------------
// Strict listeners — same pattern as `admin-pages-smoke.spec.ts`. Any
// console error or `/admin/*` request failure fails the test at the end.
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Stubs — installed in `beforeEach` so every test starts from the same
// network surface. Per-test variants override the routes they care about
// by re-registering AFTER these (Playwright resolves most-recent-first).
// ---------------------------------------------------------------------------

/**
 * `/admin/me` — the layout guard `getSession()` hits this on every admin
 * route. `must_change_password: false` so the guard doesn't bounce us to
 * `/account/security`.
 */
async function installAuthStubs(page: Page): Promise<void> {
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

/**
 * `/health` — `HealthDot` in the TopNav polls this opportunistically.
 */
async function installHealthStub(page: Page): Promise<void> {
  await page.route("**/health", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ status: "ok", version: "test", checks: [] }),
    });
  });
}

/**
 * Dashboard (`/admin`) mounts several queries + an SSE log stream.
 * Stub them so navigating there from a test doesn't generate a forest
 * of strict-listener failures. The bodies are deliberately empty: this
 * spec doesn't care about the dashboard contents, only that the layout
 * and TopNav paint without throwing.
 */
async function installDashboardStubs(page: Page): Promise<void> {
  // SSE log stream — empty body, no events. EventSource will sit open.
  await page.route("**/admin/logs/stream*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      headers: {
        "content-type": "text/event-stream",
        "cache-control": "no-cache",
      },
      body: "\n",
    });
  });
  await page.route("**/admin/plugins", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([]),
    });
  });
  await page.route("**/admin/agents", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([]),
    });
  });
  await page.route("**/admin/rag/stats*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ chunks: 0, files: 0, tags: 0 }),
    });
  });
  await page.route("**/admin/approvals*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([]),
    });
  });
  // Command palette + sidebar accent queries — the palette lazy-loads
  // some lists; if those fire we don't want unmatched-route failures.
  await page.route("**/admin/profiles*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ profiles: [], active: null }),
    });
  });
  await page.route("**/admin/tenants*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ tenants: [], active: null }),
    });
  });
  await page.route("**/admin/health*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ status: "ok", version: "test", checks: [] }),
    });
  });
}

/**
 * Stub the auto-update endpoints with a caller-provided `UpdateStatus`.
 * `/info` is what `<UpdateBubble>` polls and what the system page reads;
 * `/check-updates` is the POST equivalent (force-refresh) and returns
 * the same shape; `/upgrade-commands` feeds the tabs on the system page.
 */
async function installSystemStubs(
  page: Page,
  info: typeof INFO_NO_UPDATE | typeof INFO_UPDATE_AVAILABLE,
): Promise<void> {
  await page.route("**/admin/system/info*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(info),
    });
  });
  await page.route("**/admin/system/check-updates*", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(info),
    });
  });
  await page.route(
    "**/admin/system/upgrade-commands*",
    async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(UPGRADE_COMMANDS),
      });
    },
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test.describe("auto-update flow — stubs only", () => {
  test.beforeEach(async ({ page }) => {
    await pinLocaleEn(page);
    await installAuthStubs(page);
    await installHealthStub(page);
    await installDashboardStubs(page);
  });

  test("no update — bubble silent + system page says up-to-date", async ({
    page,
  }) => {
    test.setTimeout(TEST_TIMEOUT_MS);
    const verify = attachStrictListeners(page);
    await installSystemStubs(page, INFO_NO_UPDATE);

    // Land on the dashboard so the TopNav (and therefore the bubble)
    // mounts. The bubble polls /admin/system/info on mount.
    await page.goto("/");

    // Wait for the layout to settle + the first /info poll to resolve
    // so the absence assertion isn't observing a pre-fetch state.
    await expect(page.getByTestId("mobile-nav-trigger")).toBeAttached({
      timeout: 10_000,
    });
    await page.waitForResponse((res) =>
      res.url().includes("/admin/system/info"),
    );
    // The bubble component returns `null` when no update is available —
    // no element with `data-testid="update-bubble"` should ever render.
    await expect(page.getByTestId("update-bubble")).toHaveCount(0);

    // Navigate to /system to assert the page-side surface.
    await page.goto("/system");
    await expect(page.getByTestId("system-page")).toBeVisible({
      timeout: 10_000,
    });
    // Current version pill shows the stub value.
    await expect(page.getByTestId("system-version-current")).toHaveText(
      INFO_NO_UPDATE.current,
    );
    // "Up to date" callout renders instead of the amber banner.
    await expect(page.getByTestId("system-up-to-date")).toBeVisible();
    await expect(page.getByTestId("system-update-banner")).toHaveCount(0);
    // Upgrade-commands card always renders (deterministic, no upstream
    // dependency). The tab strip is the most stable anchor.
    await expect(page.getByTestId("system-upgrade-tabs")).toBeVisible();
    await expect(page.getByTestId("copy-upgrade-command")).toBeVisible();

    verify();
  });

  test("update available — bubble + page banner + release notes", async ({
    page,
  }) => {
    test.setTimeout(TEST_TIMEOUT_MS);
    const verify = attachStrictListeners(page);
    await installSystemStubs(page, INFO_UPDATE_AVAILABLE);

    await page.goto("/");

    // Bubble lights up with the latest tag.
    const bubble = page.getByTestId("update-bubble");
    await expect(bubble).toBeVisible({ timeout: 10_000 });
    await expect(bubble).toContainText(INFO_UPDATE_AVAILABLE.latest);

    // Clicking the chip navigates to /system. The chip is a
    // `next/link`, so a normal click is enough.
    await bubble.click();
    await expect(page).toHaveURL(/\/system$/);
    await expect(page.getByTestId("system-page")).toBeVisible({
      timeout: 10_000,
    });

    // Update banner with the latest version called out in the title.
    const banner = page.getByTestId("system-update-banner");
    await expect(banner).toBeVisible();
    await expect(banner).toContainText(INFO_UPDATE_AVAILABLE.latest);

    // Sanitized release-notes container.
    const notes = page.getByTestId("release-notes");
    await expect(notes).toBeVisible();
    // The fixture's `## Highlights` heading + list items render.
    await expect(notes.getByRole("heading", { name: "Highlights" })).toBeVisible();
    await expect(notes.getByText("Fix long-reply truncation")).toBeVisible();
    // Second list item contains an inline `<code>` token wrapping
    // `/admin/system` — the inline code lives inside the same `<li>`,
    // so checking for the literal path text is the most reliable check.
    await expect(notes.getByText("/admin/system")).toBeVisible();
    // Fenced bash block becomes <pre><code>…</code></pre>. The `<pre>`
    // is the structural anchor; the inline `code` token also lives
    // inside it.
    await expect(notes.locator("pre")).toHaveCount(1);
    await expect(notes.locator("pre code")).toHaveText(/echo safe/);

    // Paranoid sanitization check — `rehype-sanitize` should strip any
    // raw HTML; the page itself must not embed a `<script>` element
    // (the document-wide assertion is the strongest signal we can make
    // without modifying the markdown to include a smuggled tag).
    await expect(page.locator("script[src='']")).toHaveCount(0);
    // No inline script under the release-notes container at all.
    await expect(notes.locator("script")).toHaveCount(0);

    // Switch to the "Docker" tab → CopyUpgradeCommand updates.
    await page.getByTestId("system-upgrade-tab-docker").click();
    const dockerPanel = page.getByTestId("system-upgrade-panel-docker");
    await expect(dockerPanel).toBeVisible();
    await expect(dockerPanel.getByTestId("copy-upgrade-command-pre")).toHaveText(
      UPGRADE_COMMANDS.docker,
    );

    verify();
  });

  test("dismissed via localStorage — bubble silent across reload", async ({
    page,
  }) => {
    test.setTimeout(TEST_TIMEOUT_MS);
    const verify = attachStrictListeners(page);
    await installSystemStubs(page, INFO_UPDATE_AVAILABLE);

    // Pre-seed the dismiss slot BEFORE the page loads. `addInitScript`
    // runs in every new document context (initial nav + reloads), so
    // the bubble's initial `useState(() => localStorage.get…)` reads
    // the dismissed tag synchronously on first render.
    const dismissedTag = INFO_UPDATE_AVAILABLE.latest;
    await page.addInitScript((tag) => {
      try {
        window.localStorage.setItem("corlinman_update_dismissed_tag", tag);
      } catch {
        /* private mode — ignore */
      }
    }, dismissedTag);

    await page.goto("/");

    // The bubble should not render at all (component returns null when
    // dismissedTag === data.latest). Wait for the TopNav to mount (the
    // mobile-nav-trigger lives in it) before asserting the absence —
    // a too-eager assertion would race the layout's auth check.
    await expect(page.getByTestId("mobile-nav-trigger")).toBeAttached({
      timeout: 10_000,
    });
    // Allow the bubble's first poll to resolve so we're not asserting
    // on a pre-fetch state.
    await page.waitForResponse((res) =>
      res.url().includes("/admin/system/info"),
    );
    await expect(page.getByTestId("update-bubble")).toHaveCount(0);

    // Reload → still silent (state persists in localStorage).
    await page.reload();
    await expect(page.getByTestId("mobile-nav-trigger")).toBeAttached({
      timeout: 10_000,
    });
    await page.waitForResponse((res) =>
      res.url().includes("/admin/system/info"),
    );
    await expect(page.getByTestId("update-bubble")).toHaveCount(0);

    // Clear the slot in-page and reload — the bubble should now appear
    // because the dismissed tag is gone. `evaluate` runs in the page's
    // origin so it can mutate the same storage the component reads.
    await page.evaluate(() => {
      window.localStorage.removeItem("corlinman_update_dismissed_tag");
    });
    await page.reload();
    await expect(page.getByTestId("update-bubble")).toBeVisible({
      timeout: 10_000,
    });

    verify();
  });
});
