import { afterEach, describe, expect, it } from "vitest";

// Node 22 jsdom backs localStorage with a shared on-disk store, so parallel
// test files race each other's keys. Replace with an in-memory store for
// full isolation.
const memStore = new Map<string, string>();
Object.defineProperty(window, "localStorage", {
  configurable: true,
  value: {
    getItem: (k: string) => memStore.get(k) ?? null,
    setItem: (k: string, v: string) => void memStore.set(k, String(v)),
    removeItem: (k: string) => void memStore.delete(k),
    clear: () => memStore.clear(),
    key: (i: number) => [...memStore.keys()][i] ?? null,
    get length() {
      return memStore.size;
    },
  },
});

import {
  ACCENT_PRESETS,
  ACCENT_STORAGE_KEY,
  ACCENT_STYLE_ID,
  applyAccent,
  buildAccentCss,
  getStoredAccent,
} from "./accent";

afterEach(() => {
  applyAccent(null);
  window.localStorage.removeItem(ACCENT_STORAGE_KEY);
});

describe("accent system", () => {
  it("builds both theme blocks with the full accent family", () => {
    const css = buildAccentCss(300);
    expect(css).toContain(":root{");
    expect(css).toContain(".dark{");
    for (const v of [
      "--sg-accent:",
      "--sg-accent-soft:",
      "--sg-accent-glow:",
      "--sg-accent-2:",
      "--sg-accent-3:",
      "--sg-grad-text:",
      "--primary:",
      "--ring:",
    ]) {
      expect(css).toContain(v);
    }
    expect(css).toContain("oklch(0.78 0.13 300)");
    // companion violet at H+55 wraps correctly
    expect(css).toContain("oklch(0.7 0.17 355)");
  });

  it("normalizes out-of-range hues", () => {
    expect(buildAccentCss(420)).toContain("oklch(0.78 0.13 60)");
    expect(buildAccentCss(-30)).toContain("oklch(0.78 0.13 330)");
  });

  it("applies, persists, and clears the override style block", () => {
    applyAccent(165);
    const el = document.getElementById(ACCENT_STYLE_ID);
    expect(el).not.toBeNull();
    expect(el?.textContent).toContain("oklch(0.78 0.13 165)");
    expect(getStoredAccent()).toBe(165);

    applyAccent(null);
    expect(document.getElementById(ACCENT_STYLE_ID)).toBeNull();
    expect(getStoredAccent()).toBeNull();
  });

  it("ships a default preset that maps to the reset action", () => {
    const def = ACCENT_PRESETS.find((p) => p.id === "default");
    expect(def?.hue).toBeNull();
    expect(ACCENT_PRESETS.length).toBeGreaterThanOrEqual(5);
  });
});
