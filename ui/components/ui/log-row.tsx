"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Shared log-line row. Used on the Dashboard Activity pane and in the Logs
 * stream page (those pass `variant="dense"` for info density; the Dashboard
 * uses `"comfortable"` with larger padding and alternating row tint).
 *
 * Columns are CSS-grid so overflowing messages truncate cleanly rather than
 * wrapping — the full message appears on hover via the browser's native
 * title tooltip (caller decides whether to set `title`).
 *
 * Severity map tracks a deliberately small vocabulary:
 *   ok (green) · info (neutral) · warn (amber) · err (red)
 *
 * The `justNow` flag lights up a 2px amber left-edge bar for ~2.8s (CSS
 * keyframe sg-just-now on globals). Use this when a row was appended to
 * the list within the last few seconds (typically the first row of an
 * SSE stream tick).
 */

export type LogSeverity = "ok" | "info" | "warn" | "err";

export interface LogRowProps extends React.HTMLAttributes<HTMLButtonElement> {
  ts: string;
  severity: LogSeverity;
  subsystem: string;
  /** Main message. Can include inline <em>/<code> for emphasis. */
  message: React.ReactNode;
  /** Optional duration suffix ("342ms", "—"). */
  duration?: string;
  /** `true` when this row arrived within the last ~2.8s (stream tick). */
  justNow?: boolean;
  /** `true` when this row is the currently-selected detail target. */
  selected?: boolean;
  /** Dense = Logs-page info density; comfortable = Dashboard activity. */
  variant?: "dense" | "comfortable";
}

const severityLabel: Record<LogSeverity, string> = {
  ok: "ok",
  info: "info",
  warn: "warn",
  err: "err",
};

const severityPill: Record<LogSeverity, string> = {
  ok: "bg-sg-ok-soft text-sg-ok border-sg-ok/25",
  info: "bg-sg-inset-strong text-sg-ink-3 border-sg-border",
  warn: "bg-sg-warn-soft text-sg-warn border-sg-warn/25",
  err: "bg-sg-err-soft text-sg-err border-sg-err/25",
};

// Status dot fill + a faint matching glow (shadow only — no blur).
const statusDot: Record<LogSeverity, string> = {
  ok: "bg-sg-ok shadow-[0_0_6px_color-mix(in_oklch,var(--sg-ok)_55%,transparent)]",
  info: "bg-sg-ink-4",
  warn: "bg-sg-warn shadow-[0_0_6px_color-mix(in_oklch,var(--sg-warn)_55%,transparent)]",
  err: "bg-sg-err shadow-[0_0_6px_color-mix(in_oklch,var(--sg-err)_55%,transparent)]",
};

export const LogRow = React.forwardRef<HTMLButtonElement, LogRowProps>(
  function LogRow(
    {
      ts,
      severity,
      subsystem,
      message,
      duration,
      justNow,
      selected,
      variant = "dense",
      className,
      ...rest
    },
    ref,
  ) {
    const dense = variant === "dense";
    return (
      <button
        ref={ref}
        type="button"
        data-selected={selected || undefined}
        data-just-now={justNow || undefined}
        className={cn(
          "relative grid w-full items-center gap-3 text-left",
          "border-b border-sg-border transition-colors",
          "hover:bg-sg-accent-soft focus-visible:outline-none",
          "focus-visible:ring-2 focus-visible:ring-sg-accent/40",
          dense
            ? "grid-cols-[70px_56px_140px_1fr_auto] px-4 py-2 text-[12.5px]"
            : "grid-cols-[60px_120px_1fr_auto] px-4 py-3.5 text-[13.5px]",
          selected && "bg-sg-accent-soft",
          selected && "shadow-[inset_2px_0_0_var(--sg-accent),inset_0_0_0_1px_color-mix(in_oklch,var(--sg-accent)_20%,transparent)]",
          className,
        )}
        {...rest}
      >
        {/* Just-now left-edge bar — overlaps selected accent; selected wins. */}
        {justNow && !selected ? (
          <span
            aria-hidden
            className="absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-sm bg-sg-accent shadow-[0_0_8px_var(--sg-accent-glow)] sg-just-now"
          />
        ) : null}

        <span className="font-mono text-[11px] text-sg-ink-4">{ts}</span>

        {dense ? (
          <span
            className={cn(
              "inline-flex rounded px-1.5 py-px text-[9.5px] font-medium uppercase tracking-wider border",
              severityPill[severity],
              "w-fit",
            )}
          >
            {severityLabel[severity]}
          </span>
        ) : null}

        <span className="font-mono text-[11.5px] text-sg-ink-3">
          {subsystem}
        </span>

        <span className="truncate text-sg-ink-2">{message}</span>

        <span
          className={cn(
            "flex items-center gap-2",
            dense ? "font-mono text-[10.5px] tabular-nums text-sg-ink-4" : "",
          )}
        >
          {duration}
          {!dense ? (
            <span
              aria-hidden
              className={cn("h-1.5 w-1.5 rounded-full", statusDot[severity])}
            />
          ) : null}
        </span>
      </button>
    );
  },
);

export default LogRow;
