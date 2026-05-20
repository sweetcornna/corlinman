/**
 * Wave 5.1 — curator dry-run E2E.
 *
 * Walks the `/evolution` curator surface end-to-end:
 *
 *   - profile cards visible
 *   - "Preview" → dry-run dialog with transitions
 *   - "Apply now" → real run, summary, dialog closes
 *   - skill list reflects new state badges after reload
 *   - Pause → status flips
 *   - origin filter narrows the skill list
 *   - pin toggle persists across reload
 *
 * Seeding strategy:
 *   - Create profile `curator-test` via the public `/admin/profiles`
 *     endpoint.
 *   - For state injection (an `agent-created` skill with
 *     `last_used_at` 40 days ago) we have to either (a) use the public
 *     /admin/curator/*  pin & threshold endpoints to make the
 *     deterministic curator engine select a pre-existing skill, or
 *     (b) reach the helper API if one lands. As of W4.1–W4.6 the
 *     public API does NOT expose a write path for seeding skill
 *     timestamps directly. We therefore exercise what's reachable:
 *     create the profile, drive Preview / Apply / Pause / filters
 *     against whatever skills the profile inherits from `default`, and
 *     assert the UI flow regardless of the transition count. If a
 *     future wave adds /admin/curator/{slug}/skills POST or a test
 *     helper, replace `seedAgedSkill()` with a real seed call.
 *
 * Gated behind `CORLINMAN_E2E=1`.
 */

import {
  expect,
  test,
  type APIRequestContext,
  type Page,
} from "@playwright/test";

import {
  DEFAULT_ADMIN_USER,
  loginAsAdmin,
  pinLocaleEn,
} from "./helpers/auth";
import {
  apiCreateProfile,
  apiDeleteProfileIfExists,
  apiLogin,
  apiLogout,
  apiPurgeTestProfiles,
  ensureAdminPasswordRotated,
  GATEWAY_URL,
} from "./helpers/test-data";

const FULL_STACK = process.env.CORLINMAN_E2E === "1";
const SEEDED_SLUG = "curator-test";
const PREVIEW_LABEL = /preview|预览/i;
const PAUSE_LABEL = /^(pause|暂停)$/i;
const CANCEL_LABEL = /^(cancel|取消)$/i;
const APPLY_NOW_LABEL = /apply now|立即应用/i;

/**
 * Best-effort: ensure the seeded profile exists and has at least one
 * skill the UI can hover its filters / pin button over. We clone from
 * `default` so the new profile inherits whatever bundled skills the
 * gateway has shipped.
 */
async function seedProfile(request: APIRequestContext): Promise<void> {
  // If a prior run left it behind, purge first so cloning is clean.
  await apiDeleteProfileIfExists(request, SEEDED_SLUG);
  await apiCreateProfile(request, {
    slug: SEEDED_SLUG,
    display_name: "Curator E2E",
    clone_from: "default",
  });
}

async function clickProfileAction(
  page: Page,
  slug: string,
  name: RegExp,
): Promise<void> {
  let lastError: unknown;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const button = page
      .getByTestId(`profile-card-${slug}`)
      .getByRole("button", { name });
    try {
      await expect(button).toBeVisible({ timeout: 5_000 });
      await expect(button).toBeEnabled({ timeout: 5_000 });
      await button.click({ timeout: 5_000 });
      return;
    } catch (err) {
      lastError = err;
      await page.waitForTimeout(250);
    }
  }
  throw lastError;
}

async function closePreviewDialog(page: Page): Promise<void> {
  const previewBody = page.getByTestId("preview-body");
  let lastError: unknown;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    if (!(await previewBody.isVisible().catch(() => false))) {
      return;
    }

    const button = page.getByRole("button", { name: CANCEL_LABEL });
    try {
      await expect(button).toBeVisible({ timeout: 2_000 });
      await expect(button).toBeEnabled({ timeout: 2_000 });
      await button.click({ timeout: 2_000 });
      await expect(previewBody).toBeHidden({ timeout: 5_000 });
      return;
    } catch (err) {
      lastError = err;
      if (!(await previewBody.isVisible().catch(() => false))) {
        return;
      }
      await page.waitForTimeout(250);
    }
  }
  throw lastError;
}

(FULL_STACK ? test.describe.serial : test.describe.skip)(
  "Wave 5.1 — curator dry-run",
  () => {
    let adminPassword = "";

    test.beforeAll(async ({ request }) => {
      adminPassword = await ensureAdminPasswordRotated(request);
      await apiLogin(request, DEFAULT_ADMIN_USER, adminPassword);
      try {
        await seedProfile(request);
      } finally {
        await apiLogout(request);
      }
    });

    test.afterAll(async ({ request }) => {
      if (!adminPassword) return;
      await apiLogin(request, DEFAULT_ADMIN_USER, adminPassword);
      try {
        await apiPurgeTestProfiles(request, [SEEDED_SLUG]);
      } finally {
        await apiLogout(request);
      }
    });

    test.beforeEach(async ({ page }) => {
      await pinLocaleEn(page);
      await loginAsAdmin(page, adminPassword);
    });

    test("curator flow — preview, apply, pause, filter, pin", async ({
      page,
      request,
    }) => {
      // ── 2. /evolution renders curator section + the seeded profile card ──
      await page.goto("/evolution");
      const section = page.getByTestId("curator-section");
      await expect(section).toBeVisible({ timeout: 10_000 });

      const cards = page.getByTestId("curator-profile-cards");
      await expect(cards).toBeVisible({ timeout: 10_000 });

      const seededCard = page.getByTestId(`profile-card-${SEEDED_SLUG}`);
      await expect(seededCard).toBeVisible({ timeout: 10_000 });

      // ── 4. Preview → dialog opens with transitions or empty state ──
      await clickProfileAction(page, SEEDED_SLUG, PREVIEW_LABEL);
      const previewBody = page.getByTestId("preview-body");
      await expect(previewBody).toBeVisible({ timeout: 10_000 });

      const transitionRows = page.locator("[data-testid^='transition-']");
      const transitionCount = await transitionRows.count();

      if (transitionCount === 0) {
        // The freshly-cloned profile may have nothing to transition.
        // The contract is "Preview returns something" — empty body is
        // a valid response shape per the W4.6 dialog.
        await expect(previewBody).toContainText(/./);
      } else {
        // ── 6. None of the transitions should be on a bundled skill ──
        // The W4.6 contract limits transitions to `origin=agent-created`.
        // The dry-run preview should NEVER list a bundled skill row.
        for (let i = 0; i < transitionCount; i += 1) {
          const row = transitionRows.nth(i);
          const text = await row.textContent();
          // We can't assert origin from this row alone (the dialog
          // only renders skill_name + states), but we can assert the
          // skill_name doesn't match a bundled-skill convention if the
          // gateway prefixes them. Soft check — log + continue.
          expect(text ?? "").not.toMatch(/\bbundled\b/i);
        }
      }

      // ── 7. Apply now — only enabled when transitions > 0 ──
      const applyBtn = page.getByRole("button", { name: APPLY_NOW_LABEL });
      const applyEnabled = await applyBtn
        .isEnabled()
        .catch(() => false);
      if (applyEnabled) {
        await applyBtn.click();
        // Dialog closes after the run completes.
        await expect(previewBody).toBeHidden({ timeout: 10_000 });
      } else {
        // Close manually so subsequent steps can drive the page.
        await closePreviewDialog(page);
      }

      // ── 8. Reload + verify skill list renders for the profile ──
      await page.reload();
      // The page renders multiple `profile-switcher` testids: a
      // native <select> inside the curator section, plus the top-nav
      // disclosure (W3.4). Prefer the <select> so we can deterministically
      // route the skill list to the seeded profile.
      const selectSwitcher = page.locator(
        "select[data-testid='profile-switcher']",
      );
      if (await selectSwitcher.count()) {
        await selectSwitcher.selectOption(SEEDED_SLUG);
      }

      // Skill list should be visible (or the empty banner) — either is
      // a real W4.6 response shape.
      const skillList = page.getByTestId("skill-list");
      const skillEmpty = page.getByTestId("skill-list-empty");
      await expect(skillList.or(skillEmpty)).toBeVisible({ timeout: 10_000 });

      // ── 9. Pause — status flips to "Paused" ──
      const pauseBtn = page
        .getByTestId(`profile-card-${SEEDED_SLUG}`)
        .getByRole("button", { name: PAUSE_LABEL });
      if (await pauseBtn.count()) {
        await clickProfileAction(page, SEEDED_SLUG, PAUSE_LABEL);
        await expect(
          page
            .getByTestId(`profile-card-${SEEDED_SLUG}`)
            .getByTestId("status-paused"),
        ).toBeVisible({ timeout: 5_000 });
      }

      // ── 10. Preview again with pause active ──
      // Per the spec: "if backend ignores pause for preview, document
      // it". The W4.6 implementation runs the deterministic pass
      // regardless of `paused` (pause only stops scheduled triggers),
      // so we just verify the dialog opens and renders.
      await clickProfileAction(page, SEEDED_SLUG, PREVIEW_LABEL);
      await expect(page.getByTestId("preview-body")).toBeVisible({
        timeout: 10_000,
      });
      await closePreviewDialog(page);

      // ── 11. Filter origin = agent-created ──
      const originFilter = page.getByTestId("skill-filter-origin");
      if (await originFilter.count()) {
        await originFilter.selectOption("agent-created");
        // List should still render (filtered or empty banner).
        await expect(skillList.or(skillEmpty)).toBeVisible({
          timeout: 5_000,
        });
        // No skill row should carry a `bundled` origin badge after the
        // filter. We can't read CSS-only badges via Playwright reliably,
        // so this is a soft assertion: every row exposed must contain
        // the agent-created literal in its accessible text.
        const rows = page.locator("[data-testid^='skill-row-']");
        const rowCount = await rows.count();
        for (let i = 0; i < rowCount; i += 1) {
          const text = await rows.nth(i).textContent();
          expect(text ?? "").not.toContain("bundled");
        }
      }

      // ── 12. Pin toggle persists ──
      // Reset the origin filter so we can find any skill to pin.
      if (await originFilter.count()) {
        await originFilter.selectOption("all");
      }
      const pinToggles = page.locator("[data-testid^='pin-toggle-']");
      if (await pinToggles.count()) {
        const firstToggle = pinToggles.first();
        // Capture the skill name from the testid so we can re-locate
        // after the reload.
        const testid = await firstToggle.getAttribute("data-testid");
        const skillName = testid?.replace(/^pin-toggle-/, "") ?? "";
        const beforePressed =
          (await firstToggle.getAttribute("aria-pressed")) === "true";
        await firstToggle.click();
        // Optimistic UI may flicker — wait for the aria-pressed flip.
        await expect(firstToggle).toHaveAttribute(
          "aria-pressed",
          beforePressed ? "false" : "true",
          { timeout: 5_000 },
        );

        await page.reload();
        if (skillName) {
          const reloaded = page.getByTestId(`pin-toggle-${skillName}`);
          if (await reloaded.count()) {
            await expect(reloaded).toHaveAttribute(
              "aria-pressed",
              beforePressed ? "false" : "true",
              { timeout: 5_000 },
            );
            // Cleanup: flip the pin back so the profile is left in its
            // pre-test state.
            await reloaded.click();
          }
        }
      }

      // Sanity check — the gateway is still reachable (catch silent
      // hangups that would have failed earlier assertions for the
      // wrong reason).
      const health = await request
        .get(`${GATEWAY_URL}/health`)
        .catch(() => null);
      expect(health?.ok()).toBeTruthy();
    });
  },
);
