"use client";

import * as React from "react";
import {
  motion,
  useMotionValue,
  useSpring,
  useTransform,
  type HTMLMotionProps,
} from "framer-motion";
import { cn } from "@/lib/utils";

export interface TiltCardProps extends HTMLMotionProps<"div"> {
  /** Max tilt angle in degrees (applied to both axes). */
  maxTiltDeg?: number;
  children?: React.ReactNode;
}

function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = React.useState(false);
  React.useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mql = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(mql.matches);
    update();
    if (mql.addEventListener) {
      mql.addEventListener("change", update);
      return () => mql.removeEventListener("change", update);
    }
    mql.addListener(update);
    return () => mql.removeListener(update);
  }, []);
  return reduced;
}

function useCoarsePointer(): boolean {
  const [coarse, setCoarse] = React.useState(false);
  React.useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mql = window.matchMedia("(pointer: coarse)");
    const update = () => setCoarse(mql.matches);
    update();
    if (mql.addEventListener) {
      mql.addEventListener("change", update);
      return () => mql.removeEventListener("change", update);
    }
    mql.addListener(update);
    return () => mql.removeListener(update);
  }, []);
  return coarse;
}

/**
 * Wraps children in a subtle cursor-driven tilt on the Spatial Glass card
 * recipe (gradient faux glass, no backdrop-filter). Falls back to a plain
 * (untilted) card under `prefers-reduced-motion` or touch (`pointer: coarse`)
 * environments. Uses motion values scoped to the element — no window-level
 * listeners.
 */
const surface = "rounded-sg-lg border border-sg-border bg-sg-card-grad shadow-sg-2";

export const TiltCard = React.forwardRef<HTMLDivElement, TiltCardProps>(
  function TiltCard(
    { maxTiltDeg = 3, children, className, ...rest },
    ref,
  ) {
    const reduced = usePrefersReducedMotion();
    const coarse = useCoarsePointer();
    const disabled = reduced || coarse;

    const mx = useMotionValue(0);
    const my = useMotionValue(0);
    const springX = useSpring(mx, { stiffness: 200, damping: 20 });
    const springY = useSpring(my, { stiffness: 200, damping: 20 });
    const rotateY = useTransform(springX, [-0.5, 0.5], [-maxTiltDeg, maxTiltDeg]);
    const rotateX = useTransform(springY, [-0.5, 0.5], [maxTiltDeg, -maxTiltDeg]);

    const onMouseMove = React.useCallback(
      (e: React.MouseEvent<HTMLDivElement>) => {
        if (disabled) return;
        const rect = e.currentTarget.getBoundingClientRect();
        const px = (e.clientX - rect.left) / rect.width - 0.5;
        const py = (e.clientY - rect.top) / rect.height - 0.5;
        mx.set(px);
        my.set(py);
      },
      [disabled, mx, my],
    );

    const onMouseLeave = React.useCallback(() => {
      if (disabled) return;
      mx.set(0);
      my.set(0);
    }, [disabled, mx, my]);

    if (disabled) {
      return (
        <motion.div ref={ref} className={cn(surface, className)} {...rest}>
          {children}
        </motion.div>
      );
    }

    return (
      <motion.div
        ref={ref}
        onMouseMove={onMouseMove}
        onMouseLeave={onMouseLeave}
        style={{ rotateX, rotateY, transformStyle: "preserve-3d" }}
        className={cn(surface, "will-change-transform", className)}
        {...rest}
      >
        {children}
      </motion.div>
    );
  },
);

export default TiltCard;
