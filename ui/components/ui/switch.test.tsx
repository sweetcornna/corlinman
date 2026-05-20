import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Switch } from "./switch";

describe("Switch", () => {
  it("uses theme-aware glass surfaces instead of a hard-coded white thumb", () => {
    render(
      <Switch
        checked={false}
        onCheckedChange={vi.fn()}
        aria-label="Demo switch"
      />,
    );

    const control = screen.getByRole("switch", { name: "Demo switch" });
    expect(control.className).toContain("backdrop-blur-glass");
    expect(control.className).toContain("bg-tp-glass-inner");

    const thumb = control.querySelector("span");
    expect(thumb?.className).toContain("var(--tp-ink)_18%");
    expect(thumb?.className).not.toContain("bg-white");
    expect(thumb?.className).not.toContain("bg-background");
  });
});
