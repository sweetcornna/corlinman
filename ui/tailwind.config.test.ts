import { describe, expect, it } from "vitest";
import config from "./tailwind.config";

describe("Spatial Glass backdrop tokens", () => {
  it("enforces the blur budget: legacy content tier flat, overlay/shell tiers blurred", () => {
    const extend = config.theme?.extend;

    // `glass-strong` consumers are all overlays (dialogs/drawers/palettes);
    // the legacy 0px `glass` tier is gone and must stay gone.
    expect(extend?.backdropBlur).toMatchObject({
      "glass-strong": "28px",
      "sg-shell": "20px",
      "sg-overlay": "28px",
    });
    expect(extend?.backdropBlur).not.toHaveProperty("glass");
    expect(extend?.backdropSaturate).toMatchObject({
      "glass-strong": "1.5",
      "sg-shell": "1.4",
      "sg-overlay": "1.5",
    });
  });

  it("exposes the sg elevation and radius scales", () => {
    const extend = config.theme?.extend;
    expect(extend?.boxShadow).toMatchObject({
      "sg-1": "var(--sg-elev-1)",
      "sg-4": "var(--sg-elev-4)",
      "sg-primary": "var(--sg-shadow-primary)",
    });
    expect(extend?.borderRadius).toMatchObject({
      "sg-sm": "10px",
      "sg-xl": "28px",
    });
  });
});
