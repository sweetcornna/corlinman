"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Fixed-position background.
 *
 * Light mode: warm tidepool aurora (unchanged).
 * Dark mode: bas-relief navy artwork at /bg/relief-blue.jpg, layered under
 *   a deep-navy vignette + grain. The aurora gradients sit on top of the
 *   image at low alpha so the carving texture reads through but corners
 *   don't go flat.
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
      className={cn(fixed && "fixed inset-0 -z-10", "tp-bg-root", className)}
      style={style}
      {...rest}
    >
      {/* Day: a few faint radial washes, NO solid linear-gradient base
          (the `bg-tp-aurora` token includes one). Painting a solid
          ivory gradient here would hide the relief landscape that
          html paints below. */}
      <div
        className="absolute inset-0 dark:hidden"
        style={{
          background:
            "radial-gradient(900px 500px at 15% 10%, var(--tp-aurora-1), transparent 60%), " +
            "radial-gradient(700px 500px at 85% 20%, var(--tp-aurora-2), transparent 60%), " +
            "radial-gradient(600px 400px at 50% 95%, var(--tp-aurora-3), transparent 60%)",
        }}
      />

      {/* Night: relief artwork is painted on `html.dark` (see globals.css),
          so this layer only adds the depth vignette + cool aurora wash. */}
      <div className="absolute inset-0 hidden dark:block">
        <div
          className="absolute inset-0"
          style={{
            background:
              "radial-gradient(120% 90% at 50% 35%, transparent 0%, hsl(226 70% 3% / 0.25) 65%, hsl(226 80% 2% / 0.5) 100%)",
          }}
        />
        <div className="absolute inset-0 bg-tp-aurora opacity-35" />
      </div>
    </div>
  );
});

export default AuroraBackground;
