"use client";

import * as React from "react";

/**
 * Pointer-tracked specular highlight for Liquid Glass surfaces.
 *
 * Writes `--lg-x` / `--lg-y` / `--lg-on` directly on the element (no React
 * re-render per move — light must not lag a frame behind the pointer).
 * Pair with the `.lg-specular` utility, which renders the highlight layer.
 *
 * No-ops for touch-only devices and when the user prefers reduced motion.
 */
export function useSpecular<T extends HTMLElement = HTMLDivElement>() {
  const ref = React.useRef<T | null>(null);

  React.useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (typeof window.matchMedia !== "function") return;
    if (
      !window.matchMedia("(hover: hover)").matches ||
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ) {
      return;
    }

    const onMove = (e: PointerEvent) => {
      const rect = el.getBoundingClientRect();
      el.style.setProperty("--lg-x", `${(((e.clientX - rect.left) / rect.width) * 100).toFixed(2)}%`);
      el.style.setProperty("--lg-y", `${(((e.clientY - rect.top) / rect.height) * 100).toFixed(2)}%`);
    };
    const onEnter = () => el.style.setProperty("--lg-on", "1");
    const onLeave = () => el.style.setProperty("--lg-on", "0");

    el.addEventListener("pointermove", onMove, { passive: true });
    el.addEventListener("pointerenter", onEnter, { passive: true });
    el.addEventListener("pointerleave", onLeave, { passive: true });
    return () => {
      el.removeEventListener("pointermove", onMove);
      el.removeEventListener("pointerenter", onEnter);
      el.removeEventListener("pointerleave", onLeave);
    };
  }, []);

  return ref;
}
