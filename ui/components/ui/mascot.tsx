"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * The corlinman pixel mascot, staged as a Liquid Glass hero object:
 * a layered accent glow behind it, a gentle float (reduced-motion
 * gated in CSS), and a soft ground-light ellipse beneath. Pixel art is
 * kept crisp with `image-rendering: pixelated`.
 */
export interface MascotProps extends React.HTMLAttributes<HTMLDivElement> {
  /** Rendered square size of the mascot sprite, px. */
  size?: number;
  /** Disable the float loop (e.g. dense contexts). */
  still?: boolean;
}

export function Mascot({ size = 132, still = false, className, ...rest }: MascotProps) {
  return (
    <div
      aria-hidden="true"
      data-testid="mascot"
      className={cn("relative inline-flex flex-col items-center", className)}
      {...rest}
    >
      {/* Back glow — cyan core with a violet rim, the mascot's own light. */}
      <div
        className="pointer-events-none absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 rounded-full"
        style={{
          width: size * 1.9,
          height: size * 1.9,
          background:
            "radial-gradient(circle, var(--sg-accent-glow), color-mix(in oklch, var(--sg-accent-2) 18%, transparent) 55%, transparent 72%)",
          filter: "blur(2px)",
        }}
      />
      <div className={cn("relative", !still && "")}>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src="/mascot.png"
          alt=""
          width={size}
          height={size}
          draggable={false}
          className="relative select-none"
          style={{
            imageRendering: "pixelated",
            filter:
              "drop-shadow(0 10px 24px color-mix(in oklch, var(--sg-accent) 35%, transparent))",
          }}
        />
      </div>
      {/* Ground light — soft ellipse the mascot floats above. */}
      <div
        className="pointer-events-none -mt-2 rounded-[50%]"
        style={{
          width: size * 0.78,
          height: size * 0.14,
          background:
            "radial-gradient(ellipse, color-mix(in oklch, var(--sg-accent) 28%, transparent), transparent 70%)",
          filter: "blur(3px)",
        }}
      />
    </div>
  );
}

export default Mascot;
