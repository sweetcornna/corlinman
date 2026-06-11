/**
 * User-customizable accent color system.
 *
 * The whole design system flows through the `--sg-accent*` token family,
 * so a custom theme color is just a hue: we regenerate the accent tokens
 * (light + dark variants) from a single oklch hue and inject them as a
 * `<style id="sg-accent-override">` block — inline element vars would not
 * flip with `.dark`, a style block does.
 *
 * Persistence: localStorage `corlinman-accent` (stringified hue 0-359).
 * The boot script in app/layout.tsx applies the same CSS synchronously
 * before first paint (its tiny generator MUST stay in sync with
 * buildAccentCss below).
 */

export const ACCENT_STORAGE_KEY = "corlinman-accent";
export const ACCENT_STYLE_ID = "sg-accent-override";

/** Curated presets. `hue` is the oklch hue; null = restore the default. */
export const ACCENT_PRESETS: ReadonlyArray<{
  id: string;
  /** i18n key under nav.accent.presets.* */
  labelKey: string;
  hue: number | null;
  /** Swatch color shown in the picker (dark-theme accent). */
  swatch: string;
}> = [
  { id: "default", labelKey: "default", hue: null, swatch: "oklch(0.78 0.13 230)" },
  { id: "violet", labelKey: "violet", hue: 300, swatch: "oklch(0.78 0.13 300)" },
  { id: "emerald", labelKey: "emerald", hue: 165, swatch: "oklch(0.78 0.13 165)" },
  { id: "amber", labelKey: "amber", hue: 80, swatch: "oklch(0.78 0.13 80)" },
  { id: "rose", labelKey: "rose", hue: 15, swatch: "oklch(0.78 0.13 15)" },
  { id: "ocean", labelKey: "ocean", hue: 200, swatch: "oklch(0.78 0.13 200)" },
];

/** Rough oklch→hsl hue mapping for the shadcn `--primary`/`--ring` triplets. */
function hslHue(oklchHue: number): number {
  return Math.round((oklchHue - 18 + 360) % 360);
}

/**
 * Generate the override CSS for a given oklch hue. Mirrors the default
 * accent recipe in globals.css with the hue swapped (companion violet at
 * H+55, ice at H−15). `intensity` scales every accent chroma — 1 is the
 * stock vividness, lower values give desaturated "mono premium" looks
 * (used by the obsidian designer theme).
 */
export function buildAccentCss(hue: number, intensity = 1): string {
  const h = ((hue % 360) + 360) % 360;
  const h2 = (h + 55) % 360;
  const h3 = (h - 15 + 360) % 360;
  const hh = hslHue(h);
  const k = Math.max(0.05, Math.min(intensity, 1.5));
  const c = (base: number) => +(base * k).toFixed(3);
  const sat = (base: number) => Math.round(base * Math.min(k, 1));
  return [
    ":root{",
    `--sg-accent:oklch(0.5 ${c(0.16)} ${h});`,
    `--sg-accent-soft:oklch(0.5 ${c(0.16)} ${h} / 0.1);`,
    `--sg-accent-glow:oklch(0.55 ${c(0.16)} ${h} / 0.3);`,
    `--sg-accent-2:oklch(0.47 ${c(0.2)} ${h2});`,
    `--sg-accent-2-soft:oklch(0.47 ${c(0.2)} ${h2} / 0.1);`,
    `--sg-accent-3:oklch(0.55 ${c(0.1)} ${h3});`,
    `--sg-accent-3-soft:oklch(0.55 ${c(0.1)} ${h3} / 0.1);`,
    `--sg-grad-text:linear-gradient(115deg, oklch(0.46 ${c(0.17)} ${h}), oklch(0.52 ${c(0.12)} ${h3}) 45%, oklch(0.44 ${c(0.21)} ${h2}));`,
    `--primary:${hh} ${sat(70)}% 45%;`,
    `--ring:${hh} ${sat(75)}% 55%;`,
    "}",
    ".dark{",
    `--sg-accent:oklch(0.78 ${c(0.13)} ${h});`,
    `--sg-accent-soft:oklch(0.78 ${c(0.13)} ${h} / 0.14);`,
    `--sg-accent-glow:oklch(0.78 ${c(0.13)} ${h} / 0.45);`,
    `--sg-accent-2:oklch(0.7 ${c(0.17)} ${h2});`,
    `--sg-accent-2-soft:oklch(0.7 ${c(0.17)} ${h2} / 0.14);`,
    `--sg-accent-3:oklch(0.85 ${c(0.07)} ${h3});`,
    `--sg-accent-3-soft:oklch(0.85 ${c(0.07)} ${h3} / 0.14);`,
    `--sg-grad-text:linear-gradient(115deg, oklch(0.86 ${c(0.11)} ${h}), oklch(0.92 ${c(0.05)} ${h3}) 45%, oklch(0.76 ${c(0.17)} ${h2}));`,
    `--primary:${hh} ${sat(75)}% 70%;`,
    `--ring:${hh} ${sat(80)}% 70%;`,
    "}",
  ].join("");
}

/** Read the persisted hue, or null when the default palette is active. */
export function getStoredAccent(): number | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(ACCENT_STORAGE_KEY);
    if (raw == null || raw === "") return null;
    const n = Number(raw);
    return Number.isFinite(n) ? ((n % 360) + 360) % 360 : null;
  } catch {
    return null;
  }
}

/**
 * Apply (hue) or clear (null) the accent override, and persist the choice.
 */
export function applyAccent(hue: number | null): void {
  if (typeof document === "undefined") return;
  try {
    if (hue == null) window.localStorage.removeItem(ACCENT_STORAGE_KEY);
    else window.localStorage.setItem(ACCENT_STORAGE_KEY, String(Math.round(hue)));
  } catch {
    /* private mode — apply without persistence */
  }
  let el = document.getElementById(ACCENT_STYLE_ID) as HTMLStyleElement | null;
  if (hue == null) {
    el?.remove();
    return;
  }
  if (!el) {
    el = document.createElement("style");
    el.id = ACCENT_STYLE_ID;
    document.head.appendChild(el);
  }
  el.textContent = buildAccentCss(hue);
}
