"use client";

import * as React from "react";
import { motion, useReducedMotion } from "framer-motion";
import { AnimatedNumber } from "@/components/ui/animated-number";
import { cn } from "@/lib/utils";

export interface HealthRingProps {
  value: number;
  total: number;
  size?: number;
  className?: string;
}

/**
 * Concentric ring visualising `value/total`. Foreground stroke animates from
 * 0 → `value/total` on mount via framer-motion's spring. Snaps to the final
 * state when the user prefers reduced motion.
 */
export function HealthRing({
  value,
  total,
  size = 120,
  className,
}: HealthRingProps) {
  const reduced = useReducedMotion();
  const safeTotal = total > 0 ? total : 1;
  const progress = Math.max(0, Math.min(1, value / safeTotal));

  const stroke = 10;
  const radius = (size - stroke) / 2;
  const cx = size / 2;
  const cy = size / 2;

  return (
    <div
      className={cn(
        "relative inline-flex items-center justify-center",
        className,
      )}
      style={{ width: size, height: size }}
    >
      <svg
        width={size}
        height={size}
        viewBox={`0 0 ${size} ${size}`}
        role="img"
        aria-label={`${value} of ${total} checks healthy`}
      >
        <circle
          cx={cx}
          cy={cy}
          r={radius}
          fill="none"
          stroke="currentColor"
          strokeWidth={stroke}
          className="text-sg-border"
        />
        <motion.circle
          cx={cx}
          cy={cy}
          r={radius}
          fill="none"
          stroke="var(--sg-ok)"
          strokeWidth={stroke}
          strokeLinecap="round"
          pathLength={1}
          strokeDasharray={1}
          initial={{ strokeDashoffset: reduced ? 1 - progress : 1 }}
          animate={{ strokeDashoffset: 1 - progress }}
          transition={
            reduced
              ? { duration: 0 }
              : { type: "spring", stiffness: 80, damping: 20 }
          }
          style={{
            transform: `rotate(-90deg)`,
            transformOrigin: `${cx}px ${cy}px`,
            filter: "drop-shadow(0 0 6px var(--sg-accent-glow))",
          }}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <div className="font-mono text-2xl font-semibold tracking-tight text-sg-ink">
          <AnimatedNumber value={value} format="number" />
        </div>
        <div className="font-mono text-xs text-sg-ink-3">
          / {total}
        </div>
      </div>
    </div>
  );
}

export default HealthRing;
