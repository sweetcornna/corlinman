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

describe("Spatial Glass invariants", () => {
  it("defines the sg glass tiers in both themes", () => {
    for (const block of [rootBlock, darkBlock]) {
      varIn(block, "--sg-glass-1-bg");
      varIn(block, "--sg-glass-2-bg");
      varIn(block, "--sg-glass-3-bg");
      varIn(block, "--sg-inset-bg");
      varIn(block, "--sg-glass-opaque");
      varIn(block, "--sg-accent");
      varIn(block, "--sg-row-alt");
    }
  });

  it("contains no legacy Tidepool tp-* tokens or classes", () => {
    expect(css).not.toMatch(/--tp-/);
    expect(css).not.toMatch(/\.tp-/);
    expect(css).not.toMatch(/\b(emboss|pattern-active|relief-text|ridge-divider|dot-grid)\b/);
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
});

describe("blur budget (repo-wide static check)", () => {
  // The real-blur tiers may only appear on shell surfaces and floating
  // overlays. Content cards/rows must stay faux-glass so scrolling lists
  // and SSE feeds never trigger re-blurs. If you add a file here, it must
  // be a floating overlay (dialog/drawer/popover/lightbox/scrim) — not a
  // content surface.
  const SHELL_FILES = ["components/layout/sidebar.tsx", "components/layout/nav.tsx"];
  const OVERLAY_FILES = [
    ...SHELL_FILES,
    "app/(admin)/layout.tsx", // mobile drawer scrim
    "app/login/page.tsx", // public overlay card
    "app/status/[token]/status-client.tsx", // showcase hero (spec-exempted)
    "components/approvals/ArgsDialog.tsx",
    "components/approvals/DenyReasonDialog.tsx",
    "components/chat/attachment-gallery.tsx", // lightbox
    "components/chat/composer-mention-menu.tsx",
    "components/chat/composer-slash-menu.tsx",
    "components/chat/conversation-search.tsx",
    "components/chat/emoji-picker.tsx",
    "components/chat/markdown-message.tsx", // lightbox
    "components/cmdk-palette.tsx",
    "components/ui/accent-picker.tsx", // theme-color popover

    "components/layout/profile-switcher.tsx", // popover
    "components/playground/agent-picker.tsx", // popover
    "components/providers.tsx", // sonner toasts
    "components/sessions/replay-dialog.tsx",
    "components/ui/command-palette.tsx",
    "components/ui/dialog.tsx",
    "components/ui/drawer.tsx",
  ];

  function filesMatching(pattern: string): string[] {
    try {
      const out = execSync(
        `grep -rlE '${pattern}' app components --include='*.tsx' | grep -v '\\.test\\.' | sort`,
        { cwd: UI_ROOT, encoding: "utf8" },
      );
      return out.split("\n").filter(Boolean);
    } catch {
      return []; // grep exits 1 on zero matches
    }
  }

  it("keeps real blur inside the shell/overlay whitelist", () => {
    const realBlurUsers = filesMatching(
      "backdrop-blur-(sg-shell|sg-overlay|glass-strong|sm|md|lg|xl|2xl|3xl)|sg-glass-(shell|overlay)",
    );
    const offBudget = realBlurUsers.filter((f) => !OVERLAY_FILES.includes(f));
    expect(offBudget, `content-tier files using real blur: ${offBudget.join(", ")}`).toEqual([]);
  });

  it("keeps the sg-shell tier exclusive to sidebar and topnav", () => {
    const shellUsers = filesMatching("backdrop-blur-sg-shell|sg-glass-shell");
    expect(shellUsers.sort()).toEqual([...SHELL_FILES].sort());
  });

  it("does not grow the legacy 0px backdrop-blur-glass tier", () => {
    // The 0px legacy tier was fully removed in phase 3b — keep it at zero.
    
    
    const legacy = filesMatching("backdrop-blur-glass[^-]");
    expect(legacy).toEqual([]);
  });
});
