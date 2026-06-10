/**
 * Motion tokens for framer-motion (B1-FE2).
 *
 * All variants are static, serializable objects. Use `useMotionVariants()` from
 * within a React component to receive instant-transition versions when the user
 * has enabled `prefers-reduced-motion`.
 */
import { useReducedMotion, type Variants, type Transition } from "framer-motion";

/** Fade + slide up. Used for panel/section mounts. */
export const fadeUp: Variants = {
  hidden: { opacity: 0, y: 8 },
  visible: {
    opacity: 1,
    y: 0,
    transition: { duration: 0.28, ease: [0.22, 1, 0.36, 1] },
  },
};

/** Parent orchestrator for staggered children (lists, grids). */
export const stagger: Variants = {
  hidden: {},
  visible: {
    transition: { staggerChildren: 0.06, delayChildren: 0.04 },
  },
};

/** Pop-in with subtle overshoot. Good for toasts, dialog content, badges. */
export const springPop: Variants = {
  hidden: { opacity: 0, scale: 0.96 },
  visible: {
    opacity: 1,
    scale: 1,
    transition: { type: "spring", stiffness: 420, damping: 26, mass: 0.7 },
  },
};

/** List-item default — pair with `stagger` on the parent. */
export const listItem: Variants = {
  hidden: { opacity: 0, y: 8 },
  visible: {
    opacity: 1,
    y: 0,
    transition: { duration: 0.24, ease: "easeOut" },
  },
};

/** Spread onto a `<motion.*>` for shared-layout card transitions. */
export const sharedCard = {
  layout: true as const,
  transition: { type: "spring", stiffness: 380, damping: 30 } as Transition,
};

// ────────────────────────────────────────────────────────────────
// Tidepool — Phase 0 additions.
// Continuous animations (breathing dots, drawing underlines, just-now
// fades, badge pulses) live in CSS keyframes under .sg-* utility
// classes in globals.css — they're cheaper than per-frame React work.
// Only transient entrance animations need Framer variants:
// ────────────────────────────────────────────────────────────────

/** Stat-value tick-up on mount. Sequential via stagger.delayChildren. */
export const tickUp: Variants = {
  hidden: { opacity: 0, y: 8 },
  visible: {
    opacity: 1,
    y: 0,
    transition: { duration: 0.8, ease: [0.16, 1, 0.3, 1] },
  },
};

/** Command palette entrance. 260ms, subtle rise + scale. */
export const paletteIn: Variants = {
  hidden: { opacity: 0, y: -12, scale: 0.98 },
  visible: {
    opacity: 1,
    y: 0,
    scale: 1,
    transition: { duration: 0.26, ease: [0.16, 1, 0.3, 1] },
  },
};

// ────────────────────────────────────────────────────────────────
// Liquid Glass — non-linear spring choreography.
// Springs, not curves: entrances overshoot slightly and settle, the
// way liquid glass would. Use `springs.*` as `transition` values and
// the variant pairs below for orchestrated sequences.
// ────────────────────────────────────────────────────────────────

/** Canonical spring transitions. */
export const springs = {
  /** Default UI spring — visible overshoot, settles fast. */
  soft: { type: "spring", stiffness: 260, damping: 22, mass: 0.8 } as Transition,
  /** Playful bounce for small elements (chips, pills, badges). */
  bouncy: { type: "spring", stiffness: 380, damping: 17, mass: 0.6 } as Transition,
  /** Snappy, barely-overshooting — shared-layout indicators, tabs. */
  snappy: { type: "spring", stiffness: 480, damping: 34, mass: 0.7 } as Transition,
  /** Large surfaces (dialogs, drawers) — weighty but alive. */
  surface: { type: "spring", stiffness: 300, damping: 26, mass: 1 } as Transition,
} as const;

/** Card/tile entrance: rise + scale with spring overshoot. */
export const liquidRise: Variants = {
  hidden: { opacity: 0, y: 14, scale: 0.97 },
  visible: { opacity: 1, y: 0, scale: 1, transition: springs.soft },
};

/** Orchestrator for liquidRise children — tighter, livelier cascade. */
export const liquidStagger: Variants = {
  hidden: {},
  visible: { transition: { staggerChildren: 0.045, delayChildren: 0.02 } },
};

/** Overlay surfaces (dialog/drawer/palette) — spring scale-in. */
export const liquidSurface: Variants = {
  hidden: { opacity: 0, scale: 0.94, y: 10 },
  visible: { opacity: 1, scale: 1, y: 0, transition: springs.surface },
  exit: { opacity: 0, scale: 0.98, transition: { duration: 0.14, ease: "easeIn" } },
};

/** Interactive gel props — spread onto motion buttons/chips. */
export const gelTap = {
  whileHover: { y: -2 },
  whileTap: { scale: 0.96 },
  transition: springs.bouncy,
} as const;

// ---------- reduced-motion friendly copies ----------

const instantFadeUp: Variants = {
  hidden: { opacity: 0, y: 0 },
  visible: { opacity: 1, y: 0, transition: { duration: 0 } },
};

const instantStagger: Variants = {
  hidden: {},
  visible: { transition: { staggerChildren: 0, delayChildren: 0 } },
};

const instantSpringPop: Variants = {
  hidden: { opacity: 0, scale: 1 },
  visible: { opacity: 1, scale: 1, transition: { duration: 0 } },
};

const instantListItem: Variants = {
  hidden: { opacity: 0, y: 0 },
  visible: { opacity: 1, y: 0, transition: { duration: 0 } },
};

const instantSharedCard = {
  layout: true as const,
  transition: { duration: 0 } as Transition,
};

const instantTickUp: Variants = {
  hidden: { opacity: 0, y: 0 },
  visible: { opacity: 1, y: 0, transition: { duration: 0 } },
};

const instantPaletteIn: Variants = {
  hidden: { opacity: 0, y: 0, scale: 1 },
  visible: {
    opacity: 1,
    y: 0,
    scale: 1,
    transition: { duration: 0 },
  },
};

const instantLiquidRise: Variants = {
  hidden: { opacity: 0, y: 0, scale: 1 },
  visible: { opacity: 1, y: 0, scale: 1, transition: { duration: 0 } },
};

const instantLiquidStagger: Variants = {
  hidden: {},
  visible: { transition: { staggerChildren: 0, delayChildren: 0 } },
};

const instantLiquidSurface: Variants = {
  hidden: { opacity: 0, scale: 1, y: 0 },
  visible: { opacity: 1, scale: 1, y: 0, transition: { duration: 0 } },
  exit: { opacity: 0, transition: { duration: 0 } },
};

const instantGelTap = {
  whileHover: {},
  whileTap: {},
  transition: { duration: 0 } as Transition,
} as const;

export interface MotionVariants {
  fadeUp: Variants;
  stagger: Variants;
  springPop: Variants;
  listItem: Variants;
  sharedCard: { layout: true; transition: Transition };
  tickUp: Variants;
  paletteIn: Variants;
  liquidRise: Variants;
  liquidStagger: Variants;
  liquidSurface: Variants;
  gelTap: typeof gelTap | typeof instantGelTap;
}

/**
 * Returns animated or instant variants based on the user's reduced-motion
 * preference. Must be called from within a React component.
 */
export function useMotionVariants(): MotionVariants {
  const reduced = useReducedMotion();
  if (reduced) {
    return {
      fadeUp: instantFadeUp,
      stagger: instantStagger,
      springPop: instantSpringPop,
      listItem: instantListItem,
      sharedCard: instantSharedCard,
      tickUp: instantTickUp,
      paletteIn: instantPaletteIn,
      liquidRise: instantLiquidRise,
      liquidStagger: instantLiquidStagger,
      liquidSurface: instantLiquidSurface,
      gelTap: instantGelTap,
    };
  }
  return {
    fadeUp,
    stagger,
    springPop,
    listItem,
    sharedCard,
    tickUp,
    paletteIn,
    liquidRise,
    liquidStagger,
    liquidSurface,
    gelTap,
  };
}
