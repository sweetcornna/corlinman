import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { AuroraBackground } from "./aurora-background";

afterEach(() => {
  cleanup();
});

describe("AuroraBackground", () => {
  it("renders as a fixed full-viewport layer by default", () => {
    render(<AuroraBackground data-testid="bg" />);
    const el = screen.getByTestId("bg");
    expect(el).toHaveAttribute("aria-hidden", "true");
    expect(el.className).toContain("fixed");
    expect(el.className).toContain("-z-10");
    expect(el.className).toContain("tp-bg-root");
  });

  it("can be rendered inline when fixed=false", () => {
    render(<AuroraBackground fixed={false} data-testid="bg" />);
    const el = screen.getByTestId("bg");
    expect(el.className).not.toContain("fixed");
    expect(el.className).not.toContain("-z-10");
  });
});
