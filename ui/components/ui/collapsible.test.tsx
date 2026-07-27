import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Collapsible } from "./collapsible";

describe("Collapsible", () => {
  it("renders a native details fold, closed by default, with a sprite chevron", () => {
    render(
      <Collapsible summary="登录流程详情" data-testid="fold">
        <p>folded body</p>
      </Collapsible>,
    );

    const details = screen.getByTestId("fold") as HTMLDetailsElement;
    expect(details.tagName).toBe("DETAILS");
    expect(details.open).toBe(false);
    // Self-drawn sprite chevron (svg <use>), no third-party icons.
    expect(details.querySelector("summary svg use")).toBeTruthy();
    // Matte inset surface — never a backdrop blur.
    expect(details.className).toContain("bg-sg-inset");
    expect(details.className).not.toContain("backdrop-");
  });

  it("honors defaultOpen and keeps the body in the DOM for a11y/find-in-page", () => {
    render(
      <Collapsible summary="summary" defaultOpen data-testid="fold">
        <p>long contract body</p>
      </Collapsible>,
    );

    const details = screen.getByTestId("fold") as HTMLDetailsElement;
    expect(details.open).toBe(true);
    expect(screen.getByText("long contract body")).toBeTruthy();
  });
});
