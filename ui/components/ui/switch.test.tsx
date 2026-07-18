import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Switch } from "./switch";

describe("Switch", () => {
  it("unchecked: sunken well track with an ink thumb, never a backdrop blur", () => {
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
    expect(thumb?.className).toContain("bg-sg-ink");
  });

  it("checked: solid tint track with a tint-ink thumb (contrast holds for any tint hue)", () => {
    render(
      <Switch checked onCheckedChange={vi.fn()} aria-label="Demo switch" />,
    );

    const control = screen.getByRole("switch", { name: "Demo switch" });
    expect(control.className).toContain("bg-sg-tint");
    // The thumb must pair with the tint's derived ink so it stays visible
    // on any preset/custom tint — never a fixed color.
    const thumb = control.querySelector("span");
    expect(thumb?.className).toContain("bg-sg-tint-ink");
    expect(thumb?.className).not.toContain("bg-white");
  });
});
