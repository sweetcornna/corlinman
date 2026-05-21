import { describe, expect, it } from "vitest";
import config from "./tailwind.config";

describe("Tidepool backdrop tokens", () => {
  it("keeps legacy glass utilities visually flat", () => {
    const extend = config.theme?.extend;

    expect(extend?.backdropBlur).toMatchObject({
      glass: "0px",
      "glass-strong": "0px",
    });
    expect(extend?.backdropSaturate).toMatchObject({
      glass: "1",
      "glass-strong": "1",
    });
  });
});
