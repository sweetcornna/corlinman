import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const css = readFileSync(join(__dirname, "globals.css"), "utf8");

function lightVar(name: string): string {
  const rootMatch = css.match(/:root\s*\{([\s\S]*?)\n\s*\}/);
  expect(rootMatch).not.toBeNull();
  const match = rootMatch?.[1].match(new RegExp(`${name}:\\s*([^;]+);`));
  expect(match).not.toBeNull();
  return match?.[1].trim() ?? "";
}

function alphaOfOklch(value: string): number {
  const match = value.match(/\/\s*([0-9.]+)\s*\)/);
  expect(match).not.toBeNull();
  return Number(match?.[1] ?? Number.NaN);
}

describe("Tidepool textured surfaces", () => {
  it("keeps light glass visible before texture artwork loads", () => {
    expect(alphaOfOklch(lightVar("--tp-glass"))).toBeGreaterThanOrEqual(0.08);
    expect(alphaOfOklch(lightVar("--tp-glass-3"))).toBeGreaterThanOrEqual(0.06);
  });

  it("uses the requested day/night oil textures for card surfaces", () => {
    expect(lightVar("--tp-card-texture-url")).toBe('url("/bg/oil-sky.jpg?v=1")');

    const darkMatch = css.match(/\.dark\s*\{([\s\S]*?)\n\s*\}/);
    expect(darkMatch).not.toBeNull();
    const texture = darkMatch?.[1].match(/--tp-card-texture-url:\s*([^;]+);/);
    expect(texture?.[1].trim()).toBe('url("/bg/oil-navy.jpg?v=1")');
  });

  it("paints card surfaces with texture artwork and no backdrop blur", () => {
    const surfaceRuleMatch = css.match(
      /\.bg-tp-glass,[\s\S]*?\.bg-popover,[\s\S]*?\.bg-panel\s*\{([\s\S]*?)\n\s*\}/,
    );
    const body = surfaceRuleMatch?.[1] ?? "";
    expect(body).toContain("background-image: var(--tp-card-texture-overlay), var(--tp-card-texture-url)");
    expect(body).toContain("background-size: cover");
    expect(body).toContain("backdrop-filter: none");
    expect(body).toContain("-webkit-backdrop-filter: none");
    expect(body).not.toContain("will-change: backdrop-filter");
  });

  it("keeps compact inner utilities flat instead of blurred", () => {
    const innerRuleMatch = css.match(
      /\.bg-tp-glass-inner,[\s\S]*?\.bg-tp-glass-inner\\\/70\s*\{([\s\S]*?)\n\s*\}/,
    );

    expect(innerRuleMatch).not.toBeNull();
    const selectors = innerRuleMatch?.[0] ?? "";
    const body = innerRuleMatch?.[1] ?? "";
    expect(selectors).toContain(".bg-tp-glass-inner-strong");
    expect(selectors).toContain(".bg-tp-glass-inner-hover");
    expect(selectors).toContain(".bg-tp-glass-inner\\/40");
    expect(body).toContain("backdrop-filter: none");
    expect(body).toContain("-webkit-backdrop-filter: none");
    expect(body).not.toContain("will-change: backdrop-filter");
  });
});
