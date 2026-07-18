import { describe, expect, it, vi, afterEach } from "vitest";

import {
  DEFAULT_LANG,
  LANG_STORAGE_KEY,
  resolveInitialLang,
  resolvePreferredLang,
} from "./i18n";

describe("i18n language resolution", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    window.localStorage.clear();
  });

  it("keeps the initial client language aligned with static HTML", () => {
    window.localStorage.setItem(LANG_STORAGE_KEY, "en");
    vi.stubGlobal("navigator", { language: "en-US" });

    expect(resolveInitialLang()).toBe(DEFAULT_LANG);
  });

  it("resolves the persisted language after hydration", () => {
    window.localStorage.setItem(LANG_STORAGE_KEY, "en");

    expect(resolvePreferredLang()).toBe("en");
  });

  it("ignores navigator.language when nothing is stored (stays on the default)", () => {
    // Regression: a zh operator on an en-US browser used to be silently
    // flipped to English on first visit. Only the explicit toggle moves
    // the UI off the default now.
    window.localStorage.removeItem(LANG_STORAGE_KEY);
    vi.stubGlobal("navigator", { language: "en-US" });

    expect(resolvePreferredLang()).toBe(DEFAULT_LANG);

    vi.stubGlobal("navigator", { language: "zh-Hans-CN" });
    expect(resolvePreferredLang()).toBe(DEFAULT_LANG);
  });
});
