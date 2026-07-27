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

    const trigger = screen.getByRole("button", { name: "trigger" });
    fireEvent.focus(trigger);
    expect(screen.getByRole("tooltip")).toBeTruthy();
    // aria-describedby sits on the focusable TRIGGER (clone-injected) —
    // wrapper-level describedby is never announced by screen readers.
    expect(trigger.getAttribute("aria-describedby")).toBe(
      screen.getByRole("tooltip").id,
    );

    fireEvent.keyDown(trigger, { key: "Escape" });
    expect(screen.queryByRole("tooltip")).toBeNull();
    // Cleared once hidden so stale ids never dangle.
    expect(trigger.getAttribute("aria-describedby")).toBeNull();
  });

  it("Escape dismisses the hover-opened bubble without focus", () => {
    render(
      <Tooltip content="hover contract" data-testid="tip">
        <button type="button">trigger</button>
      </Tooltip>,
    );

    fireEvent.mouseEnter(screen.getByTestId("tip"));
    expect(screen.getByRole("tooltip")).toBeTruthy();
    // Focus is elsewhere (document.body) — the document-level listener
    // must still close the bubble.
    fireEvent.keyDown(document.body, { key: "Escape" });
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
