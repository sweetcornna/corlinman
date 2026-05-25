/**
 * W2.3 — i18n key parity for the skill-hub initiative.
 *
 * Locks in the contract that **every** key emitted by W2.1 (Installed tab),
 * W2.2 (Browse-hub tab) and the playground low-skill hint exists in BOTH
 * locale bundles, and that the Chinese bundle hasn't been silently shipped
 * with English fallbacks (a value equal to its dotted key means the
 * translator forgot it).
 *
 * The expected key catalog lives in :data:`EXPECTED_KEYS` below — the
 * orchestrator (main session) reconciles drift against W2.1/W2.2's real
 * `t("...")` inventory after both sibling agents report.
 *
 * Key-collision note. i18next's default separator is ``.`` — a key can be
 * either a string leaf OR an object node, not both. Two paths in the spec
 * collided:
 *
 *   - ``skills.installed.delete`` (button) vs.
 *     ``skills.installed.delete.confirm.*`` (modal) → flattened the modal
 *     branch under ``skills.installed.deleteConfirm.*``.
 *   - ``skills.origin.bundled`` (badge label) vs.
 *     ``skills.origin.bundled.tooltip`` → flattened to
 *     ``skills.origin.bundledTooltip``.
 *
 * Both renames are reflected in the expected catalog below so this test
 * matches the shipped shape, not the original spec.
 */

import { describe, expect, it } from "vitest";

import { en } from "../en";
import { zhCN } from "../zh-CN";

const EXPECTED_KEYS: readonly string[] = [
  // ── skills root ────────────────────────────────────────────────
  "skills.title",
  "skills.subtitle",

  // ── skills.installed.* (W2.1) ──────────────────────────────────
  // Reconciled against the components' actual t("…") inventory. The
  // W2.3 spec used nested groups (`search.placeholder`, `filter.all`,
  // `empty`, `deleteConfirm.*`); shipping components call flat names
  // such as `searchPlaceholder` / `filterAll` / `emptyTitle` /
  // `deleteConfirmTitle`, so the catalog tracks the flat names.
  "skills.installed.tab",
  "skills.installed.statTotal",
  "skills.installed.statBundled",
  "skills.installed.statUser",
  "skills.installed.statHub",
  "skills.installed.statFootTotal",
  "skills.installed.statFootBundled",
  "skills.installed.statFootUser",
  "skills.installed.statFootHub",
  "skills.installed.filterAll",
  "skills.installed.filterBundled",
  "skills.installed.filterUser",
  "skills.installed.filterHub",
  "skills.installed.filterPinned",
  "skills.installed.filterLabel",
  "skills.installed.searchPlaceholder",
  "skills.installed.emptyTitle",
  "skills.installed.emptyHint",
  "skills.installed.emptyFilteredTitle",
  "skills.installed.emptyFilteredHint",
  "skills.installed.offlineTitle",
  "skills.installed.offlineHint",
  "skills.installed.gridAria",
  "skills.installed.cardAria",
  "skills.installed.noDescription",
  "skills.installed.pin",
  "skills.installed.unpin",
  "skills.installed.delete",
  "skills.installed.bundledTooltip",
  // Flattened: `delete` leaf + `delete.confirm.*` would collide under
  // i18next's default separator, so the dialog strings live as flat
  // `deleteConfirm*` keys.
  "skills.installed.deleteConfirmTitle",
  "skills.installed.deleteConfirmBody",
  "skills.installed.deleteConfirmRetype",
  "skills.installed.deleteConfirmAction",
  "skills.installed.deleteSuccess",
  "skills.installed.deleteFailed",
  "skills.installed.pinFailed",

  // ── skills.origin.* (badges) ───────────────────────────────────
  "skills.origin.bundled",
  "skills.origin.user",
  "skills.origin.hub",
  // Renamed: was `skills.origin.bundled.tooltip` in the spec.
  "skills.origin.bundledTooltip",

  // ── skills.hub.* (W2.2) ────────────────────────────────────────
  "skills.hub.tab",
  "skills.hub.search.placeholder",
  "skills.hub.sort.label",
  "skills.hub.sort.trending",
  "skills.hub.sort.downloads",
  "skills.hub.sort.stars",
  "skills.hub.sort.updated",
  "skills.hub.gridLabel",
  "skills.hub.offline.title",
  "skills.hub.offline.hint",
  "skills.hub.offline.retry",
  "skills.hub.empty.searchTitle",
  "skills.hub.empty.searchHint",
  "skills.hub.empty.featuredTitle",
  "skills.hub.empty.featuredHint",
  "skills.hub.detail.close",
  "skills.hub.detail.install",
  "skills.hub.detail.homepage",
  "skills.hub.detail.loading",
  "skills.hub.detail.errorUnknown",
  "skills.hub.detail.scanTitle",
  "skills.hub.detail.scan.pass",
  "skills.hub.detail.scan.warn",
  "skills.hub.detail.scan.fail",
  "skills.hub.detail.versionsTitle",
  "skills.hub.detail.readmeTitle",

  // ── skills.hub.install.* (progress modal — W2.2) ───────────────
  "skills.hub.install.titleRunning",
  "skills.hub.install.titleDone",
  "skills.hub.install.titleFailed",
  "skills.hub.install.subtitle",
  "skills.hub.install.phase.download.started",
  "skills.hub.install.phase.extract.started",
  "skills.hub.install.phase.installed",
  "skills.hub.install.errorTitle",
  "skills.hub.install.errorUnknown",
  "skills.hub.install.errorStream",
  "skills.hub.install.toastSuccess",
  "skills.hub.install.retry",
  "skills.hub.install.close",
  "skills.hub.install.done",

  // ── playground.skills.hint.* (W2.3) ────────────────────────────
  "playground.skills.hint.title",
  "playground.skills.hint.body",
  "playground.skills.hint.cta",
] as const;

/** Walk a nested locale bundle and return the string at the dotted path. */
function resolve(bundle: unknown, path: string): string | undefined {
  const parts = path.split(".");
  let cursor: unknown = bundle;
  for (const part of parts) {
    if (cursor === null || typeof cursor !== "object") return undefined;
    cursor = (cursor as Record<string, unknown>)[part];
  }
  return typeof cursor === "string" ? cursor : undefined;
}

describe("skills hub i18n catalog (W2.3)", () => {
  describe.each(EXPECTED_KEYS)("%s", (key) => {
    it("is present in the en bundle", () => {
      const value = resolve(en, key);
      expect(value, `en is missing key ${key}`).toBeDefined();
      expect(value).not.toBe("");
    });

    it("is present in the zh-CN bundle", () => {
      const value = resolve(zhCN, key);
      expect(value, `zh-CN is missing key ${key}`).toBeDefined();
      expect(value).not.toBe("");
    });

    it("is translated in zh-CN (value ≠ key — catches forgotten translations)", () => {
      const value = resolve(zhCN, key);
      expect(value, `zh-CN missing translation for ${key}`).not.toBe(key);
    });
  });

  it("keeps interpolation placeholders aligned between en and zh-CN", () => {
    // Surface accidental drift like ``{{name}}`` in en but ``{{slug}}`` in
    // zh — same placeholders, same count, same names.
    const placeholderRe = /\{\{\s*([a-zA-Z0-9_]+)\s*\}\}/g;
    const mismatches: string[] = [];
    for (const key of EXPECTED_KEYS) {
      const enValue = resolve(en, key) ?? "";
      const zhValue = resolve(zhCN, key) ?? "";
      const enPlaceholders = [
        ...enValue.matchAll(placeholderRe),
      ]
        .map((m) => m[1])
        .sort();
      const zhPlaceholders = [
        ...zhValue.matchAll(placeholderRe),
      ]
        .map((m) => m[1])
        .sort();
      if (
        enPlaceholders.length !== zhPlaceholders.length ||
        enPlaceholders.some((p, i) => p !== zhPlaceholders[i])
      ) {
        mismatches.push(
          `${key}: en=[${enPlaceholders.join(",")}] zh=[${zhPlaceholders.join(",")}]`,
        );
      }
    }
    expect(mismatches, mismatches.join("\n")).toEqual([]);
  });
});
