import type { Config } from "tailwindcss";

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

        // Tidepool — Phase 0 additive colour namespace.
        // Consumed by new primitive components in Phase 1+; flat names
        // with `tp-` prefix to avoid collisions with existing and with
        // Tailwind's default palette (e.g. `amber-*` is reserved).
        "tp-amber": "var(--tp-amber)",
        "tp-amber-soft": "var(--tp-amber-soft)",
        "tp-amber-glow": "var(--tp-amber-glow)",
        "tp-ember": "var(--tp-ember)",
        "tp-peach": "var(--tp-peach)",
        "tp-ok": "var(--tp-ok)",
        "tp-ok-soft": "var(--tp-ok-soft)",
        "tp-warn": "var(--tp-warn)",
        "tp-warn-soft": "var(--tp-warn-soft)",
        "tp-err": "var(--tp-err)",
        "tp-err-soft": "var(--tp-err-soft)",
        "tp-ink": "var(--tp-ink)",
        "tp-ink-2": "var(--tp-ink-2)",
        "tp-ink-3": "var(--tp-ink-3)",
        "tp-ink-4": "var(--tp-ink-4)",
        "tp-ink-5": "var(--tp-ink-5)",
        "tp-glass": "var(--tp-glass)",
        "tp-glass-2": "var(--tp-glass-2)",
        "tp-glass-3": "var(--tp-glass-3)",
        "tp-glass-edge": "var(--tp-glass-edge)",
        "tp-glass-edge-strong": "var(--tp-glass-edge-strong)",
        "tp-glass-hl": "var(--tp-glass-hl)",
        "tp-glass-inner": "var(--tp-glass-inner)",
        "tp-glass-inner-hover": "var(--tp-glass-inner-hover)",
        "tp-glass-inner-strong": "var(--tp-glass-inner-strong)",
        "tp-row-alt": "var(--tp-row-alt)",
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
        // Tidepool (Phase 0)
        "tp-panel": "var(--tp-shadow-panel)",
        "tp-hero": "var(--tp-shadow-hero)",
        "tp-primary": "var(--tp-shadow-primary)",
      },
      backgroundImage: {
        // Tidepool gradients (Phase 0)
        "tp-grad-text": "var(--tp-grad-text)",
        "tp-grad-border": "var(--tp-grad-border)",
        "tp-aurora":
          "radial-gradient(900px 500px at 15% 10%, var(--tp-aurora-1), transparent 60%), " +
          "radial-gradient(700px 500px at 85% 20%, var(--tp-aurora-2), transparent 60%), " +
          "radial-gradient(600px 400px at 50% 95%, var(--tp-aurora-3), transparent 60%), " +
          "linear-gradient(135deg, var(--tp-bg-a), var(--tp-bg-b) 60%, var(--tp-bg-c))",
      },
      // Glass retired: bg-tp-glass surfaces are transparent, blur/saturate neutralised.
      backdropBlur: {
        glass: "0",
        "glass-strong": "0",
      },
      backdropSaturate: {
        glass: "1",
        "glass-strong": "1",
      },
      transitionTimingFunction: {
        spring: "cubic-bezier(0.34, 1.56, 0.64, 1)",
        "tp-ease-out": "cubic-bezier(0.16, 1, 0.3, 1)",
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      fontFamily: {
        sans: ["var(--font-geist-sans)", "ui-sans-serif", "system-ui", "sans-serif"],
        // Tidepool (Phase 0): display serif for hero / streak / italic emphasis.
        // Loaded via next/font in app/layout.tsx as --font-instrument-serif.
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
        // Tidepool (Phase 0)
        "tp-tick-up": {
          "0%": { opacity: "0", transform: "translateY(8px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "tp-palette-in": {
          "0%": { opacity: "0", transform: "translateY(-12px) scale(0.98)" },
          "100%": { opacity: "1", transform: "translateY(0) scale(1)" },
        },
      },
      animation: {
        "accordion-down": "accordion-down 0.2s ease-out",
        "accordion-up": "accordion-up 0.2s ease-out",
        "fade-in": "fade-in 200ms ease-out",
        "pulse-glow": "pulse-glow 2s ease-in-out infinite",
        "count-up": "count-up 400ms cubic-bezier(0.34, 1.56, 0.64, 1)",
        "tp-tick-up": "tp-tick-up 800ms cubic-bezier(0.16, 1, 0.3, 1) both",
        "tp-palette-in":
          "tp-palette-in 260ms cubic-bezier(0.16, 1, 0.3, 1) both",
      },
    },
  },
  plugins: [require("tailwindcss-animate")],
};

export default config;
