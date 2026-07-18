import { beforeEach, describe, expect, it } from "vitest";
import {
  LEGACY_THEME_KEYS,
  TINT_PRESETS,
  TINT_STORAGE_KEY,
  TINT_STYLE_ID,
  applyTint,
  buildTintCss,
  getStoredTint,
} from "./tint";

beforeEach(() => {
  window.localStorage.clear();
  document.getElementById(TINT_STYLE_ID)?.remove();
  document.documentElement.removeAttribute("data-tint");
});

describe("buildTintCss", () => {
  it("locks L/C and emits both theme blocks with derived tint-ink", () => {
    const cssText = buildTintCss(210);
    // Paper light: deepened to L 0.55 → white ink.
    expect(cssText).toContain(":root:not(.dark){--sg-tint:oklch(0.55 0.09 210)");
    expect(cssText).toContain("--sg-tint-ink:#fff");
    // Dark: L 0.85 → black ink.
    expect(cssText).toContain(".dark{--sg-tint:oklch(0.85 0.09 210)");
    expect(cssText).toContain("--sg-tint-ink:#000");
    // Glow/soft derive from the same hue.
    expect(cssText).toContain("oklch(0.85 0.09 210 / 0.42)");
    expect(cssText).toContain("oklch(0.55 0.09 210 / 0.3)");
  });

  it("normalizes hue into 0-359", () => {
    expect(buildTintCss(-30)).toContain("oklch(0.85 0.09 330)");
    expect(buildTintCss(390)).toContain("oklch(0.85 0.09 30)");
  });
});

describe("applyTint", () => {
  it("applies a preset via data-tint and persists it", () => {
    applyTint({ preset: "ice" });
    expect(document.documentElement.getAttribute("data-tint")).toBe("ice");
    expect(document.getElementById(TINT_STYLE_ID)).toBeNull();
    expect(JSON.parse(window.localStorage.getItem(TINT_STORAGE_KEY)!)).toEqual({
      preset: "ice",
    });
    expect(getStoredTint()).toEqual({ preset: "ice" });
  });

  it("applies a custom hue via the injected style block", () => {
    applyTint({ hue: 123 });
    expect(document.documentElement.getAttribute("data-tint")).toBeNull();
    const style = document.getElementById(TINT_STYLE_ID);
    expect(style?.textContent).toContain("oklch(0.85 0.09 123)");
    expect(getStoredTint()).toEqual({ hue: 123 });
  });

  it("null restores moonlight: removes attribute, style and storage", () => {
    applyTint({ preset: "rose" });
    applyTint({ hue: 40 });
    applyTint(null);
    expect(document.documentElement.getAttribute("data-tint")).toBeNull();
    expect(document.getElementById(TINT_STYLE_ID)).toBeNull();
    expect(window.localStorage.getItem(TINT_STORAGE_KEY)).toBeNull();
    expect(getStoredTint()).toBeNull();
  });

  it("always purges the Spatial Glass era theme keys", () => {
    for (const key of LEGACY_THEME_KEYS) window.localStorage.setItem(key, "stale");
    applyTint({ preset: "moss" });
    for (const key of LEGACY_THEME_KEYS) {
      expect(window.localStorage.getItem(key)).toBeNull();
    }
    for (const key of LEGACY_THEME_KEYS) window.localStorage.setItem(key, "stale");
    applyTint(null);
    for (const key of LEGACY_THEME_KEYS) {
      expect(window.localStorage.getItem(key)).toBeNull();
    }
  });

  it("switching preset → hue → preset never leaves both mechanisms active", () => {
    applyTint({ preset: "dawn" });
    applyTint({ hue: 300 });
    expect(document.documentElement.getAttribute("data-tint")).toBeNull();
    expect(document.getElementById(TINT_STYLE_ID)).not.toBeNull();
    applyTint({ preset: "iris" });
    expect(document.documentElement.getAttribute("data-tint")).toBe("iris");
    expect(document.getElementById(TINT_STYLE_ID)).toBeNull();
  });
});

describe("getStoredTint", () => {
  it("rejects malformed or unknown values", () => {
    window.localStorage.setItem(TINT_STORAGE_KEY, "not-json{");
    expect(getStoredTint()).toBeNull();
    window.localStorage.setItem(TINT_STORAGE_KEY, JSON.stringify({ preset: "neon" }));
    expect(getStoredTint()).toBeNull();
    window.localStorage.setItem(TINT_STORAGE_KEY, JSON.stringify({ hue: "red" }));
    expect(getStoredTint()).toBeNull();
  });

  it("every preset id in TINT_PRESETS round-trips", () => {
    for (const p of TINT_PRESETS) {
      if (!p.preset) continue;
      applyTint({ preset: p.preset });
      expect(getStoredTint()).toEqual({ preset: p.preset });
    }
  });
});
