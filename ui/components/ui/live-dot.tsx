"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

type DotVariant = "ok" | "warn" | "err" | "muted";

export interface LiveDotProps extends React.HTMLAttributes<HTMLSpanElement> {
  variant?: DotVariant;
  pulse?: boolean;
  label?: string;
}

const colorFor: Record<DotVariant, string> = {
  ok: "bg-sg-ok",
  warn: "bg-sg-warn",
  err: "bg-sg-err",
  // Idle / off — faint ink, no live read.
  muted: "bg-sg-ink-5",
};

const ringFor: Record<DotVariant, string> = {
  ok: "bg-sg-ok/50",
  warn: "bg-sg-warn/50",
  err: "bg-sg-err/50",
  muted: "bg-sg-ink-5/30",
};

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

/**
 * Tiny 8px status dot with optional breathing pulse. Honors reduced-motion
 * (no glow pulse). Pass `label` for an invisible screen-reader announcement.
 */
export const LiveDot = React.forwardRef<HTMLSpanElement, LiveDotProps>(
  function LiveDot(
    { variant = "ok", pulse = true, label, className, ...rest },
    ref,
  ) {
    const reduced = usePrefersReducedMotion();
    const showPulse = pulse && !reduced;

    return (
      <span
        ref={ref}
        className={cn("relative inline-flex h-2 w-2 shrink-0", className)}
        data-variant={variant}
        {...rest}
      >
        {showPulse ? (
          <span
            aria-hidden="true"
            className={cn(
              "absolute inset-0 rounded-full animate-ping",
              ringFor[variant],
            )}
          />
        ) : null}
        <span
          aria-hidden="true"
          className={cn(
            "relative inline-flex h-2 w-2 rounded-full",
            colorFor[variant],
          )}
        />
        {label ? <span className="sr-only">{label}</span> : null}
      </span>
    );
  },
);

export default LiveDot;
