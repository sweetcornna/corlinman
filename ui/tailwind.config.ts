import type { Config } from "tailwindcss";
import animate from "tailwindcss-animate";

// Linear-style redesign. Neutral base + indigo accent. Geist sans/mono fonts
// are injected via `app/layout.tsx` as CSS variables consumed here.
const config: Config = {
  darkMode: ["class"],
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
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
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
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
        "accent-2": "hsl(var(--accent-2))",
        "accent-3": "hsl(var(--accent-3))",
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

        // Spatial Glass — canonical sg-* namespace.
        // Opaque tokens are wrapped in color-mix so Tailwind opacity
        // modifiers compose (e.g. border-sg-accent/30, ring-sg-err/40);
        // a bare class resolves to calc(1 * 100%) = the token itself.
        // Alpha-baked tokens (-soft/-glow/fills/borders) stay raw var()
        // and must NOT take /NN modifiers (they would silently no-op).
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
        "sg-highlight": "var(--sg-highlight)",
        "sg-shell": "var(--sg-glass-1-bg)",
        "sg-card": "var(--sg-glass-2-bg)",
        "sg-card-strong": "var(--sg-glass-2-bg-strong)",
        "sg-card-weak": "var(--sg-glass-2-bg-weak)",
        "sg-overlay": "var(--sg-glass-3-bg)",
        "sg-inset": "var(--sg-inset-bg)",
        "sg-inset-hover": "var(--sg-inset-bg-hover)",
        "sg-inset-strong": "var(--sg-inset-bg-strong)",
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
        // Spatial Glass elevation
        "sg-1": "var(--sg-elev-1)",
        "sg-2": "var(--sg-elev-2)",
        "sg-3": "var(--sg-elev-3)",
        "sg-4": "var(--sg-elev-4)",
        "sg-glow": "var(--sg-glow-primary)",
        "sg-primary": "var(--sg-shadow-primary)",
      },
      backgroundImage: {
        // Spatial Glass
        "sg-grad-text": "var(--sg-grad-text)",
        "sg-grad-border": "var(--sg-grad-border)",
        "sg-card-grad":
          "linear-gradient(180deg, var(--sg-glass-2-grad-a), var(--sg-glass-2-grad-b))",
        "sg-aurora":
          "radial-gradient(900px 500px at 15% 10%, var(--sg-nebula-1), transparent 60%), " +
          "radial-gradient(700px 500px at 85% 20%, var(--sg-nebula-2), transparent 60%), " +
          "radial-gradient(600px 400px at 50% 95%, var(--sg-nebula-3), transparent 60%), " +
          "linear-gradient(135deg, var(--sg-space-1), var(--sg-space-2) 60%, var(--sg-space-3))",
      },
      // Blur budget: glass-strong consumers are all overlays → real blur.
      // sg-shell/sg-overlay are the canonical Spatial Glass tiers; the
      // legacy 0px `glass` tier was removed with its last consumers.
      backdropBlur: {
        "glass-strong": "28px",
        "sg-shell": "20px",
        "sg-overlay": "28px",
      },
      backdropSaturate: {
        "glass-strong": "1.5",
        "sg-shell": "1.4",
        "sg-overlay": "1.5",
      },
      transitionTimingFunction: {
        spring: "cubic-bezier(0.34, 1.56, 0.64, 1)",
        "sg-ease-out": "cubic-bezier(0.16, 1, 0.3, 1)",
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
        // Spatial Glass radius scale
        "sg-sm": "10px",
        "sg-md": "14px",
        "sg-lg": "20px",
        "sg-xl": "28px",
      },
      fontFamily: {
        sans: ["var(--font-geist-sans)", "ui-sans-serif", "system-ui", "sans-serif"],
        // Tidepool (Phase 0): display serif for hero / streak / italic emphasis.
        // Defined in globals.css with local system fallbacks so Docker builds
        // never need Google Fonts access.
        serif: [
          "var(--font-instrument-serif)",
          "Instrument Serif",
          "Georgia",
          "serif",
        ],
        mono: [
          "var(--font-geist-mono)",
          "ui-monospace",
          "SFMono-Regular",
          "monospace",
        ],
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
          "0%, 100%": { boxShadow: "0 0 0 rgb(var(--accent) / 0)" },
          "50%": { boxShadow: "var(--glow-primary)" },
        },
        "count-up": {
          "0%": { opacity: "0", transform: "translateY(6px)" },
          "60%": { opacity: "1", transform: "translateY(-2px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        // Spatial Glass
        "sg-tick-up": {
          "0%": { opacity: "0", transform: "translateY(8px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "sg-palette-in": {
          "0%": { opacity: "0", transform: "translateY(-12px) scale(0.98)" },
          "100%": { opacity: "1", transform: "translateY(0) scale(1)" },
        },
        // Spatial Glass — card entrance rise (staggered via delay).
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
        "sg-rise": "sg-rise 500ms cubic-bezier(0.16, 1, 0.3, 1) both",
      },
    },
  },
  plugins: [animate],
};

export default config;
