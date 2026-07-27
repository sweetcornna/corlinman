import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { InfoTip, Tooltip } from "./tooltip";

describe("Tooltip", () => {
  it("stays hidden until hover, then shows an opaque matte bubble (no backdrop blur)", () => {
    render(
      <Tooltip content="server regex ^[a-z]+$" data-testid="tip">
        <button type="button">trigger</button>
      </Tooltip>,
    );

    expect(screen.queryByRole("tooltip")).toBeNull();

    fireEvent.mouseEnter(screen.getByTestId("tip"));
    const bubble = screen.getByRole("tooltip");
    expect(bubble.textContent).toContain("^[a-z]+$");
    // Eclipse: opaque surface, never a blur.
    expect(bubble.className).toContain("bg-sg-opaque");
    expect(bubble.className).not.toContain("backdrop-");

    fireEvent.mouseLeave(screen.getByTestId("tip"));
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("shows on keyboard focus and hides on Escape", () => {
    render(
      <Tooltip content="long contract" data-testid="tip">
        <button type="button">trigger</button>
      </Tooltip>,
    );

    fireEvent.focus(screen.getByRole("button", { name: "trigger" }));
    expect(screen.getByRole("tooltip")).toBeTruthy();
    // aria-describedby links trigger wrapper to the bubble while visible.
    const wrapper = screen.getByTestId("tip");
    expect(wrapper.getAttribute("aria-describedby")).toBe(
      screen.getByRole("tooltip").id,
    );

    fireEvent.keyDown(wrapper, { key: "Escape" });
    expect(screen.queryByRole("tooltip")).toBeNull();
  });
});

describe("InfoTip", () => {
  it("renders a sprite-drawn focusable glyph carrying the tooltip", () => {
    render(<InfoTip content="detail body" label="格式详情" />);

    const trigger = screen.getByRole("button", { name: "格式详情" });
    // Self-drawn sprite icon (svg <use>), zero third-party icon imports.
    expect(trigger.querySelector("svg use")).toBeTruthy();

    fireEvent.focus(trigger);
    expect(screen.getByRole("tooltip").textContent).toBe("detail body");
  });
});
