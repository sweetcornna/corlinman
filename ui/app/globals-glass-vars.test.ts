import { execSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const UI_ROOT = join(__dirname, "..");
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

function filesMatching(pattern: string): string[] {
  try {
    const out = execSync(
      `grep -rlE '${pattern}' app components lib --include='*.tsx' --include='*.ts' | grep -v '\\.test\\.' | sort`,
      { cwd: UI_ROOT, encoding: "utf8" },
    );
    return out.split("\n").filter(Boolean);
  } catch {
    return []; // grep exits 1 on zero matches
  }
}

describe("Eclipse token grammar", () => {
  it("defines the full token family in both themes", () => {
    for (const block of [rootBlock, darkBlock]) {
      // matte surfaces
      varIn(block, "--sg-glass-1-bg");
      varIn(block, "--sg-glass-2-bg");
      varIn(block, "--sg-glass-3-bg");
      varIn(block, "--sg-glass-opaque");
      varIn(block, "--sg-inset-bg");
      // borders incl. the ghost tier
      varIn(block, "--sg-border");
      varIn(block, "--sg-border-strong");
      varIn(block, "--sg-border-ghost");
      // ink scale
      for (const ink of ["--sg-ink", "--sg-ink-2", "--sg-ink-3", "--sg-ink-4", "--sg-ink-5"]) {
        varIn(block, ink);
      }
      // tint pipeline
      varIn(block, "--sg-tint");
      varIn(block, "--sg-tint-ink");
      varIn(block, "--sg-tint-glow");
      varIn(block, "--sg-tint-soft");
      // light grammar
      varIn(block, "--sg-edge-top");
      varIn(block, "--sg-edge-top-strong");
      varIn(block, "--sg-well");
      varIn(block, "--sg-well-soft");
      for (const elev of ["--sg-elev-1", "--sg-elev-2", "--sg-elev-3", "--sg-elev-4"]) {
        varIn(block, elev);
      }
      varIn(block, "--sg-lift");
      varIn(block, "--sg-scrim-down");
      varIn(block, "--sg-bloom-1");
      varIn(block, "--sg-bloom-2");
      varIn(block, "--sg-bloom-3");
      // canvas
      varIn(block, "--sg-moonrise");
      varIn(block, "--sg-vignette");
      // status + misc
      varIn(block, "--sg-ok");
      varIn(block, "--sg-warn");
      varIn(block, "--sg-err");
      varIn(block, "--sg-row-alt");
      varIn(block, "--sg-grad-text");
      varIn(block, "--sg-card-sheen");
    }
  });

  it("aliases the legacy accent family onto the tint pipeline", () => {
    for (const block of [rootBlock, darkBlock]) {
      expect(varIn(block, "--sg-accent")).toBe("var(--sg-tint)");
      expect(varIn(block, "--sg-accent-soft")).toBe("var(--sg-tint-soft)");
      expect(varIn(block, "--sg-accent-glow")).toBe("var(--sg-tint-glow)");
    }
  });

  it("keeps the skeleton un-tinted: status colors never reference tint", () => {
    for (const block of [rootBlock, darkBlock]) {
      for (const name of ["--sg-ok", "--sg-warn", "--sg-err", "--sg-border", "--sg-ink"]) {
        expect(varIn(block, name)).not.toContain("--sg-tint");
      }
    }
  });

  it("defines every tint preset for both themes", () => {
    for (const preset of ["dawn", "ice", "rose", "moss", "iris"]) {
      expect(css).toMatch(new RegExp(`\\.dark\\[data-tint="${preset}"\\]`));
      expect(css).toMatch(new RegExp(`:root\\[data-tint="${preset}"\\]:not\\(\\.dark\\)`));
    }
  });

  it("retires the nebula layer — transparent aliases only", () => {
    for (const block of [rootBlock, darkBlock]) {
      expect(varIn(block, "--sg-nebula-1")).toBe("transparent");
      expect(varIn(block, "--sg-nebula-2")).toBe("transparent");
      expect(varIn(block, "--sg-nebula-3")).toBe("transparent");
    }
  });

  it("contains no legacy Tidepool tp-* tokens or classes", () => {
    expect(css).not.toMatch(/--tp-/);
    expect(css).not.toMatch(/\.tp-/);
    expect(css).not.toMatch(/\b(emboss|pattern-active|relief-text|ridge-divider|dot-grid)\b/);
  });

  it("contains no liquid-glass remnants", () => {
    expect(css).not.toMatch(/\.lg-/);
    expect(css).not.toMatch(/--sg-noise/);
    expect(css).not.toMatch(/sg-aurora/);
    expect(css).not.toMatch(/--sg-grad-border/);
  });

  it("paints the canvas on <html>: moonrise + vignette, fixed, no texture files", () => {
    expect(css).not.toContain('url("/bg/');
    const html = blockOf(/\n\s*html\s*\{([\s\S]*?)\}/);
    expect(html).toContain("var(--sg-vignette)");
    expect(html).toContain("var(--sg-moonrise)");
    expect(html).toContain("background-attachment: fixed");
  });
});

describe("zero backdrop-filter (repo-wide static check)", () => {
  it("globals.css declares no backdrop-filter", () => {
    expect(css).not.toMatch(/backdrop-filter\s*:/);
  });

  it("no source file uses backdrop blur/saturate classes", () => {
    // Matches class usage (backdrop-blur-*, backdrop-saturate-*) but not
    // prose comments that merely mention the banned property.
    expect(filesMatching("backdrop-(blur|saturate)-")).toEqual([]);
  });

  it("no source file uses liquid-glass optic classes", () => {
    expect(filesMatching("lg-(gel|edge|refract|specular|sheen|stars|hue-drift|float)")).toEqual([]);
  });

  it("no source file imports lucide-react — icons come from the sprite", () => {
    expect(filesMatching('from "lucide-react"')).toEqual([]);
  });
});

describe("glow and gradient whitelists", () => {
  it("gradient display text appears only in the login/onboard hero", () => {
    const users = filesMatching("sg-grad-text");
    const allowed = ["app/login/page.tsx", "app/onboard/page.tsx"];
    const offList = users.filter((f) => !allowed.includes(f));
    expect(offList, `sg-grad-text outside the hero whitelist: ${offList.join(", ")}`).toEqual([]);
  });

  it("bloom shadows stay inside the whitelist", () => {
    // Glow is whitelist-only: eclipse orb, streaming thread, live dots,
    // caret, solid tint buttons, progress bars, selected states. The c-*
    // component classes carry bloom internally; direct tsx usage of the
    // bloom utilities must stay on this list.
    const BLOOM_FILES = [
      "components/ui/button.tsx",
      "components/ui/live-dot.tsx",
      "components/ui/presence-orb.tsx",
      "components/ui/stream-pill.tsx",
    ];
    const users = filesMatching("shadow-sg-bloom");
    const offList = users.filter((f) => !BLOOM_FILES.includes(f));
    expect(offList, `bloom outside the whitelist: ${offList.join(", ")}`).toEqual([]);
  });

  it("caps font weights at 500 — no extrabold/black/arbitrary heavy weights", () => {
    expect(filesMatching("font-(extrabold|black)\\b|font-\\[[6-9]00\\]")).toEqual([]);
  });
});

describe("sticky chrome opacity", () => {
  it(".c-appbar is opaque canvas, never transparent", () => {
    // Regression: `.c-appbar { background: transparent }` lands after the
    // utility layer, silently beating the topbar's `bg-sg-space-0` — with
    // no backdrop-filter in this design language, a see-through sticky bar
    // lets scrolled content bleed through (illegible on the Paper theme).
    const appbar = blockOf(/\.c-appbar\s*\{([\s\S]*?)\}/);
    expect(appbar).toContain("background: var(--sg-space-0)");
    expect(appbar).not.toMatch(/background:\s*transparent/);
  });
});
