"use client";

import * as React from "react";
import { cn } from "@/lib/utils";
import { GlassPanel } from "./glass-panel";

/**
 * Stat tile: label + large value + optional delta + ambient sparkline.
 *
 * Built on `<GlassPanel>`. Pass `variant="primary"` for the "most active"
 * metric on a dashboard row — adds the amber ring/glow outline and a
 * `live` badge next to the label.
 *
 * Sparkline is rendered as an SVG at the bottom of the panel (absolute).
 * The path string is provided by the caller — shape it with the same
 * geometric scale you use on the rest of the page (`0 0 300 36`
 * `preserveAspectRatio="none"`). This keeps the component dumb — no
 * autoscaling math inside.
 */

export type StatDelta = {
  /** Human string: "↑ 12.4%", "+ 412", etc. */
  label: string;
  /** Tone influences colour. `flat` stays muted. */
  tone?: "up" | "down" | "flat";
};

export type StatChipVariant = "default" | "primary";

export interface StatChipProps extends React.HTMLAttributes<HTMLDivElement> {
  label: string;
  /** Formatted display value — the caller does the formatting. */
  value: React.ReactNode;
  delta?: StatDelta;
  /** Small helper text below the value. Use for context (p50, subscribers…). */
  foot?: React.ReactNode;
  /** Raw SVG `<path d>` for the ambient sparkline. */
  sparkPath?: string;
  sparkTone?: "amber" | "ember" | "peach";
  /** `primary` adds the amber outline + "live" badge. */
  variant?: StatChipVariant;
  /** When true, shows a 'live' badge next to the label. */
  live?: boolean;
}

const sparkGradientStops: Record<
  NonNullable<StatChipProps["sparkTone"]>,
  { top: string; bottom: string }
> = {
  // Re-mapped onto the Spatial Glass accent palette: amber→cyan primary,
  // ember→violet secondary, peach→ok green.
  amber: {
    top: "color-mix(in oklch, var(--sg-accent) 32%, transparent)",
    bottom: "color-mix(in oklch, var(--sg-accent) 0%, transparent)",
  },
  ember: {
    top: "color-mix(in oklch, var(--sg-accent-2) 24%, transparent)",
    bottom: "color-mix(in oklch, var(--sg-accent-2) 0%, transparent)",
  },
  peach: {
    top: "color-mix(in oklch, var(--sg-ok) 22%, transparent)",
    bottom: "color-mix(in oklch, var(--sg-ok) 0%, transparent)",
  },
};

const deltaToneClass: Record<NonNullable<StatDelta["tone"]>, string> = {
  up: "text-sg-ok",
  down: "text-sg-err",
  flat: "text-sg-ink-3",
};

export const StatChip = React.forwardRef<HTMLDivElement, StatChipProps>(
  function StatChip(
    {
      label,
      value,
      delta,
      foot,
      sparkPath,
      sparkTone = "amber",
      variant = "default",
      live = false,
      className,
      ...rest
    },
    ref,
  ) {
    const gradientId = React.useId();
    const grad = sparkGradientStops[sparkTone];
    const isPrimary = variant === "primary";

    return (
      <GlassPanel
        ref={ref}
        // Primary chips get the glow treatment; secondary chips use the same
        // glass filter with a quieter shadow so page-to-page glass does not
        // visibly switch between blurred and solid surfaces.
        variant={isPrimary ? "primary" : "subtle"}
        className={cn(
          "flex flex-col gap-2 overflow-hidden px-[18px] pb-[14px] pt-4",
          className,
        )}
        {...rest}
      >
        <div className="flex items-center gap-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-sg-ink-3">
          {label}
          {(live || isPrimary) && (
            <span
              className={cn(
                "rounded-full px-1.5 py-[1px] text-[9px] font-medium lowercase tracking-[0.04em]",
                "bg-sg-accent-soft text-sg-accent tp-breathe-amber",
              )}
            >
              live
            </span>
          )}
        </div>

        <div className="flex items-end justify-between gap-2.5">
          <span
            className={cn(
              "font-sans text-[34px] font-medium leading-none tracking-[-0.03em] tabular-nums animate-tp-tick-up",
              isPrimary ? "sg-grad-text" : "text-sg-ink",
            )}
          >
            {value}
          </span>
          {delta ? (
            <span className={cn("font-mono text-[11px]", deltaToneClass[delta.tone ?? "up"])}>
              {delta.label}
            </span>
          ) : null}
        </div>

        {foot ? (
          <div className="border-t border-sg-border pt-1.5 text-[11.5px] text-sg-ink-3">
            {foot}
          </div>
        ) : null}

        {sparkPath ? (
          <svg
            aria-hidden
            viewBox="0 0 300 36"
            preserveAspectRatio="none"
            className={cn(
              // Ambient underline, not a layer. Low opacity + short height
              // so it reads as a glow at the base rather than a tinted block
              // behind the foot text.
              "pointer-events-none absolute inset-x-0 bottom-0 h-6 w-full",
              isPrimary ? "opacity-40" : "opacity-20",
            )}
          >
            <defs>
              <linearGradient id={gradientId} x1="0" x2="0" y1="0" y2="1">
                <stop offset="0%" stopColor={grad.top} />
                <stop offset="100%" stopColor={grad.bottom} />
              </linearGradient>
            </defs>
            <path d={sparkPath} fill={`url(#${gradientId})`} />
          </svg>
        ) : null}
      </GlassPanel>
    );
  },
);

export default StatChip;
