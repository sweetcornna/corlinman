/**
 * i18next bootstrap for the admin UI.
 *
 * Two locales: `zh-CN` (default, authoritative) and `en`.
 * Two language phases:
 *   - Initial hydration always uses `zh-CN`, because the exported static HTML
 *     is rendered in that default language.
 *   - After React mounts, the provider may switch to the persisted/browser
 *     preference. Deferring that switch prevents text hydration mismatches.
 *
 * Static export / SSG also uses `zh-CN` so the server-rendered HTML and the
 * first client render agree.
 */

import i18next from "i18next";
import { initReactI18next } from "react-i18next";

import { zhCN } from "./locales/zh-CN";
import { en } from "./locales/en";

export const LANG_STORAGE_KEY = "corlinman_lang";
export const SUPPORTED_LANGS = ["zh-CN", "en"] as const;
export type SupportedLang = (typeof SUPPORTED_LANGS)[number];
export const DEFAULT_LANG: SupportedLang = "zh-CN";

/** Resolve the first-render language. Must match the exported static HTML. */
export function resolveInitialLang(): SupportedLang {
  return DEFAULT_LANG;
}

/** Resolve the operator's preferred language after hydration is complete.
 *
 * Only an EXPLICIT choice (the stored toggle value) moves the UI off the
 * authoritative default. We deliberately do NOT fall back to
 * `navigator.language`: a zh operator on an en-US browser/OS (the common
 * dev-machine setup) would be silently flipped to English on first
 * visit, which reads as "mixed-language UI" — the toggle is one click
 * away for genuine English users. */
export function resolvePreferredLang(): SupportedLang {
  if (typeof window === "undefined") return DEFAULT_LANG;
  try {
    const stored = window.localStorage.getItem(LANG_STORAGE_KEY);
    if (stored === "zh-CN" || stored === "en") return stored;
  } catch {
    /* fall through */
  }
  return DEFAULT_LANG;
}

let initialized = false;

/** Initialise i18next once. Safe to call more than once. */
export function initI18n(): typeof i18next {
  if (initialized) return i18next;
  initialized = true;

  const initialLang = resolveInitialLang();

  i18next.use(initReactI18next).init({
    resources: {
      "zh-CN": { translation: zhCN },
      en: { translation: en },
    },
    lng: initialLang,
    fallbackLng: DEFAULT_LANG,
    supportedLngs: SUPPORTED_LANGS as readonly string[] as string[],
    interpolation: { escapeValue: false },
    returnNull: false,
    // Synchronous init — we bundle resources inline, there's nothing
    // async to wait for. Matters for vitest/SSG: React tests / static
    // export render before an async init promise would resolve. (v26
    // renamed `initImmediate` → `initAsync`, inverted semantics.)
    initAsync: false,
    react: { useSuspense: false },
  });

  // Keep <html lang> in sync so screen readers / search engines see it.
  if (typeof document !== "undefined") {
    document.documentElement.setAttribute("lang", initialLang);
    i18next.on("languageChanged", (lng) => {
      document.documentElement.setAttribute("lang", lng);
      try {
        window.localStorage.setItem(LANG_STORAGE_KEY, lng);
      } catch {
        /* storage disabled */
      }
    });
  }

  return i18next;
}

export { i18next };
