import { describe, expect, it } from "vitest";
import config from "./tailwind.config";

describe("Eclipse tailwind invariants", () => {
  it("bans backdrop-filter at build time — no blur tiers, core plugins off", () => {
    const extend = config.theme?.extend;
    expect(extend?.backdropBlur).toBeUndefined();
    expect(extend?.backdropSaturate).toBeUndefined();
    const core = config.corePlugins as Record<string, boolean>;
    expect(core.backdropBlur).toBe(false);
    expect(core.backdropSaturate).toBe(false);
  });

  it("exposes the eclipse elevation, light-grammar and radius scales", () => {
    const extend = config.theme?.extend;
    expect(extend?.boxShadow).toMatchObject({
      "sg-1": "var(--sg-elev-1)",
      "sg-4": "var(--sg-elev-4)",
      "sg-primary": "var(--sg-shadow-primary)",
      "sg-edge": "var(--sg-edge-top)",
      "sg-edge-strong": "var(--sg-edge-top-strong)",
      "sg-well": "var(--sg-well)",
      "sg-well-soft": "var(--sg-well-soft)",
      "sg-lift": "var(--sg-lift)",
      "sg-scrim": "var(--sg-scrim-down)",
      "sg-bloom-1": "var(--sg-bloom-1)",
      "sg-bloom-3": "var(--sg-bloom-3)",
    });
    expect(extend?.borderRadius).toMatchObject({
      "sg-sm": "10px",
      "sg-xl": "28px",
      "st-bubble": "var(--st-bubble-radius)",
      "st-sheet": "var(--st-sheet-radius)",
      "st-pill": "var(--st-pill-radius)",
    });
  });

  it("routes primary/ring through the tint pipeline, not HSL triplets", () => {
    const colors = config.theme?.extend?.colors as Record<string, unknown>;
    const primary = colors.primary as { DEFAULT: string; foreground: string };
    expect(primary.DEFAULT).toContain("var(--sg-tint)");
    expect(primary.foreground).toBe("var(--sg-tint-ink)");
    expect(String(colors.ring)).toContain("var(--sg-tint)");
    expect(String(colors["sg-tint"])).toContain("var(--sg-tint)");
    expect(String(colors["sg-border-ghost"])).toBe("var(--sg-border-ghost)");
  });

  it("caps font weights at 500 — hierarchy comes from the ink scale", () => {
    const weights = config.theme?.extend?.fontWeight as Record<string, string>;
    expect(weights.semibold).toBe("500");
    expect(weights.bold).toBe("500");
  });

  it("ships the eclipse type stacks (MiSans body / M PLUS 1 display / JetBrains mono)", () => {
    const fonts = config.theme?.extend?.fontFamily as Record<string, string[]>;
    expect(fonts.sans[0]).toBe("var(--font-misans)");
    expect(fonts.display[0]).toBe("var(--font-mplus)");
    expect(fonts.mono[0]).toBe("var(--font-jetbrains-mono)");
  });
});
