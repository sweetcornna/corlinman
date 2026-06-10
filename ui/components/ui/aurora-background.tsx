"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Fixed-position Spatial Glass backdrop.
 *
 * Layers, deepest → top (all `pointer-events-none`, sit behind content):
 *   1. Nebula blobs — 3 soft radial glows in the sg accent hues
 *      (`--sg-nebula-1/2/3`), drifting slowly via `.sg-drift`
 *      (reduced-motion gated in globals.css).
 *   2. Vignette — a radial fade to near-black at the edges so corners
 *      keep depth instead of going flat.
 *   3. Noise — `.sg-noise` at ~3% opacity to break gradient banding.
 *
 * The deep-space base gradient itself is painted on `html` in globals.css
 * (pre-hydration, no FOUC); this component only adds the glow + texture.
 * Tokens flip with `.dark`, so a single tree covers both themes.
 *
 * Mount **once** at the admin layout root.
 */

export interface AuroraBackgroundProps
  extends React.HTMLAttributes<HTMLDivElement> {
  /** When true, render as a `fixed` full-viewport element (default). */
  fixed?: boolean;
}

export const AuroraBackground = React.forwardRef<
  HTMLDivElement,
  AuroraBackgroundProps
>(function AuroraBackground({ fixed = true, className, style, ...rest }, ref) {
  return (
    <div
      ref={ref}
      aria-hidden="true"
      className={cn(
        fixed && "fixed inset-0 -z-10",
        "sg-bg-root overflow-hidden pointer-events-none",
        className,
      )}
      style={style}
      {...rest}
    >
      {/* Nebula glow blobs — soft accent-hued radials that drift slowly and
          cycle hue over ~90s (liquid light, never static). Composed from the
          sg-nebula tokens so they flip light/dark. */}
      <div
        className="absolute inset-0 sg-drift lg-hue-drift pointer-events-none"
        style={{
          backgroundImage:
            "radial-gradient(900px 560px at 15% 8%, var(--sg-nebula-1), transparent 60%), " +
            "radial-gradient(760px 540px at 86% 18%, var(--sg-nebula-2), transparent 60%), " +
            "radial-gradient(680px 460px at 52% 96%, var(--sg-nebula-3), transparent 62%)",
        }}
      />

      {/* Sparse twinkling starfield — dark theme only (hidden in light via
          the .lg-stars rule); two offset copies so twinkle phases differ. */}
      <div className="absolute inset-0 lg-stars pointer-events-none" />
      <div
        className="absolute inset-0 lg-stars pointer-events-none"
        style={{ transform: "scale(-1, 1)", animationDelay: "-3.5s" }}
      />

      {/* Depth vignette — translucent fade so corners keep depth without
          burying the nebula or the html gradient. */}
      <div
        className="absolute inset-0 pointer-events-none"
        style={{
          background:
            "radial-gradient(120% 90% at 50% 30%, transparent 0%, transparent 48%, " +
            "color-mix(in oklch, var(--sg-space-0) 55%, transparent) 100%)",
        }}
      />

      {/* Full-screen fractal noise — breaks gradient banding at ~3%. */}
      <div className="absolute inset-0 sg-noise opacity-[0.03] pointer-events-none" />
    </div>
  );
});

export default AuroraBackground;
