import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Switch } from "./switch";

describe("Switch", () => {
  it("uses theme-aware spatial-glass surfaces and never a backdrop blur", () => {
    render(
      <Switch
        checked={false}
        onCheckedChange={vi.fn()}
        aria-label="Demo switch"
      />,
    );

    const control = screen.getByRole("switch", { name: "Demo switch" });
    expect(control.className).not.toContain("backdrop-");
    expect(control.className).toContain("bg-sg-inset");

    const thumb = control.querySelector("span");
    expect(thumb?.className).not.toContain("backdrop-");
  });
});
