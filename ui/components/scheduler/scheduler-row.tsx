"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { MoreVertical, Pause, Pencil, Play } from "lucide-react";

import { cn } from "@/lib/utils";
import { CountdownRing } from "@/components/ui/countdown-ring";
import type { SchedulerJob } from "@/lib/api";
import { formatCountdown, formatRelative, type SchedulerStatus } from "./scheduler-util";

/**
 * `<SchedulerRow>` — one cron job rendered as a flat glass row.
 *
 * Layout (left → right):
 *   - 8px status dot (ok / ink-4 / err) — matches the plugin card palette
 *   - job name + cron (mono, ink-3, truncate)
 *   - centre: CountdownRing with "in Xm Ys" label (paused/errored fallbacks)
 *   - right: icon actions — Run now (Play), Pause/Resume, Edit, overflow
 *
 * Interaction:
 *   - Whole row is a `<button>` so keyboard users can activate it. Clicking
 *     anywhere outside the action cluster fires `onSelect(job)` (parent
 *     opens the history drawer).
 *   - Action buttons stop propagation so they don't double-trigger select.
 *   - `selected` state paints the amber left accent bar; `errored` paints
 *     a subtle red accent.
 *
 * `Run now` (`triggerSchedulerJob`) and Pause/Resume
 * (`pauseSchedulerJob` / `resumeSchedulerJob`) map to backed APIs. Pause /
 * Resume only apply to runtime jobs (`source === "runtime"`) — config-derived
 * rows have no `enabled` gate, so the toggle is disabled for them with a
 * tooltip. Edit/overflow are still layout-only.
 */

const RING_TOTAL_MS = 60_000; // drain window — ring fills when >1 min away.

export interface SchedulerRowProps {
  job: SchedulerJob;
  status: SchedulerStatus;
  /** Tick source for the countdown. Pass a 1-Hz clock from the parent. */
  now: number;
  selected?: boolean;
  /** True while `triggerSchedulerJob` is in flight for this row. */
  triggering?: boolean;
  /** True while a pause/resume mutation is in flight for this row. */
  pausing?: boolean;
  onSelect: (name: string) => void;
  onTrigger: (name: string) => void;
  /** Pause/resume a runtime job. Omitted (or no-op) for config rows. */
  onPause?: (name: string) => void;
  onResume?: (name: string) => void;
}

export function SchedulerRow({
  job,
  status,
  now,
  selected = false,
  triggering = false,
  pausing = false,
  onSelect,
  onTrigger,
  onPause,
  onResume,
}: SchedulerRowProps) {
  const { t } = useTranslation();
  const errored = status === "errored";
  const paused = status === "paused";
  // Pause/resume only apply to operator-created runtime jobs — config rows
  // carry no `enabled` gate, so the toggle stays disabled with a tooltip.
  const isRuntime = job.source === "runtime";
  const canToggle = isRuntime && (paused ? Boolean(onResume) : Boolean(onPause));

  return (
    <div
      className={cn(
        "group relative overflow-hidden rounded-2xl border transition-colors",
        // left accent bar — amber when selected, red when errored, otherwise
        // invisible. Emitted as a pseudo via shadow-inset so the row itself
        // stays borderless.
        selected
          ? "border-tp-amber/40 bg-tp-amber-soft shadow-[inset_3px_0_0_var(--tp-amber)]"
          : errored
            ? "border-tp-err/25 bg-tp-glass hover:bg-tp-glass-inner-hover shadow-[inset_3px_0_0_color-mix(in_oklch,var(--tp-err)_70%,transparent)]"
            : "border-tp-glass-edge bg-tp-glass hover:bg-tp-glass-inner-hover hover:shadow-[inset_3px_0_0_var(--tp-amber)]",
      )}
      data-testid={`scheduler-row-${job.name}`}
      data-status={status}
      data-selected={selected || undefined}
    >
      <div className="flex items-center gap-3 px-4 py-3">
        {/* Row body — selectable region (whole row minus action cluster). */}
        <button
          type="button"
          onClick={() => onSelect(job.name)}
          aria-label={t("scheduler.tp.rowSelectAria", { name: job.name })}
          aria-pressed={selected || undefined}
          className={cn(
            "flex min-w-0 flex-1 items-center gap-3 text-left",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40 focus-visible:rounded-md",
          )}
        >
          {/* Status dot */}
          <span
            aria-hidden
            className={cn(
              "h-2 w-2 shrink-0 rounded-full",
              errored
                ? "bg-tp-err"
                : paused
                  ? "bg-tp-ink-4"
                  : "bg-tp-ok tp-breathe-amber",
            )}
          />

          {/* Name + cron */}
          <div className="min-w-0 flex-1">
            <div className="flex items-baseline gap-2">
              <span className="truncate text-[14px] font-medium text-tp-ink">
                {job.name}
              </span>
              <span className="shrink-0 font-mono text-[10px] uppercase tracking-[0.08em] text-tp-ink-4">
                {job.action_kind}
              </span>
            </div>
            <div className="mt-0.5 flex min-w-0 items-center gap-2 font-mono text-[11.5px] text-tp-ink-3">
              <span className="truncate">{job.cron}</span>
              <span className="shrink-0 text-tp-ink-4">
                · {job.timezone ?? "utc"}
              </span>
            </div>
          </div>

          {/* Countdown slot */}
          <div className="hidden shrink-0 sm:block">
            <CountdownSlot
              job={job}
              status={status}
              now={now}
            />
          </div>
        </button>

        {/* Action cluster — stops propagation so the row stays a single unit. */}
        <div
          className="flex shrink-0 items-center gap-1"
          onClick={(e) => e.stopPropagation()}
        >
          <IconButton
            label={t("scheduler.tp.actionRun")}
            onClick={() => onTrigger(job.name)}
            disabled={triggering}
            testId={`scheduler-trigger-${job.name}`}
          >
            <Play className="h-3.5 w-3.5" aria-hidden />
          </IconButton>
          <IconButton
            label={
              paused
                ? t("scheduler.tp.actionResume")
                : t("scheduler.tp.actionPause")
            }
            disabled={!canToggle || pausing}
            title={
              isRuntime
                ? paused
                  ? t("scheduler.tp.actionResume")
                  : t("scheduler.tp.actionPause")
                : t("scheduler.tp.actionConfigJob")
            }
            testId={`scheduler-toggle-${job.name}`}
            onClick={() => {
              if (!canToggle || pausing) return;
              if (paused) onResume?.(job.name);
              else onPause?.(job.name);
            }}
          >
            {paused ? (
              <Play className="h-3.5 w-3.5" aria-hidden />
            ) : (
              <Pause className="h-3.5 w-3.5" aria-hidden />
            )}
          </IconButton>
          <IconButton
            label={t("scheduler.tp.actionEdit")}
            disabled
            title={t("scheduler.tp.actionSoon")}
          >
            <Pencil className="h-3.5 w-3.5" aria-hidden />
          </IconButton>
          <IconButton
            label={t("scheduler.tp.actionMore")}
            disabled
            title={t("scheduler.tp.actionSoon")}
          >
            <MoreVertical className="h-3.5 w-3.5" aria-hidden />
          </IconButton>
        </div>
      </div>

      {/* Mobile fallback: countdown stacks under the row on xs. */}
      <div className="flex items-center justify-end gap-2 px-4 pb-3 sm:hidden">
        <CountdownSlot job={job} status={status} now={now} />
      </div>
    </div>
  );
}

// ─── Countdown slot ──────────────────────────────────────────────────────

function CountdownSlot({
  job,
  status,
  now,
}: {
  job: SchedulerJob;
  status: SchedulerStatus;
  now: number;
}) {
  const { t } = useTranslation();

  if (status === "errored") {
    return (
      <span
        className={cn(
          "inline-flex items-center gap-1.5 rounded-full border px-2 py-[3px]",
          "border-tp-err/35 bg-tp-err-soft text-tp-err",
          "font-mono text-[11px] tabular-nums",
        )}
      >
        {t("scheduler.tp.statusErroredShort")}
      </span>
    );
  }

  if (status === "paused" || !job.next_fire_at) {
    return (
      <span
        className={cn(
          "inline-flex items-center gap-1.5 rounded-full border px-2 py-[3px]",
          "border-tp-glass-edge bg-tp-glass-inner text-tp-ink-3",
          "font-mono text-[11px] tabular-nums",
        )}
      >
        {t("scheduler.tp.statusPaused")}
      </span>
    );
  }

  const then = new Date(job.next_fire_at).getTime();
  if (!Number.isFinite(then)) {
    return (
      <span className="font-mono text-[11px] text-tp-ink-4">
        {job.next_fire_at}
      </span>
    );
  }
  const delta = then - now;
  if (delta <= 0) {
    return (
      <span
        className={cn(
          "inline-flex items-center gap-1.5 rounded-full border px-2 py-[3px]",
          "border-tp-warn/30 bg-tp-warn-soft text-tp-warn",
          "font-mono text-[11px] tabular-nums",
        )}
      >
        {t("scheduler.tp.dueNow")}
      </span>
    );
  }

  const label = formatCountdown(delta) ?? "";
  return (
    <span className="inline-flex items-center gap-2">
      <CountdownRing
        remainingMs={delta}
        totalMs={RING_TOTAL_MS}
        size={22}
        strokeWidth={2.5}
        label={t("scheduler.tp.nextFireAria", { name: job.name })}
        // Hide the ring's own text so we can render the richer h/m/s label.
        className="[&>span]:hidden"
      />
      <span className="font-mono text-[11.5px] tabular-nums text-tp-ink-2">
        {t("scheduler.tp.inDelta", { delta: label })}
      </span>
    </span>
  );
}

// ─── Icon button primitive ───────────────────────────────────────────────

interface IconButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  label: string;
  testId?: string;
}

function IconButton({
  label,
  testId,
  disabled,
  className,
  children,
  title,
  ...rest
}: IconButtonProps) {
  return (
    <button
      type="button"
      aria-label={label}
      title={title ?? label}
      data-testid={testId}
      disabled={disabled}
      className={cn(
        "inline-flex h-7 w-7 items-center justify-center rounded-md",
        "text-tp-ink-3 transition-colors",
        "hover:bg-tp-glass-inner-hover hover:text-tp-ink",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
        "disabled:pointer-events-none disabled:opacity-40",
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}

// ─── helpers re-exported for test use ────────────────────────────────────

/** Test hook — the relative-time helper is a thin wrapper we want to
 *  exercise directly. */
export { formatRelative };
