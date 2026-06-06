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
 * keyframe tp-just-now on globals). Use this when a row was appended to
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
  ok: "bg-tp-ok-soft text-tp-ok border-tp-ok/25",
  info: "bg-tp-glass-inner-strong text-tp-ink-3 border-tp-glass-edge",
  warn: "bg-tp-warn-soft text-tp-warn border-tp-warn/25",
  err: "bg-tp-err-soft text-tp-err border-tp-err/25",
};

const statusDot: Record<LogSeverity, string> = {
  ok: "bg-tp-ok",
  info: "bg-tp-ink-4",
  warn: "bg-tp-warn",
  err: "bg-tp-err",
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
          "border-b border-tp-glass-edge transition-colors",
          "hover:bg-tp-glass-inner-hover focus-visible:outline-none",
          "focus-visible:ring-2 focus-visible:ring-tp-amber/40",
          dense
            ? "grid-cols-[70px_56px_140px_1fr_auto] px-4 py-2 text-[12.5px]"
            : "grid-cols-[60px_120px_1fr_auto] px-4 py-3.5 text-[13.5px]",
          selected && "bg-tp-amber-soft",
          selected && "shadow-[inset_2px_0_0_var(--tp-amber),inset_0_0_0_1px_color-mix(in_oklch,var(--tp-amber)_20%,transparent)]",
          className,
        )}
        {...rest}
      >
        {/* Just-now left-edge bar — overlaps selected accent; selected wins. */}
        {justNow && !selected ? (
          <span
            aria-hidden
            className="absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-sm bg-tp-amber shadow-[0_0_8px_var(--tp-amber-glow)] tp-just-now"
          />
        ) : null}

        <span className="min-w-0 font-mono text-[11px] text-tp-ink-4">
          {ts}
        </span>

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

        <span className="min-w-0 truncate font-mono text-[11.5px] text-tp-ink-3">
          {subsystem}
        </span>

        <span className="min-w-0 truncate text-tp-ink-2">{message}</span>

        <span
          className={cn(
            "flex min-w-0 items-center justify-end gap-2 whitespace-nowrap",
            dense ? "font-mono text-[10.5px] tabular-nums text-tp-ink-4" : "",
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
