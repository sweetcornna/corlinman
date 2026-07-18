import type { Config } from "tailwindcss";
import animate from "tailwindcss-animate";

// Eclipse Minimal v2. Pure-black monochrome + tint pipeline. MiSans / M PLUS 1
// / JetBrains Mono are injected via `app/fonts.ts` as CSS variables consumed
// here. backdrop-filter is banned app-wide: the core plugins are disabled so
// no backdrop-blur-* / backdrop-saturate-* class can even be generated.
const config: Config = {
  darkMode: ["class"],
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  corePlugins: {
    backdropBlur: false,
    backdropSaturate: false,
  },
  theme: {
    container: {
      center: true,
      padding: "2rem",
      screens: { "2xl": "1400px" },
    },
    extend: {
      colors: {
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        // primary/ring follow the tint pipeline — a runtime-swappable full
        // color, so they bypass the HSL-triplet convention entirely.
        ring: "color-mix(in oklch, var(--sg-tint) calc(<alpha-value> * 100%), transparent)",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT:
            "color-mix(in oklch, var(--sg-tint) calc(<alpha-value> * 100%), transparent)",
          foreground: "var(--sg-tint-ink)",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        "accent-2": "var(--sg-accent-2)",
        "accent-3": "var(--sg-accent-3)",
        popover: {
          DEFAULT: "hsl(var(--popover))",
          foreground: "hsl(var(--popover-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
        panel: "hsl(var(--panel))",
        surface: "hsl(var(--surface))",
        ok: "hsl(var(--ok))",
        warn: "hsl(var(--warn))",
        err: "hsl(var(--err))",
        state: {
          hover: "hsl(var(--state-hover))",
          focus: "hsl(var(--state-focus))",
          press: "hsl(var(--state-press))",
          loading: "hsl(var(--state-loading))",
          skeleton: "hsl(var(--state-skeleton))",
          empty: "hsl(var(--state-empty))",
          error: "hsl(var(--state-error))",
        },

        // Eclipse — canonical sg-* namespace (names unchanged from Spatial
        // Glass so existing call sites re-skin via token values).
        // Opaque tokens are wrapped in color-mix so Tailwind opacity
        // modifiers compose (e.g. border-sg-tint/30, ring-sg-err/40);
        // a bare class resolves to calc(1 * 100%) = the token itself.
        // Alpha-baked tokens (-soft/-glow/fills/borders) stay raw var()
        // and must NOT take /NN modifiers (they would silently no-op).
        "sg-tint": "color-mix(in oklch, var(--sg-tint) calc(<alpha-value> * 100%), transparent)",
        "sg-tint-ink": "var(--sg-tint-ink)",
        "sg-tint-soft": "var(--sg-tint-soft)",
        "sg-tint-glow": "var(--sg-tint-glow)",
        "sg-accent": "color-mix(in oklch, var(--sg-accent) calc(<alpha-value> * 100%), transparent)",
        "sg-accent-soft": "var(--sg-accent-soft)",
        "sg-accent-glow": "var(--sg-accent-glow)",
        "sg-accent-2": "color-mix(in oklch, var(--sg-accent-2) calc(<alpha-value> * 100%), transparent)",
        "sg-accent-2-soft": "var(--sg-accent-2-soft)",
        "sg-accent-3": "color-mix(in oklch, var(--sg-accent-3) calc(<alpha-value> * 100%), transparent)",
        "sg-accent-3-soft": "var(--sg-accent-3-soft)",
        "sg-ok": "color-mix(in oklch, var(--sg-ok) calc(<alpha-value> * 100%), transparent)",
        "sg-ok-soft": "var(--sg-ok-soft)",
        "sg-warn": "color-mix(in oklch, var(--sg-warn) calc(<alpha-value> * 100%), transparent)",
        "sg-warn-soft": "var(--sg-warn-soft)",
        "sg-err": "color-mix(in oklch, var(--sg-err) calc(<alpha-value> * 100%), transparent)",
        "sg-err-soft": "var(--sg-err-soft)",
        "sg-ink": "color-mix(in oklch, var(--sg-ink) calc(<alpha-value> * 100%), transparent)",
        "sg-ink-2": "color-mix(in oklch, var(--sg-ink-2) calc(<alpha-value> * 100%), transparent)",
        "sg-ink-3": "color-mix(in oklch, var(--sg-ink-3) calc(<alpha-value> * 100%), transparent)",
        "sg-ink-4": "color-mix(in oklch, var(--sg-ink-4) calc(<alpha-value> * 100%), transparent)",
        "sg-ink-5": "color-mix(in oklch, var(--sg-ink-5) calc(<alpha-value> * 100%), transparent)",
        "sg-row-alt": "var(--sg-row-alt)",
        "sg-border": "var(--sg-border)",
        "sg-border-strong": "var(--sg-border-strong)",
        "sg-border-ghost": "var(--sg-border-ghost)",
        "sg-highlight": "var(--sg-highlight)",
        "sg-shell": "var(--sg-glass-1-bg)",
        "sg-card": "var(--sg-glass-2-bg)",
        "sg-card-strong": "var(--sg-glass-2-bg-strong)",
        "sg-card-weak": "var(--sg-glass-2-bg-weak)",
        "sg-overlay": "var(--sg-glass-3-bg)",
        "sg-opaque": "var(--sg-glass-opaque)",
        "sg-inset": "var(--sg-inset-bg)",
        "sg-inset-hover": "var(--sg-inset-bg-hover)",
        "sg-inset-strong": "var(--sg-inset-bg-strong)",
        "sg-space-0": "var(--sg-space-0)",
      },
      backgroundColor: {
        "state-hover": "hsl(var(--state-hover))",
        "state-focus": "hsl(var(--state-focus))",
        "state-press": "hsl(var(--state-press))",
        "state-loading": "hsl(var(--state-loading))",
        "state-skeleton": "hsl(var(--state-skeleton))",
        "state-empty": "hsl(var(--state-empty))",
        "state-error": "hsl(var(--state-error))",
      },
      boxShadow: {
        1: "var(--shadow-1)",
        2: "var(--shadow-2)",
        3: "var(--shadow-3)",
        "glow-primary": "var(--glow-primary)",
        // Eclipse elevation (dual-layer: contact + ambient, floating only)
        "sg-1": "var(--sg-elev-1)",
        "sg-2": "var(--sg-elev-2)",
        "sg-3": "var(--sg-elev-3)",
        "sg-4": "var(--sg-elev-4)",
        "sg-glow": "var(--sg-glow-primary)",
        "sg-primary": "var(--sg-shadow-primary)",
        // Eclipse light grammar
        "sg-edge": "var(--sg-edge-top)",
        "sg-edge-strong": "var(--sg-edge-top-strong)",
        "sg-well": "var(--sg-well)",
        "sg-well-soft": "var(--sg-well-soft)",
        "sg-lift": "var(--sg-lift)",
        "sg-scrim": "var(--sg-scrim-down)",
        "sg-bloom-1": "var(--sg-bloom-1)",
        "sg-bloom-2": "var(--sg-bloom-2)",
        "sg-bloom-3": "var(--sg-bloom-3)",
        // Selected/"most active" treatment (single source shared with the
        // .nav-active class via --sg-shadow-selected).
        "sg-selected": "var(--sg-shadow-selected)",
      },
      backgroundImage: {
        "sg-grad-text": "var(--sg-grad-text)",
        // Matte sheen (replaces the faux-glass card gradient; consumers of
        // bg-sg-card-grad re-skin without edits).
        "sg-card-grad": "var(--sg-card-sheen)",
        "sg-moonrise": "var(--sg-moonrise)",
      },
      transitionTimingFunction: {
        spring: "cubic-bezier(0.34, 1.56, 0.64, 1)",
        "sg-ease-out": "cubic-bezier(0.16, 1, 0.3, 1)",
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
        // Eclipse radius scale (values coincide with the st-* spec:
        // card 20 = sg-lg, sheet 28 = sg-xl).
        "sg-sm": "10px",
        "sg-md": "14px",
        "sg-lg": "20px",
        "sg-xl": "28px",
        "st-bubble": "var(--st-bubble-radius)",
        "st-card": "var(--st-card-radius)",
        "st-sheet": "var(--st-sheet-radius)",
        "st-pill": "var(--st-pill-radius)",
      },
      spacing: {
        "st-1": "var(--st-sp-1)",
        "st-2": "var(--st-sp-2)",
        "st-3": "var(--st-sp-3)",
        "st-4": "var(--st-sp-4)",
        "st-5": "var(--st-sp-5)",
        "st-6": "var(--st-sp-6)",
        "st-8": "var(--st-sp-8)",
        "st-10": "var(--st-sp-10)",
        "st-touch": "var(--st-touch-min)",
      },
      fontFamily: {
        sans: [
          "var(--font-misans)",
          "MiSans",
          "HarmonyOS Sans SC",
          "PingFang SC",
          "Noto Sans CJK SC",
          "Microsoft YaHei UI",
          "system-ui",
          "sans-serif",
        ],
        display: [
          "var(--font-mplus)",
          "M PLUS 1",
          "var(--font-misans)",
          "MiSans",
          "HarmonyOS Sans SC",
          "PingFang SC",
          "Noto Sans CJK SC",
          "system-ui",
          "sans-serif",
        ],
        // Transitional alias: legacy font-serif opt-ins render in the
        // display stack until the long-tail sweep renames them.
        serif: [
          "var(--font-mplus)",
          "M PLUS 1",
          "var(--font-misans)",
          "MiSans",
          "system-ui",
          "sans-serif",
        ],
        mono: [
          "var(--font-jetbrains-mono)",
          "JetBrains Mono",
          "ui-monospace",
          "SFMono-Regular",
          "Consolas",
          "Noto Sans Mono CJK SC",
          "monospace",
        ],
      },
      // Weight discipline: 400/500 only — hierarchy comes from the ink
      // scale, not weight. semibold/bold intentionally resolve to 500 so
      // every legacy call site complies without edits (user content
      // <strong> keeps element-level 700 via the MiSans 560-900 cut).
      fontWeight: {
        semibold: "500",
        bold: "500",
      },
      keyframes: {
        "accordion-down": {
          from: { height: "0" },
          to: { height: "var(--radix-accordion-content-height)" },
        },
        "accordion-up": {
          from: { height: "var(--radix-accordion-content-height)" },
          to: { height: "0" },
        },
        "fade-in": {
          from: { opacity: "0", transform: "translateY(4px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        "pulse-glow": {
          "0%, 100%": { boxShadow: "0 0 0 transparent" },
          "50%": { boxShadow: "var(--glow-primary)" },
        },
        "count-up": {
          "0%": { opacity: "0", transform: "translateY(6px)" },
          "60%": { opacity: "1", transform: "translateY(-2px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "sg-tick-up": {
          "0%": { opacity: "0", transform: "translateY(8px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "sg-palette-in": {
          "0%": { opacity: "0", transform: "translateY(-12px) scale(0.98)" },
          "100%": { opacity: "1", transform: "translateY(0) scale(1)" },
        },
        "sg-rise": {
          "0%": { opacity: "0", transform: "translateY(10px) scale(0.99)" },
          "100%": { opacity: "1", transform: "translateY(0) scale(1)" },
        },
      },
      animation: {
        "accordion-down": "accordion-down 0.2s ease-out",
        "accordion-up": "accordion-up 0.2s ease-out",
        "fade-in": "fade-in 200ms ease-out",
        "pulse-glow": "pulse-glow 2s ease-in-out infinite",
        "count-up": "count-up 400ms cubic-bezier(0.34, 1.56, 0.64, 1)",
        "sg-tick-up": "sg-tick-up 800ms cubic-bezier(0.16, 1, 0.3, 1) both",
        "sg-palette-in":
          "sg-palette-in 260ms cubic-bezier(0.16, 1, 0.3, 1) both",
        "sg-rise": "sg-rise 600ms cubic-bezier(0.16, 1, 0.3, 1) both",
      },
    },
  },
  plugins: [animate],
};

export default config;
