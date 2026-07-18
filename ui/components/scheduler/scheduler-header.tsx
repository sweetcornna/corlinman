"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { RefreshCw, Search } from "@/components/icons";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";
import { useCommandPalette } from "@/components/cmdk-palette";
import { formatCountdown } from "./scheduler-util";

/**
 * `<SchedulerHeader>` — hero strip for the Scheduler list page.
 *
 * Mirrors the Plugins/Dashboard prose-hero pattern: lead pill with summary
 * counts, prose paragraph describing the current state (including the
 * soonest upcoming job when known), and a CTA row (refresh + ⌘K hint).
 * Always rendered as GlassPanel `strong` so the amber gradient reads
 * against the aurora background.
 */

export interface SchedulerHeaderCounts {
  total: number;
  enabled: number;
  paused: number;
  errored: number;
}

export interface SchedulerHeaderProps {
  counts: SchedulerHeaderCounts | undefined;
  /** Relative-time string for "last refreshed X ago". */
  updatedLabel: string | undefined;
  /** Soonest-firing job — surfaced in the prose. */
  nextUp: { name: string; deltaMs: number } | null;
  /** Recently-errored count ("N errored since last hour" flavour). */
  recentlyErrored: number;
  offline: boolean;
  fetching: boolean;
  onRefresh: () => void;
}

export function SchedulerHeader({
  counts,
  updatedLabel,
  nextUp,
  recentlyErrored,
  offline,
  fetching,
  onRefresh,
}: SchedulerHeaderProps) {
  const { t } = useTranslation();
  const palette = useCommandPalette();

  const pillTotal = counts?.total ?? 0;
  const pillEnabled = counts?.enabled ?? 0;
  const pillPaused = counts?.paused ?? 0;
  const pillError = counts?.errored ?? 0;

  const nextUpLabel = nextUp ? formatCountdown(nextUp.deltaMs) : null;

  return (
    <GlassPanel variant="strong" as="section" className="relative overflow-hidden p-7">
      {/* Ambient amber glow — matches Plugins/Dashboard hero. */}
      <div
        aria-hidden
        className="pointer-events-none absolute bottom-[-90px] right-[-40px] h-[240px] w-[360px] rounded-full opacity-60 blur-3xl"
        style={{
          background: "radial-gradient(closest-side, var(--sg-accent-glow), transparent 70%)",
        }}
      />
      <div
        aria-hidden
        className="pointer-events-none absolute top-[-60px] left-[-40px] h-[180px] w-[260px] rounded-full opacity-40 blur-[50px]"
        style={{
          background:
            "radial-gradient(closest-side, color-mix(in oklch, var(--sg-accent-2) 35%, transparent), transparent 70%)",
        }}
      />

      <div className="relative flex min-w-0 flex-col gap-4">
        {/* Lead pill */}
        <div className="inline-flex w-fit items-center gap-2.5 rounded-full border border-sg-border bg-sg-inset-strong py-1 pl-2 pr-3 font-mono text-[11px] text-sg-ink-2">
          <span
            className={cn(
              "h-1.5 w-1.5 rounded-full",
              offline ? "bg-sg-err" : "bg-sg-accent sg-breathe-accent",
            )}
          />
          {offline
            ? t("scheduler.tp.offlineTitle")
            : t("scheduler.tp.leadPill", {
                enabled: pillEnabled,
                total: pillTotal,
                paused: pillPaused,
                errored: pillError,
              })}
        </div>

        <h1 className="text-balance font-sans text-[28px] font-semibold leading-[1.15] tracking-[-0.025em] text-sg-ink sm:text-[32px]">
          {t("scheduler.title")}
        </h1>

        <p className="max-w-[72ch] text-[14.5px] leading-[1.6] text-sg-ink-2">
          {offline ? (
            <>{t("scheduler.tp.proseOffline")}</>
          ) : (
            <>
              {t("scheduler.tp.proseLead", { total: pillTotal })}
              {nextUp && nextUpLabel ? (
                <>
                  {" "}
                  {t("scheduler.tp.proseNext", {
                    name: nextUp.name,
                    delta: nextUpLabel,
                  })}
                </>
              ) : (
                <> {t("scheduler.tp.proseNoNext")}</>
              )}
              {recentlyErrored > 0 ? (
                <>
                  {" "}
                  {t("scheduler.tp.proseErrored", { n: recentlyErrored })}
                </>
              ) : null}
              {updatedLabel ? ` ${t("scheduler.tp.proseUpdated", { when: updatedLabel })}` : ""}
            </>
          )}
        </p>

        <div className="mt-1 flex flex-wrap items-center gap-2.5">
          <button
            type="button"
            onClick={onRefresh}
            disabled={fetching}
            aria-label={t("scheduler.tp.refreshAria")}
            className={cn(
              "inline-flex items-center gap-2 rounded-lg border border-sg-accent/35 bg-sg-accent-soft px-3 py-2 text-[13px] font-medium text-sg-accent",
              "transition-colors hover:bg-[color-mix(in_oklch,var(--sg-accent)_22%,transparent)]",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/50",
              "disabled:cursor-not-allowed disabled:opacity-70",
            )}
          >
            <RefreshCw
              className={cn("h-3.5 w-3.5", fetching && "animate-spin")}
              aria-hidden
            />
            {t("common.refresh")}
          </button>

          <button
            type="button"
            onClick={() => palette.setOpen(true)}
            className="inline-flex items-center gap-2 rounded-lg border border-sg-border bg-sg-inset px-3 py-2 text-[13px] font-medium text-sg-ink-2 transition-colors hover:bg-sg-inset-hover hover:text-sg-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40"
          >
            <Search className="h-3.5 w-3.5" aria-hidden />
            {t("scheduler.tp.ctaPaletteHint")}
            <span className="ml-1 rounded bg-black/5 px-1.5 py-0.5 font-mono text-[10px] text-sg-ink-3 dark:bg-white/5">
              ⌘K
            </span>
          </button>
        </div>
      </div>
    </GlassPanel>
  );
}

export default SchedulerHeader;
