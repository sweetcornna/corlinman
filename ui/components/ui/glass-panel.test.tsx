import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { GlassPanel } from "./glass-panel";

afterEach(() => {
  cleanup();
});

describe("GlassPanel", () => {
  it("defaults to the soft variant", () => {
    render(<GlassPanel data-testid="panel">content</GlassPanel>);
    const el = screen.getByTestId("panel");
    expect(el).toHaveAttribute("data-glass-variant", "soft");
    expect(el).toHaveClass("bg-sg-card-grad");
    expect(el).not.toHaveClass("backdrop-blur-md");
  });

  it("renders subtle variant without backdrop blur", () => {
    render(
      <GlassPanel variant="subtle" data-testid="panel">
        content
      </GlassPanel>,
    );
    const el = screen.getByTestId("panel");
    expect(el).toHaveAttribute("data-glass-variant", "subtle");
    expect(el).toHaveClass("bg-sg-card-grad");
    expect(el).not.toHaveClass("backdrop-blur-md");
  });

  it("renders the primary ring/glow when variant=primary", () => {
    render(
      <GlassPanel variant="primary" data-testid="panel">
        x
      </GlassPanel>,
    );
    expect(screen.getByTestId("panel")).toHaveClass("shadow-sg-primary");
  });

  it("can render as a different element", () => {
    render(
      <GlassPanel as="section" data-testid="panel">
        x
      </GlassPanel>,
    );
    expect(screen.getByTestId("panel").tagName).toBe("SECTION");
  });

  it("mounts a top inset highlight layer", () => {
    const { container } = render(<GlassPanel>x</GlassPanel>);
    const hl = container.querySelector(".bg-sg-highlight");
    expect(hl).not.toBeNull();
    expect(hl).toHaveAttribute("aria-hidden", "true");
  });
});
