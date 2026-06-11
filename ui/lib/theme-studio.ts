/**
 * Theme Studio — full-theme customization on top of the Spatial Glass
 * token system.
 *
 * A theme is four numbers:
 *   accent          — oklch hue of the accent family (lib/accent.ts)
 *   accentIntensity — chroma multiplier for the accent family (1 = stock;
 *                     low values give the desaturated "mono premium" look)
 *   canvas          — oklch hue of the canvas family (deep-space gradient,
 *                     glass fills, inset wells, nebula base)
 *   canvasChroma    — canvas saturation, HARD-CAPPED at 0.045 — the taste
 *                     guardrail that keeps any user choice looking premium
 *                     (color belongs to the accents, not the canvas)
 *
 * The generated CSS override block is persisted verbatim in localStorage
 * (`corlinman-theme-css`) so the boot script in app/layout.tsx can inject
 * it before first paint with zero logic duplication. Params are stored
 * alongside (`corlinman-theme-studio`) for the picker UI state.
 */

import { buildAccentCss } from "./accent";

export const THEME_CSS_KEY = "corlinman-theme-css";
export const THEME_PARAMS_KEY = "corlinman-theme-studio";
export const THEME_STYLE_ID = "sg-accent-override"; // reuse the accent slot

export interface ThemeParams {
  accent: number;
  accentIntensity?: number;
  canvas?: number;
  canvasChroma?: number;
  /**
   * Glass fill opacity multiplier (0.55–1.4, default 1). Lower = clearer
   * Apple "clear" material; higher = denser frosted glass. Applies to
   * both themes; the opaque no-backdrop-filter fallback never scales.
   */
  glassOpacity?: number;
  /** Designer preset id, for picker UI state only. */
  preset?: string;
}

/** Art-directed designer themes. `params: null` = stock Deep Space. */
export const DESIGNER_THEMES: ReadonlyArray<{
  id: string;
  /** i18n key under nav.themeStudio.themes.* */
  labelKey: string;
  params: ThemeParams | null;
  /** Preview chips: [canvas, accent, companion]. */
  preview: [string, string, string];
}> = [
  {
    id: "deep-space",
    labelKey: "deepSpace",
    params: null,
    preview: ["oklch(0.13 0.03 266)", "oklch(0.78 0.13 230)", "oklch(0.7 0.17 300)"],
  },
  {
    id: "aurora",
    labelKey: "aurora",
    params: { preset: "aurora", accent: 170, canvas: 215, canvasChroma: 0.03 },
    preview: ["oklch(0.13 0.03 215)", "oklch(0.78 0.13 170)", "oklch(0.7 0.17 225)"],
  },
  {
    id: "dusk",
    labelKey: "dusk",
    params: { preset: "dusk", accent: 62, canvas: 45, canvasChroma: 0.018 },
    preview: ["oklch(0.13 0.018 45)", "oklch(0.78 0.13 62)", "oklch(0.7 0.17 117)"],
  },
  {
    id: "rose",
    labelKey: "rose",
    params: { preset: "rose", accent: 2, canvas: 350, canvasChroma: 0.022 },
    preview: ["oklch(0.13 0.022 350)", "oklch(0.78 0.13 2)", "oklch(0.7 0.17 57)"],
  },
  {
    id: "obsidian",
    labelKey: "obsidian",
    params: { preset: "obsidian", accent: 228, accentIntensity: 0.42, canvas: 270, canvasChroma: 0.004 },
    preview: ["oklch(0.12 0.004 270)", "oklch(0.78 0.055 228)", "oklch(0.85 0.03 213)"],
  },
  {
    id: "gilded",
    labelKey: "gilded",
    params: { preset: "gilded", accent: 86, canvas: 262, canvasChroma: 0.03 },
    preview: ["oklch(0.13 0.03 262)", "oklch(0.78 0.13 86)", "oklch(0.7 0.17 141)"],
  },
];

const CANVAS_CHROMA_CAP = 0.045;

function norm(h: number): number {
  return ((h % 360) + 360) % 360;
}

/** Rough oklch→hsl hue mapping for shadcn HSL-triplet tokens. */
function hslHue(oklchHue: number): number {
  return Math.round(norm(oklchHue - 18));
}

/**
 * Canvas family override — deep-space gradient, glass tiers, inset wells,
 * shadcn background/card semantics, and a nebula trio derived from the
 * accent pair so the backdrop glow always matches the chosen identity.
 */
export function buildCanvasCss(params: ThemeParams): string {
  const H = norm(params.canvas ?? 266);
  const C = Math.max(0.003, Math.min(params.canvasChroma ?? 0.03, CANVAS_CHROMA_CAP));
  const A = norm(params.accent);
  const A2 = norm(params.accent + 55);
  const k = Math.max(0.05, Math.min(params.accentIntensity ?? 1, 1.5));
  const g = Math.max(0.55, Math.min(params.glassOpacity ?? 1, 1.4));
  const ga = (alpha: number) => +Math.min(alpha * g, 0.96).toFixed(3);
  const hh = hslHue(H);
  const sat = Math.round(Math.min(C / 0.034, 1) * 32);
  const f = (n: number) => +n.toFixed(3);
  return [
    ":root{",
    // Light glass keeps its neutral white base — only the density scales.
    `--sg-glass-1-bg:oklch(1 0 0 / ${ga(0.55)});`,
    `--sg-glass-2-bg:oklch(1 0 0 / ${ga(0.6)});`,
    `--sg-glass-2-bg-strong:oklch(1 0 0 / ${ga(0.72)});`,
    `--sg-glass-2-bg-weak:oklch(1 0 0 / ${ga(0.45)});`,
    `--sg-glass-2-grad-a:oklch(1 0 0 / ${ga(0.72)});`,
    `--sg-glass-2-grad-b:oklch(0.98 0.005 270 / ${ga(0.5)});`,
    `--sg-glass-3-bg:oklch(0.99 0.004 270 / ${ga(0.78)});`,
    // Daylight haze keeps a whisper of the canvas hue.
    `--sg-space-0:oklch(0.965 ${f(Math.min(C * 0.3, 0.012))} ${H});`,
    `--sg-space-1:oklch(0.975 ${f(Math.min(C * 0.22, 0.009))} ${norm(H - 16)});`,
    `--sg-space-2:oklch(0.94 ${f(Math.min(C * 0.5, 0.018))} ${norm(H + 12)});`,
    `--sg-space-3:oklch(0.91 ${f(Math.min(C * 0.65, 0.024))} ${norm(H - 8)});`,
    `--background:${hh} ${Math.max(sat - 12, 6)}% 96%;`,
    "}",
    ".dark{",
    `--sg-space-0:oklch(0.115 ${f(C * 0.82)} ${H});`,
    `--sg-space-1:oklch(0.15 ${f(C)} ${norm(H - 4)});`,
    `--sg-space-2:oklch(0.1 ${f(C)} ${norm(H + 6)});`,
    `--sg-space-3:oklch(0.075 ${f(C * 0.7)} ${norm(H - 10)});`,
    `--sg-nebula-1:oklch(0.75 ${f(0.12 * k)} ${A} / 0.12);`,
    `--sg-nebula-2:oklch(0.65 ${f(0.13 * k)} ${A2} / 0.08);`,
    `--sg-nebula-3:oklch(0.6 ${f(Math.max(C * 2.5, 0.04))} ${H} / 0.08);`,
    `--sg-glass-1-bg:oklch(0.21 ${f(C * 0.94)} ${norm(H + 4)} / ${ga(0.55)});`,
    `--sg-glass-2-bg:oklch(0.23 ${f(C * 0.82)} ${norm(H + 4)} / ${ga(0.5)});`,
    `--sg-glass-2-bg-strong:oklch(0.25 ${f(C * 0.82)} ${norm(H + 4)} / ${ga(0.6)});`,
    `--sg-glass-2-bg-weak:oklch(0.2 ${f(C * 0.82)} ${norm(H + 4)} / ${ga(0.4)});`,
    `--sg-glass-2-grad-a:oklch(0.25 ${f(C * 0.82)} ${norm(H + 4)} / ${ga(0.55)});`,
    `--sg-glass-2-grad-b:oklch(0.17 ${f(C * 0.88)} ${norm(H + 2)} / ${ga(0.5)});`,
    `--sg-glass-3-bg:oklch(0.19 ${f(C * 1.06)} ${norm(H + 4)} / ${ga(0.7)});`,
    `--sg-glass-opaque:oklch(0.17 ${f(C * 0.88)} ${norm(H + 4)} / 0.95);`,
    `--sg-inset-bg:oklch(0.085 ${f(C * 0.7)} ${norm(H + 2)} / ${ga(0.5)});`,
    `--sg-inset-bg-hover:oklch(0.1 ${f(C * 0.82)} ${norm(H + 2)} / ${ga(0.55)});`,
    `--sg-inset-bg-strong:oklch(0.12 ${f(C * 0.82)} ${norm(H + 2)} / ${ga(0.6)});`,
    `--background:${hh} ${sat}% 8%;`,
    `--card:${hh} ${Math.max(sat - 7, 4)}% 16% / 0.45;`,
    `--popover:${hh} ${Math.max(sat - 4, 4)}% 13% / 0.75;`,
    `--secondary:${hh} ${Math.max(sat - 7, 4)}% 18% / 0.5;`,
    `--muted:${hh} ${Math.max(sat - 12, 4)}% 16% / 0.45;`,
    `--input:${hh} ${Math.max(sat - 7, 4)}% 8% / 0.5;`,
    "}",
  ].join("");
}

/** Full theme CSS = accent family + canvas family. */
export function buildThemeCss(params: ThemeParams): string {
  const accent = buildAccentCss(params.accent, params.accentIntensity ?? 1);
  const wantsCanvas =
    params.canvas != null ||
    (params.glassOpacity != null && params.glassOpacity !== 1);
  const canvas = wantsCanvas ? buildCanvasCss(params) : "";
  return accent + canvas;
}

export function getStoredThemeParams(): ThemeParams | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(THEME_PARAMS_KEY);
    if (!raw) return null;
    const p = JSON.parse(raw) as ThemeParams;
    return typeof p?.accent === "number" ? p : null;
  } catch {
    return null;
  }
}

/** Apply a theme (or null to restore stock Deep Space) and persist it. */
export function applyTheme(params: ThemeParams | null): void {
  if (typeof document === "undefined") return;
  try {
    if (params == null) {
      window.localStorage.removeItem(THEME_PARAMS_KEY);
      window.localStorage.removeItem(THEME_CSS_KEY);
      // legacy accent-only key
      window.localStorage.removeItem("corlinman-accent");
    } else {
      const css = buildThemeCss(params);
      window.localStorage.setItem(THEME_PARAMS_KEY, JSON.stringify(params));
      window.localStorage.setItem(THEME_CSS_KEY, css);
      window.localStorage.removeItem("corlinman-accent");
    }
  } catch {
    /* private mode — apply without persistence */
  }
  let el = document.getElementById(THEME_STYLE_ID) as HTMLStyleElement | null;
  if (params == null) {
    el?.remove();
    return;
  }
  if (!el) {
    el = document.createElement("style");
    el.id = THEME_STYLE_ID;
    document.head.appendChild(el);
  }
  el.textContent = buildThemeCss(params);
}
