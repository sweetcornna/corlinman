"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

export interface CountdownRingProps
  extends React.HTMLAttributes<HTMLDivElement> {
  remainingMs: number;
  totalMs: number;
  size?: number;
  strokeWidth?: number;
  label?: string;
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

function formatMs(ms: number): string {
  const clamped = Math.max(0, ms);
  const totalSec = Math.ceil(clamped / 1000);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

/**
 * Circular SVG countdown. Ticks via requestAnimationFrame while the caller's
 * `remainingMs` is unchanged (so parents don't need to re-render every frame).
 * Under reduced-motion we only refresh the text label — no stroke animation.
 */
export const CountdownRing = React.forwardRef<
  HTMLDivElement,
  CountdownRingProps
>(function CountdownRing(
  {
    remainingMs,
    totalMs,
    size = 32,
    strokeWidth = 3,
    label,
    className,
    ...rest
  },
  ref,
) {
  const reduced = usePrefersReducedMotion();

  const startedAtRef = React.useRef<number>(
    typeof performance !== "undefined" ? performance.now() : Date.now(),
  );
  const baseRemainingRef = React.useRef<number>(remainingMs);

  const [liveMs, setLiveMs] = React.useState<number>(remainingMs);

  // Reset the local clock whenever the caller hands us a new target.
  React.useEffect(() => {
    startedAtRef.current =
      typeof performance !== "undefined" ? performance.now() : Date.now();
    baseRemainingRef.current = remainingMs;
    setLiveMs(remainingMs);
  }, [remainingMs]);

  React.useEffect(() => {
    let raf = 0;
    let cancelled = false;

    const tick = () => {
      if (cancelled) return;
      const now =
        typeof performance !== "undefined" ? performance.now() : Date.now();
      const elapsed = now - startedAtRef.current;
      const next = Math.max(0, baseRemainingRef.current - elapsed);
      setLiveMs(next);
      if (next > 0) {
        raf = requestAnimationFrame(tick);
      }
    };

    raf = requestAnimationFrame(tick);
    return () => {
      cancelled = true;
      cancelAnimationFrame(raf);
    };
  }, [remainingMs]);

  const safeTotal = Math.max(1, totalMs);
  const progress = Math.min(1, Math.max(0, liveMs / safeTotal));
  const radius = (size - strokeWidth) / 2;
  const circumference = 2 * Math.PI * radius;
  const displayMs = reduced ? remainingMs : liveMs;
  const displayProgress = reduced ? Math.min(1, Math.max(0, remainingMs / safeTotal)) : progress;
  const dashOffset = circumference * (1 - displayProgress);
  const isUrgent = displayProgress < 0.2;

  return (
    // role="timer" + aria-value* is intentional: exposes remaining time to AT.
    // eslint-disable-next-line jsx-a11y/role-supports-aria-props
    <div
      ref={ref}
      role="timer"
      aria-valuemin={0}
      aria-valuemax={totalMs}
      aria-valuenow={Math.round(displayMs)}
      aria-label={label}
      className={cn("inline-flex items-center gap-2", className)}
      {...rest}
    >
      <svg
        width={size}
        height={size}
        viewBox={`0 0 ${size} ${size}`}
        aria-hidden="true"
      >
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="color-mix(in oklch, var(--sg-ink-5) 30%, transparent)"
          strokeWidth={strokeWidth}
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke={isUrgent ? "var(--sg-err)" : "var(--sg-accent)"}
          strokeWidth={strokeWidth}
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={dashOffset}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
          style={reduced ? undefined : { transition: "stroke 200ms ease" }}
        />
      </svg>
      <span
        className={cn(
          "tabular-nums text-xs",
          isUrgent ? "text-sg-err" : "text-sg-ink-4",
        )}
      >
        {formatMs(displayMs)}
      </span>
    </div>
  );
});

export default CountdownRing;
