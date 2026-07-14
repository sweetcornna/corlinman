/**
 * Nav-registry contract tests (PR6 nav consolidation).
 *
 * The registry is the single source of truth for the admin page inventory;
 * these tests lock in the invariants every derived view relies on:
 *
 *   - unique ids + unique hrefs across NAV_PAGES;
 *   - every labelKey (pages, groups, aliases, section headers, dev-settings
 *     cards) resolves in BOTH locale bundles;
 *   - commandEntries() gates developer pages on dev mode and keeps the
 *     legacy /providers + /credentials muscle memory pointed at /models;
 *   - devSettingsPages() ⊆ developer pages;
 *   - segmentLabelKey() covers every segment of every registry href.
 */

import { describe, expect, it } from "vitest";

import { en } from "./locales/en";
import { zhCN } from "./locales/zh-CN";
import {
  NAV_ALIASES,
  NAV_GROUPS,
  NAV_PAGES,
  SECTION_LABEL_KEYS,
  commandEntries,
  devSettingsPages,
  navHrefs,
  segmentLabelKey,
  sidebarSections,
} from "./nav-registry";

/** Resolves a dotted i18n key against a raw locale bundle object. */
function resolveKey(bundle: object, key: string): string | undefined {
  let cur: unknown = bundle;
  for (const part of key.split(".")) {
    if (typeof cur !== "object" || cur === null) return undefined;
    cur = (cur as Record<string, unknown>)[part];
  }
  return typeof cur === "string" ? cur : undefined;
}

function expectKeyInBothLocales(key: string) {
  expect(resolveKey(en, key), `en missing ${key}`).toBeTypeOf("string");
  expect(resolveKey(zhCN, key), `zh-CN missing ${key}`).toBeTypeOf("string");
}

const DEV_PAGES = NAV_PAGES.filter((p) => p.developer === true);

describe("nav-registry inventory", () => {
  it("has unique page ids", () => {
    const ids = NAV_PAGES.map((p) => p.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("has unique page hrefs", () => {
    const hrefs = NAV_PAGES.map((p) => p.href);
    expect(new Set(hrefs).size).toBe(hrefs.length);
  });

  it("never re-lists the merged /providers and /credentials routes", () => {
    expect(navHrefs()).not.toContain("/providers");
    expect(navHrefs()).not.toContain("/credentials");
  });

  it("keeps the developer flag and the developer section consistent", () => {
    for (const page of NAV_PAGES) {
      expect(
        page.developer === true,
        `page ${page.id} developer/section mismatch`,
      ).toBe(page.section === "developer");
    }
  });

  it("resolves every labelKey in BOTH locale bundles", () => {
    for (const page of NAV_PAGES) expectKeyInBothLocales(page.labelKey);
    for (const group of NAV_GROUPS) expectKeyInBothLocales(group.labelKey);
    for (const alias of NAV_ALIASES) expectKeyInBothLocales(alias.labelKey);
    for (const key of Object.values(SECTION_LABEL_KEYS)) {
      expectKeyInBothLocales(key);
    }
  });
});

describe("sidebarSections", () => {
  it("returns chat/ops/config/system in operator mode (no developer)", () => {
    expect(sidebarSections(false).map((s) => s.id)).toEqual([
      "chat",
      "ops",
      "config",
      "system",
    ]);
  });

  it("appends the developer section in dev mode", () => {
    expect(sidebarSections(true).map((s) => s.id)).toEqual([
      "chat",
      "ops",
      "config",
      "system",
      "developer",
    ]);
  });

  it("places Models & Keys first in the config section", () => {
    const config = sidebarSections(false).find((s) => s.id === "config");
    const first = config?.entries[0];
    expect(first?.kind).toBe("item");
    if (first?.kind === "item") expect(first.page.id).toBe("models");
  });

  it("renders channels as the only collapsible group, with 7 leaves", () => {
    const groups = sidebarSections(true)
      .flatMap((s) => s.entries)
      .filter((e) => e.kind === "group");
    expect(groups).toHaveLength(1);
    expect(groups[0]?.id).toBe("channels");
    if (groups[0]?.kind === "group") {
      expect(groups[0].children).toHaveLength(7);
    }
  });

  it("covers every sectioned page exactly once (dev mode)", () => {
    const rendered = sidebarSections(true)
      .flatMap((s) => s.entries)
      .flatMap((e) => (e.kind === "group" ? e.children : [e.page]))
      .map((p) => p.id);
    const expected = NAV_PAGES.filter((p) => p.section).map((p) => p.id);
    expect(rendered.sort()).toEqual(expected.sort());
    expect(new Set(rendered).size).toBe(rendered.length);
  });
});

describe("commandEntries", () => {
  it("excludes developer pages when dev mode is off", () => {
    const hrefs = commandEntries(false).map((e) => e.href);
    for (const page of DEV_PAGES) {
      expect(hrefs, `dev page ${page.id} leaked`).not.toContain(page.href);
    }
  });

  it("includes every developer page when dev mode is on", () => {
    const hrefs = commandEntries(true).map((e) => e.href);
    for (const page of DEV_PAGES) {
      expect(hrefs, `dev page ${page.id} missing`).toContain(page.href);
    }
  });

  it("maps the legacy /providers + /credentials entries to /models", () => {
    const entries = commandEntries(false);
    const providers = entries.find((e) => e.id === "nav.providers");
    const credentials = entries.find((e) => e.id === "nav.credentials");
    expect(providers?.href).toBe("/models");
    expect(credentials?.href).toBe("/models");
    expect(providers?.keywords).toContain("openai");
  });

  it("has unique entry ids (legacy recents keep resolving)", () => {
    const ids = commandEntries(true).map((e) => e.id);
    expect(new Set(ids).size).toBe(ids.length);
    // Legacy hardcoded ids that persisted user recents must survive.
    for (const legacy of ["nav.dashboard", "nav.models", "nav.qq", "nav.logs"]) {
      expect(ids).toContain(legacy);
    }
  });
});

describe("devSettingsPages", () => {
  it("is a subset of the developer-gated pages", () => {
    const devIds = new Set(DEV_PAGES.map((p) => p.id));
    for (const page of devSettingsPages()) {
      expect(devIds.has(page.id), `${page.id} is not developer-gated`).toBe(
        true,
      );
    }
  });

  it("covers every developer page (drift fixed)", () => {
    expect(devSettingsPages().map((p) => p.id)).toEqual(
      DEV_PAGES.map((p) => p.id),
    );
  });

  it("has card copy for every page in BOTH locales", () => {
    for (const page of devSettingsPages()) {
      expectKeyInBothLocales(`devSettings.pages.${page.id}.title`);
      expectKeyInBothLocales(`devSettings.pages.${page.id}.description`);
    }
  });
});

describe("segmentLabelKey", () => {
  it("covers every segment of every registry href", () => {
    for (const page of NAV_PAGES) {
      for (const seg of (page.href as string).split("/").filter(Boolean)) {
        const key = segmentLabelKey(seg);
        expect(key, `segment "${seg}" (from ${page.href}) uncovered`).toBeTypeOf(
          "string",
        );
        expectKeyInBothLocales(key as string);
      }
    }
  });

  it("preserves the legacy non-page segments", () => {
    for (const seg of ["detail", "account", "security", "providers", "credentials"]) {
      const key = segmentLabelKey(seg);
      expect(key, `extra segment "${seg}" uncovered`).toBeTypeOf("string");
      expectKeyInBothLocales(key as string);
    }
  });

  it("returns undefined for unknown segments", () => {
    expect(segmentLabelKey("no-such-segment")).toBeUndefined();
  });
});
