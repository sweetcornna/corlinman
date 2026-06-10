/**
 * Smoke tests for the global 404 page.
 *
 * Verifies:
 *   1. Renders the "404" headline + "Back to dashboard" link.
 *   2. Renders 15 drifting dots (matches DOTS.length in `not-found.tsx`).
 *   3. Reduced-motion path still renders the dots (they're CSS-animated,
 *      so the gate lives in `@media (prefers-reduced-motion: reduce)` —
 *      no JS branch is needed; we assert the DOM is stable either way).
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import NotFound from "./not-found";

function mockMatchMedia(reduce: boolean) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: query === "(prefers-reduced-motion: reduce)" ? reduce : false,
    media: query,
    onchange: null,
    addEventListener: () => void 0,
    removeEventListener: () => void 0,
    addListener: () => void 0,
    removeListener: () => void 0,
    dispatchEvent: () => false,
  })) as typeof window.matchMedia;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("NotFound", () => {
  it("renders 404 headline and home link", () => {
    mockMatchMedia(false);
    render(<NotFound />);
    // Headline + eyebrow both say "404"; at least one must be present.
    expect(screen.getAllByText("404").length).toBeGreaterThanOrEqual(1);
    // Test locale is forced to zh-CN (see vitest.setup.ts), so assert the
    // Chinese link label rather than the English one.
    expect(
      screen.getByRole("link", { name: "返回控制台" }),
    ).toHaveAttribute("href", "/");
  });

  it("renders 15 drifting dots", () => {
    mockMatchMedia(false);
    render(<NotFound />);
    const field = screen.getByTestId("not-found-dots");
    expect(field.querySelectorAll(".nf-dot").length).toBe(15);
  });

  it("renders the same dot DOM under reduced motion (static via @media CSS)", () => {
    mockMatchMedia(true);
    render(<NotFound />);
    const field = screen.getByTestId("not-found-dots");
    // The reduced-motion branch is CSS-only — the dot elements must still
    // exist; only their `animation` is stripped by the @media rule.
    expect(field.querySelectorAll(".nf-dot").length).toBe(15);
  });
});
