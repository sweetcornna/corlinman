/**
 * PageTransition smoke tests.
 *
 * The component is mostly a framer-motion wrapper, so the tests focus on the
 * contract we expose to Batches 2-5:
 *   1. Children actually mount.
 *   2. Baseline route wrapper is static: no opacity fade and no transform.
 *   3. A custom `variants` prop overrides the baseline.
 *   4. `prefers-reduced-motion: reduce` collapses translate/duration.
 *
 * framer-motion reads `prefers-reduced-motion` via `window.matchMedia`, which
 * jsdom stubs as `undefined` — each reduced-motion test installs its own
 * matchMedia mock.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("next/navigation", () => ({
  usePathname: () => "/test-route",
}));

import {
  PageTransition,
  baselinePageVariants,
  type PageTransitionVariants,
} from "./page-transition";

function installMatchMedia(matches: boolean) {
  const mm = vi.fn().mockImplementation((query: string) => ({
    matches,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: mm,
  });
}

describe("PageTransition", () => {
  beforeEach(() => {
    installMatchMedia(false);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders children", () => {
    render(
      <PageTransition>
        <p data-testid="child">hello</p>
      </PageTransition>,
    );
    expect(screen.getByTestId("child")).toHaveTextContent("hello");
  });

  it("exposes a static glass-safe baseline variant", () => {
    expect(baselinePageVariants.initial).toMatchObject({ opacity: 1, y: 0 });
    expect(baselinePageVariants.animate).toMatchObject({ opacity: 1, y: 0 });
    expect(baselinePageVariants.exit).toMatchObject({ opacity: 1, y: 0 });
    expect(baselinePageVariants.transition).toMatchObject({ duration: 0 });
  });

  it("applies the baseline animation to the motion wrapper", () => {
    const { container } = render(
      <PageTransition>
        <p>baseline</p>
      </PageTransition>,
    );
    // The motion.div is the outermost element inside the LayoutGroup /
    // AnimatePresence wrappers; framer-motion writes the `animate` target
    // into the inline style once mounted.
    const motionDiv = container.querySelector<HTMLDivElement>(
      "[data-testid='page-transition']",
    );
    expect(motionDiv).not.toBeNull();
    // The route wrapper must not move during page swaps; even a small y
    // transform is visible as a twitch against the fixed relief background.
    expect(motionDiv?.style.opacity).toBe("1");
    expect(motionDiv?.style.transform ?? "").not.toMatch(/matrix|translate/i);
  });

  it("honors a custom variants override", () => {
    const custom: PageTransitionVariants = {
      initial: { opacity: 0.3, x: -20 },
      animate: { opacity: 0.75, x: 0 },
      exit: { opacity: 0, x: 20 },
      transition: { duration: 0 },
    };
    const { container } = render(
      <PageTransition variants={custom}>
        <p>custom</p>
      </PageTransition>,
    );
    const motionDiv = container.querySelector<HTMLDivElement>(
      "[data-testid='page-transition']",
    );
    expect(motionDiv).not.toBeNull();
    // The override's `animate` opacity (0.75) wins over the baseline's 1.
    expect(motionDiv?.style.opacity).toBe("0.75");
  });

  it("collapses to final state when prefers-reduced-motion is set", () => {
    installMatchMedia(true);
    const { container } = render(
      <PageTransition
        variants={{
          initial: { opacity: 0, y: 100 },
          animate: { opacity: 1, y: 0 },
          exit: { opacity: 0, y: -100 },
          transition: { duration: 10 },
        }}
      >
        <p>reduced</p>
      </PageTransition>,
    );
    const motionDiv = container.querySelector<HTMLDivElement>(
      "[data-testid='page-transition']",
    );
    expect(motionDiv).not.toBeNull();
    // Reduced-motion branch forces opacity=1 immediately and never writes a
    // y-translate — so the element's transform should not contain the 100px
    // override value from the custom variants.
    expect(motionDiv?.style.opacity).toBe("1");
    expect(motionDiv?.style.transform ?? "").not.toMatch(/100px/);
  });
});
