/**
 * Eclipse tint pipeline — "换的是月光,不是夜空".
 *
 * Personalization never touches the monochrome skeleton (charcoal
 * surfaces, borders, body ink, ok/warn/err semantics). The user changes
 * only the "light": eclipse-orb corona, streaming thread, live dots,
 * solid primary buttons, selected states, progress bars, caret — all of
 * which flow through the `--sg-tint*` token quartet.
 *
 * Presets are pure CSS: `data-tint` on <html> selects a rule pair in
 * globals.css (dark L≈0.85 C≈0.09; Paper light deepens to L 0.55 for
 * ≥4.5:1 contrast). Custom hues inject the same two-block CSS as a
 * `<style id="sg-tint-override">` element with L/C locked — hue is the
 * only free axis.
 *
 * Persistence: localStorage `corlinman-tint`, JSON `{preset}` or `{hue}`.
 * The boot script in app/layout.tsx applies the same logic synchronously
 * before first paint (its generator MUST stay in sync with buildTintCss
 * below) and purges the Spatial Glass era keys, whose persisted CSS
 * would override Eclipse tokens.
 */

export const TINT_STORAGE_KEY = "corlinman-tint";
export const TINT_STYLE_ID = "sg-tint-override";

/** Spatial Glass era keys — their CSS blobs poison Eclipse tokens. */
export const LEGACY_THEME_KEYS = [
  "corlinman-accent",
  "corlinman-theme-css",
  "corlinman-theme-studio",
] as const;

export type TintPresetId = "dawn" | "ice" | "rose" | "moss" | "iris";

export type TintChoice = { preset: TintPresetId } | { hue: number } | null;

/**
 * Curated presets. `preset: null` = moonlight (default, pure white).
 * Swatches show the dark-theme tint.
 */
export const TINT_PRESETS: ReadonlyArray<{
  id: TintPresetId | "moonlight";
  /** i18n key under nav.tint.presets.* */
  labelKey: string;
  preset: TintPresetId | null;
  swatch: string;
}> = [
  { id: "moonlight", labelKey: "moonlight", preset: null, swatch: "#ffffff" },
  { id: "dawn", labelKey: "dawn", preset: "dawn", swatch: "oklch(0.85 0.09 85)" },
  { id: "ice", labelKey: "ice", preset: "ice", swatch: "oklch(0.85 0.07 230)" },
  { id: "rose", labelKey: "rose", preset: "rose", swatch: "oklch(0.8 0.09 15)" },
  { id: "moss", labelKey: "moss", preset: "moss", swatch: "oklch(0.84 0.09 150)" },
  { id: "iris", labelKey: "iris", preset: "iris", swatch: "oklch(0.8 0.1 300)" },
];

export const TINT_PRESET_IDS: ReadonlySet<string> = new Set(
  TINT_PRESETS.flatMap((p) => (p.preset ? [p.preset] : [])),
);

function normHue(hue: number): number {
  return ((Math.round(hue) % 360) + 360) % 360;
}

/**
 * Custom-hue override CSS. L/C are locked (dark 0.85/0.09, Paper 0.55)
 * so every custom tint stays in the "moonlight at a different hue"
 * envelope; tint-ink derives from the locked L (dark tints are light →
 * black ink; Paper tints are deep → white ink).
 */
export function buildTintCss(hue: number): string {
  const h = normHue(hue);
  return (
    `:root:not(.dark){--sg-tint:oklch(0.55 0.09 ${h});--sg-tint-ink:#fff;` +
    `--sg-tint-glow:oklch(0.55 0.09 ${h} / 0.3);--sg-tint-soft:oklch(0.55 0.09 ${h} / 0.08);}` +
    `.dark{--sg-tint:oklch(0.85 0.09 ${h});--sg-tint-ink:#000;` +
    `--sg-tint-glow:oklch(0.85 0.09 ${h} / 0.42);--sg-tint-soft:oklch(0.85 0.09 ${h} / 0.1);}`
  );
}

/** Read the persisted tint choice, or null when moonlight is active. */
export function getStoredTint(): TintChoice {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(TINT_STORAGE_KEY);
    if (!raw) return null;
    const v = JSON.parse(raw) as { preset?: unknown; hue?: unknown };
    if (typeof v?.preset === "string" && TINT_PRESET_IDS.has(v.preset)) {
      return { preset: v.preset as TintPresetId };
    }
    if (typeof v?.hue === "number" && Number.isFinite(v.hue)) {
      return { hue: normHue(v.hue) };
    }
    return null;
  } catch {
    return null;
  }
}

/**
 * Apply a tint choice (or null to restore moonlight) and persist it.
 * Always purges the Spatial Glass era theme keys.
 */
export function applyTint(next: TintChoice): void {
  if (typeof document === "undefined") return;
  try {
    for (const key of LEGACY_THEME_KEYS) window.localStorage.removeItem(key);
    if (next == null) window.localStorage.removeItem(TINT_STORAGE_KEY);
    else window.localStorage.setItem(TINT_STORAGE_KEY, JSON.stringify(next));
  } catch {
    /* private mode — apply without persistence */
  }
  const el = document.documentElement;
  const styleEl = document.getElementById(TINT_STYLE_ID);
  if (next && "preset" in next) {
    el.setAttribute("data-tint", next.preset);
    styleEl?.remove();
    return;
  }
  el.removeAttribute("data-tint");
  if (next && "hue" in next) {
    let style = styleEl as HTMLStyleElement | null;
    if (!style) {
      style = document.createElement("style");
      style.id = TINT_STYLE_ID;
      document.head.appendChild(style);
    }
    style.textContent = buildTintCss(next.hue);
    return;
  }
  styleEl?.remove();
}
