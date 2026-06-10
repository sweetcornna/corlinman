import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const css = readFileSync(join(__dirname, "globals.css"), "utf8");

function blockOf(selector: RegExp): string {
  const match = css.match(selector);
  expect(match).not.toBeNull();
  return match?.[1] ?? "";
}

const rootBlock = blockOf(/:root\s*\{([\s\S]*?)\n\s*\}/);
const darkBlock = blockOf(/\.dark\s*\{([\s\S]*?)\n\s*\}/);

function varIn(block: string, name: string): string {
  const match = block.match(new RegExp(`${name}:\\s*([^;]+);`));
  expect(match, `${name} should be defined`).not.toBeNull();
  return match?.[1].trim() ?? "";
}

describe("Spatial Glass invariants", () => {
  it("defines the sg glass tiers in both themes", () => {
    for (const block of [rootBlock, darkBlock]) {
      varIn(block, "--sg-glass-1-bg");
      varIn(block, "--sg-glass-2-bg");
      varIn(block, "--sg-glass-3-bg");
      varIn(block, "--sg-inset-bg");
      varIn(block, "--sg-glass-opaque");
    }
  });

  it("aliases every legacy tp glass token into the sg namespace", () => {
    expect(varIn(rootBlock, "--tp-glass")).toBe("var(--sg-glass-2-bg)");
    expect(varIn(rootBlock, "--tp-glass-edge")).toBe("var(--sg-border)");
    expect(varIn(rootBlock, "--tp-glass-inner")).toBe("var(--sg-inset-bg)");
    expect(varIn(rootBlock, "--tp-amber")).toBe("var(--sg-accent)");
    expect(varIn(rootBlock, "--tp-shadow-panel")).toBe("var(--sg-elev-2)");
    // Aliases live only in :root — lazy var() resolution carries the
    // .dark sg overrides through; a duplicate dark alias block would rot.
    expect(darkBlock).not.toContain("--tp-glass:");
    expect(darkBlock).not.toContain("--tp-amber:");
  });

  it("blurs shell and overlay tiers but never the content-card tier", () => {
    const shell = blockOf(/\.sg-glass-shell\s*\{([\s\S]*?)\}/);
    expect(shell).toContain("backdrop-filter: blur(");
    expect(shell).toContain("-webkit-backdrop-filter: blur(");

    const overlay = blockOf(/\.sg-glass-overlay\s*\{([\s\S]*?)\}/);
    expect(overlay).toContain("backdrop-filter: blur(");

    const card = blockOf(/\.sg-card\s*\{([\s\S]*?)\}/);
    expect(card).not.toContain("backdrop-filter");
    expect(card).toContain("background-image: linear-gradient(");

    const emboss = blockOf(/\n\s*\.emboss\s*\{([\s\S]*?)\}/);
    expect(emboss).not.toContain("backdrop-filter");
  });

  it("provides an opaque fallback when backdrop-filter is unsupported", () => {
    const supports = blockOf(
      /@supports not \(backdrop-filter: blur\(1px\)\)\s*\{([\s\S]*?)\n\s*\}/,
    );
    expect(supports).toContain(".sg-glass-shell");
    expect(supports).toContain(".sg-glass-overlay");
    expect(supports).toContain("var(--sg-glass-opaque)");
  });

  it("paints the backdrop as pure CSS gradient — no texture JPGs anywhere", () => {
    expect(css).not.toContain('url("/bg/');
    const html = blockOf(/\n\s*html\s*\{([\s\S]*?)\}/);
    expect(html).toContain("background-image: linear-gradient(");
    expect(html).toContain("background-attachment: fixed");
  });

  it("keeps compact inner utilities pinned to the inset fill", () => {
    const innerRule = css.match(
      /\.bg-tp-glass-inner\\\/40,[\s\S]*?\.bg-tp-glass-inner\\\/70\s*\{([\s\S]*?)\}/,
    );
    expect(innerRule).not.toBeNull();
    expect(innerRule?.[1]).toContain("var(--sg-inset-bg)");
  });
});
