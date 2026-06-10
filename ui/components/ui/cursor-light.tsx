"use client";

import * as React from "react";

/**
 * Global cursor light — the web translation of Liquid Glass
 * "touch-point radiance": a soft ambient halo that travels with the
 * pointer and brightens every glass surface it passes over.
 *
 * Two layers on one fixed element:
 *   - a wide, very faint ambient halo (~520px) that lifts nearby glass
 *   - a tight, slightly brighter core (~140px) that reads as the light
 *     source itself
 * Blended with `soft-light` so it interacts with surface luminance
 * instead of painting a white blob over content.
 *
 * Perf: the element is moved with translate3d only (GPU compositing,
 * zero layout/paint), pointermove is rAF-coalesced, and the layer is
 * pointer-events-none. Disabled for touch-only devices and
 * prefers-reduced-motion.
 */
export function CursorLight() {
  const ref = React.useRef<HTMLDivElement | null>(null);
  const [enabled, setEnabled] = React.useState(false);

  React.useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    if (
      !window.matchMedia("(hover: hover) and (pointer: fine)").matches ||
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ) {
      return;
    }
    setEnabled(true);

    let raf = 0;
    let x = -1000;
    let y = -1000;

    const apply = () => {
      raf = 0;
      const el = ref.current;
      if (el) {
        el.style.transform = `translate3d(${x}px, ${y}px, 0)`;
        el.style.opacity = "1";
      }
    };
    const onMove = (e: PointerEvent) => {
      x = e.clientX;
      y = e.clientY;
      if (!raf) raf = requestAnimationFrame(apply);
    };
    const onLeave = () => {
      const el = ref.current;
      if (el) el.style.opacity = "0";
    };

    window.addEventListener("pointermove", onMove, { passive: true });
    document.documentElement.addEventListener("pointerleave", onLeave);
    window.addEventListener("blur", onLeave);
    return () => {
      if (raf) cancelAnimationFrame(raf);
      window.removeEventListener("pointermove", onMove);
      document.documentElement.removeEventListener("pointerleave", onLeave);
      window.removeEventListener("blur", onLeave);
    };
  }, []);

  if (!enabled) return null;

  return (
    <div
      aria-hidden="true"
      data-testid="cursor-light"
      className="pointer-events-none fixed inset-0 z-[60] overflow-hidden"
      style={{ mixBlendMode: "soft-light" }}
    >
      <div
        ref={ref}
        className="absolute left-0 top-0 opacity-0 transition-opacity duration-300"
        style={{ transform: "translate3d(-1000px, -1000px, 0)", willChange: "transform" }}
      >
        {/* Wide ambient halo — lifts the glass it travels across. */}
        <div
          className="absolute rounded-full"
          style={{
            width: 520,
            height: 520,
            left: -260,
            top: -260,
            background:
              "radial-gradient(circle, oklch(1 0 0 / 0.5), oklch(1 0 0 / 0.14) 42%, transparent 70%)",
          }}
        />
        {/* Tight core — the light source. */}
        <div
          className="absolute rounded-full"
          style={{
            width: 140,
            height: 140,
            left: -70,
            top: -70,
            background:
              "radial-gradient(circle, oklch(1 0 0 / 0.55), transparent 70%)",
          }}
        />
      </div>
    </div>
  );
}

export default CursorLight;
