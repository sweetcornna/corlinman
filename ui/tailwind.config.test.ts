import { describe, expect, it } from "vitest";
import config from "./tailwind.config";

describe("Tidepool glass blur tokens", () => {
  it("keeps soft and strong glass blur consistent and light", () => {
    const extend = config.theme?.extend;

    expect(extend?.backdropBlur).toMatchObject({
      glass: "6px",
      "glass-strong": "6px",
    });
    expect(extend?.backdropSaturate).toMatchObject({
      glass: "1.12",
      "glass-strong": "1.12",
    });
  });
});
