"use client";

import * as React from "react";
import { usePathname } from "next/navigation";
import {
  AnimatePresence,
  LayoutGroup,
  motion,
  useReducedMotion,
  type Transition,
  type TargetAndTransition,
} from "framer-motion";

/**
 * Variant contract consumed by {@link PageTransition}. Each state (`initial`,
 * `animate`, `exit`) is a plain framer-motion target. Kept intentionally
 * narrow so batches that add shared-layout morphs still feed through the same
 * pipeline without a second branching API.
 */
export interface PageTransitionVariants {
  initial: TargetAndTransition;
  animate: TargetAndTransition;
  exit: TargetAndTransition;
  transition?: Transition;
}

/**
 * Static route wrapper: no movement at all. Kept as an explicit opt-out for
 * pages that must not animate on navigation (e.g. ones owning their own
 * shared-layout morphs). Pass it via the `variants` prop. The shell default is
 * now {@link depthPageVariants}.
 */
export const baselinePageVariants: PageTransitionVariants = {
  initial: { opacity: 1, y: 0 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 1, y: 0 },
  transition: { duration: 0 },
};

/**
 * Spatial Glass default — a short "depth" transition. Pages rise from slightly
 * behind and below (scale 0.985, y 8) into place, and exit by drifting
 * marginally forward (scale 1.005). The asymmetric timing (280ms in / 140ms
 * out) keeps navigation feeling responsive: the leaving page clears fast, the
 * arriving page settles with a soft visionOS-style ease. Lands on a clean
 * identity transform (scale 1, y 0) so glass surfaces never sit mid-blur.
 */
export const depthPageVariants: PageTransitionVariants = {
  initial: { opacity: 0, scale: 0.985, y: 8 },
  animate: {
    opacity: 1,
    scale: 1,
    y: 0,
    transition: { duration: 0.28, ease: [0.32, 0.72, 0, 1] },
  },
  exit: {
    opacity: 0,
    scale: 1.005,
    transition: { duration: 0.14, ease: [0.32, 0.72, 0, 1] },
  },
};

/**
 * Reduced-motion snapshot: no movement, no duration — the element lands at
 * its final state immediately. `AnimatePresence` still sees the unmount so
 * `mode="wait"` sequencing is preserved for shared-layout morphs.
 */
const reducedMotionVariants: PageTransitionVariants = {
  initial: { opacity: 1 },
  animate: { opacity: 1 },
  exit: { opacity: 1 },
  transition: { duration: 0 },
};

/**
 * Route-change page transition. Wraps children in a framer-motion
 * `<LayoutGroup>` so sibling pages can share `layoutId` values and morph
 * across navigations; `<AnimatePresence mode="wait">` lives inside the group
 * so the exiting page finishes before the next enters.
 *
 * - `variants` (optional): per-route override. Falls back to
 *   {@link depthPageVariants} (the Spatial Glass default) when absent.
 * - Reduced motion (`prefers-reduced-motion: reduce`) snaps to the final
 *   state with no translate/duration.
 * - Children are keyed on pathname so they re-mount on navigation.
 */
export function PageTransition({
  children,
  variants,
}: {
  children: React.ReactNode;
  variants?: PageTransitionVariants;
}) {
  const pathname = usePathname();
  const prefersReducedMotion = useReducedMotion();

  const active: PageTransitionVariants = prefersReducedMotion
    ? reducedMotionVariants
    : (variants ?? depthPageVariants);

  return (
    <LayoutGroup>
      <AnimatePresence mode="wait" initial={false}>
        <motion.div
          key={pathname}
          initial={active.initial}
          animate={active.animate}
          exit={active.exit}
          transition={active.transition}
          className="flex flex-1 flex-col"
          data-testid="page-transition"
        >
          {children}
        </motion.div>
      </AnimatePresence>
    </LayoutGroup>
  );
}
