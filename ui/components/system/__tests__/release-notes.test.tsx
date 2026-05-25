/**
 * `<ReleaseNotes>` tests (W2.1).
 *
 * Two cases:
 *   1. Renders structured markdown — heading + list + inline code block.
 *   2. Sanitises raw HTML — `<script>alert(1)</script>` MUST be stripped
 *      from the rendered DOM (no `<script>` element, no executable text
 *      surviving as a real script tag).
 *
 * Sanitisation is provided by `rehype-sanitize`'s default schema. We
 * exercise the live module — no mocking of either react-markdown or the
 * sanitize plugin — so the test catches any regression in either
 * dependency.
 */

import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render } from "@testing-library/react";

import { ReleaseNotes } from "../release-notes";

afterEach(() => cleanup());

describe("<ReleaseNotes>", () => {
  it("renders headings, lists, and code blocks", () => {
    const md = [
      "# Hello world",
      "",
      "Some intro paragraph.",
      "",
      "- first bullet",
      "- second bullet",
      "",
      "`inline-code`",
      "",
      "```",
      "block code",
      "```",
    ].join("\n");

    const { container, getByText } = render(<ReleaseNotes markdown={md} />);

    // Heading renders as h1 (our component override).
    const h1 = container.querySelector("h1");
    expect(h1).not.toBeNull();
    expect(h1?.textContent).toBe("Hello world");

    // The unordered list and items are emitted.
    const items = container.querySelectorAll("ul > li");
    expect(items.length).toBe(2);
    expect(items[0].textContent).toBe("first bullet");
    expect(items[1].textContent).toBe("second bullet");

    // Inline code wraps in <code>; fenced block wraps in <pre><code>.
    expect(getByText("inline-code").tagName.toLowerCase()).toBe("code");
    const pre = container.querySelector("pre");
    expect(pre).not.toBeNull();
    expect(pre?.textContent).toContain("block code");
  });

  it("strips raw HTML — no <script> survives sanitisation", () => {
    const malicious = [
      "# Safe heading",
      "",
      "<script>alert(1)</script>",
      "",
      "[evil](javascript:alert(2))",
      "",
      "<img src=x onerror=alert(3)>",
    ].join("\n");

    const { container } = render(<ReleaseNotes markdown={malicious} />);

    // The default rehype-sanitize schema removes <script> entirely.
    expect(container.querySelector("script")).toBeNull();

    // No element should carry an inline `onerror` (or any on*) handler.
    const allElements = container.querySelectorAll("*");
    for (const el of allElements) {
      for (const attr of el.attributes) {
        // Allow data-* / aria-* / standard whitelisted attrs but never
        // inline event handlers.
        expect(attr.name.startsWith("on")).toBe(false);
      }
    }

    // `javascript:` href attributes are also dropped by the default schema.
    const evilLink = container.querySelector('a[href^="javascript:"]');
    expect(evilLink).toBeNull();

    // The safe heading survives so we know the renderer ran.
    const h1 = container.querySelector("h1");
    expect(h1?.textContent).toBe("Safe heading");
  });
});
