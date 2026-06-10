"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { RefreshCw, Search } from "lucide-react";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";
import { useCommandPalette } from "@/components/cmdk-palette";

/**
 * `<PluginsHeader>` — hero-strip for the Plugins list page.
 *
 * Mirrors the Dashboard prose-hero pattern: a lead pill with summary
 * counts, a prose paragraph describing the current state, and a row of
 * CTAs (refresh + ⌘K hint). Always rendered as GlassPanel `strong` so
 * the amber gradient reads against the aurora background.
 */

export interface PluginsHeaderCounts {
  total: number;
  loaded: number;
  sandboxed: number;
  errored: number;
}

export interface PluginsHeaderProps {
  counts: PluginsHeaderCounts | undefined;
  /** Relative-time string for "last refreshed X ago". */
  updatedLabel: string | undefined;
  /** true when the underlying query errored out or hasn't loaded yet. */
  offline: boolean;
  fetching: boolean;
  onRefresh: () => void;
}

export function PluginsHeader({
  counts,
  updatedLabel,
  offline,
  fetching,
  onRefresh,
}: PluginsHeaderProps) {
  const { t } = useTranslation();
  const palette = useCommandPalette();

  const pillLoaded = counts?.loaded ?? 0;
  const pillTotal = counts?.total ?? 0;
  const pillSandbox = counts?.sandboxed ?? 0;
  const pillError = counts?.errored ?? 0;

  return (
    <GlassPanel variant="strong" as="section" className="relative overflow-hidden p-7">
      {/* Ambient amber glow behind the copy — matches Dashboard hero. */}
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
            ? t("plugins.tp.offlineTitle")
            : t("plugins.tp.leadPill", {
                loaded: pillLoaded,
                total: pillTotal,
                sandboxed: pillSandbox,
                errored: pillError,
              })}
        </div>

        <h1 className="text-balance font-sans text-[28px] font-semibold leading-[1.15] tracking-[-0.025em] text-sg-ink sm:text-[32px]">
          {t("plugins.title")}
        </h1>

        <p className="max-w-[72ch] text-[14.5px] leading-[1.6] text-sg-ink-2">
          {offline ? (
            <>{t("plugins.tp.proseOffline")}</>
          ) : (
            <>
              {t("plugins.tp.proseLead", { total: pillTotal })}
              {t("plugins.tp.proseMiddle", {
                loaded: pillLoaded,
                sandboxed: pillSandbox,
              })}
              .{updatedLabel ? ` ${t("plugins.tp.proseUpdated", { when: updatedLabel })}` : ""}
            </>
          )}
        </p>

        <div className="mt-1 flex flex-wrap items-center gap-2.5">
          <button
            type="button"
            onClick={onRefresh}
            disabled={fetching}
            aria-label={t("plugins.refreshAria")}
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
            {t("plugins.refresh")}
          </button>

          <button
            type="button"
            onClick={() => palette.setOpen(true)}
            className="inline-flex items-center gap-2 rounded-lg border border-sg-border bg-sg-inset px-3 py-2 text-[13px] font-medium text-sg-ink-2 transition-colors hover:bg-sg-inset-hover hover:text-sg-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40"
          >
            <Search className="h-3.5 w-3.5" aria-hidden />
            {t("plugins.tp.ctaPaletteHint")}
            <span className="ml-1 rounded bg-black/5 px-1.5 py-0.5 font-mono text-[10px] text-sg-ink-3 dark:bg-white/5">
              ⌘K
            </span>
          </button>
        </div>
      </div>
    </GlassPanel>
  );
}

export default PluginsHeader;
