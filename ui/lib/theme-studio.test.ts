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
  DESIGNER_THEMES,
  THEME_CSS_KEY,
  THEME_PARAMS_KEY,
  THEME_STYLE_ID,
  applyTheme,
  buildCanvasCss,
  buildThemeCss,
  getStoredThemeParams,
} from "./theme-studio";

afterEach(() => {
  applyTheme(null);
});

describe("theme studio", () => {
  it("caps canvas chroma at the taste guardrail no matter the input", () => {
    const css = buildCanvasCss({ accent: 100, canvas: 100, canvasChroma: 9 });
    // dark space-1 carries the full requested chroma — must be clamped
    const m = css.match(/\.dark\{--sg-space-0:oklch\(0\.115 ([0-9.]+) /);
    expect(m).not.toBeNull();
    expect(Number(m![1])).toBeLessThanOrEqual(0.045);
  });

  it("builds canvas overrides for both themes plus nebula + shadcn semantics", () => {
    const css = buildCanvasCss({ accent: 170, canvas: 215, canvasChroma: 0.03 });
    for (const v of [
      "--sg-space-0:",
      "--sg-nebula-1:",
      "--sg-glass-1-bg:",
      "--sg-inset-bg:",
      "--background:",
      "--card:",
    ]) {
      expect(css).toContain(v);
    }
    expect(css.indexOf(":root{")).toBeLessThan(css.indexOf(".dark{"));
  });

  it("full theme css = accent family + canvas family", () => {
    const css = buildThemeCss({ accent: 62, canvas: 45, canvasChroma: 0.018 });
    expect(css).toContain("--sg-accent:");
    expect(css).toContain("--sg-space-2:");
    // accent-only params skip the canvas block
    const accentOnly = buildThemeCss({ accent: 62 });
    expect(accentOnly).toContain("--sg-accent:");
    expect(accentOnly).not.toContain("--sg-space-2:");
  });

  it("obsidian-style intensity desaturates the accent family", () => {
    const vivid = buildThemeCss({ accent: 228 });
    const mono = buildThemeCss({ accent: 228, accentIntensity: 0.42 });
    expect(vivid).toContain("oklch(0.78 0.13 228)");
    expect(mono).toContain("oklch(0.78 0.055 228)");
  });

  it("applies, persists generated css verbatim, and clears", () => {
    const params = { accent: 170, canvas: 215, canvasChroma: 0.03, preset: "aurora" };
    applyTheme(params);
    const el = document.getElementById(THEME_STYLE_ID);
    expect(el).not.toBeNull();
    expect(window.localStorage.getItem(THEME_CSS_KEY)).toBe(el?.textContent);
    expect(getStoredThemeParams()?.preset).toBe("aurora");

    applyTheme(null);
    expect(document.getElementById(THEME_STYLE_ID)).toBeNull();
    expect(window.localStorage.getItem(THEME_CSS_KEY)).toBeNull();
    expect(window.localStorage.getItem(THEME_PARAMS_KEY)).toBeNull();
  });

  it("ships six designer themes with deep-space as the stock reset", () => {
    expect(DESIGNER_THEMES).toHaveLength(6);
    expect(DESIGNER_THEMES[0].id).toBe("deep-space");
    expect(DESIGNER_THEMES[0].params).toBeNull();
    for (const t of DESIGNER_THEMES.slice(1)) {
      expect(t.params?.preset).toBe(t.id);
    }
  });
});
