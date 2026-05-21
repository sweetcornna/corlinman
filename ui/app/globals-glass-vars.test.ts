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

describe("Tidepool light glass fallback", () => {
  it("keeps light glass visible before backdrop-filter finishes compositing", () => {
    expect(alphaOfOklch(lightVar("--tp-glass"))).toBeGreaterThanOrEqual(0.08);
    expect(alphaOfOklch(lightVar("--tp-glass-3"))).toBeGreaterThanOrEqual(0.06);
  });

  it("hints glass surfaces for early backdrop-filter compositing", () => {
    const glassRuleMatch = css.match(
      /\.bg-tp-glass,[\s\S]*?\.bg-popover,[\s\S]*?\.bg-panel\s*\{([\s\S]*?)\n\s*\}/,
    );
    expect(glassRuleMatch?.[1]).toContain("will-change: backdrop-filter");
  });
});
