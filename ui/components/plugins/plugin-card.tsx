"use client";

import * as React from "react";
import Link from "next/link";
import { useTranslation } from "react-i18next";
import { ArrowUpRight } from "lucide-react";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";
import { useMotion } from "@/components/ui/motion-safe";
import type { PluginStatus, PluginSummary } from "@/lib/api";

/**
 * `<PluginCard>` — browse-grid cell for a single plugin summary.
 *
 * A GlassPanel (soft) styled link: click anywhere to open
 * `/plugins/detail?name=<p.name>`. Hover lifts the card 2px and escalates
 * to the `shadow-sg-primary` glow — disabled under
 * `prefers-reduced-motion: reduce`.
 *
 * Status dot + label use the Tidepool semantic tokens:
 *   - loaded   → `sg-ok`
 *   - disabled → `sg-ink-3` (the "sandboxed" visual category in this UI —
 *                 plugins that are declared async/sandboxed often read as
 *                 `disabled` until first load)
 *   - error    → `sg-err`
 */

export type PluginCardCategory = "loaded" | "sandboxed" | "errored" | "other";

export interface PluginCardProps {
  plugin: PluginSummary;
  /** Relative-time string for the "last touched" footer line. */
  lastTouchedLabel: string;
}

const statusToTone: Record<PluginStatus, { dot: string; text: string; label: string }> = {
  loaded: { dot: "bg-sg-ok", text: "text-sg-ok", label: "loaded" },
  disabled: { dot: "bg-sg-ink-4", text: "text-sg-ink-3", label: "disabled" },
  error: { dot: "bg-sg-err", text: "text-sg-err", label: "error" },
};

export function PluginCard({ plugin, lastTouchedLabel }: PluginCardProps) {
  const { t } = useTranslation();
  const { reduced } = useMotion();
  const tone = statusToTone[plugin.status];
  const toolCount = plugin.capabilities.length;

  return (
    <Link
      href={{ pathname: "/plugins/detail", query: { name: plugin.name } }}
      data-testid={`plugin-link-${plugin.name}`}
      aria-label={`${plugin.name} — ${tone.label}`}
      className={cn(
        "group block focus-visible:outline-none",
        // Hover lift & shadow escalation. Bail under reduce-motion.
        !reduced && "transition-transform duration-200 ease-sg-ease-out hover:-translate-y-0.5",
      )}
    >
      <GlassPanel
        variant="soft"
        className={cn(
          "flex h-full flex-col gap-3 p-4",
          "transition-[box-shadow,border-color] duration-200 ease-sg-ease-out",
          "group-hover:shadow-sg-primary group-focus-visible:shadow-sg-primary",
          "group-focus-visible:ring-2 group-focus-visible:ring-sg-accent/50",
        )}
      >
        {/* Row 1 — name + arrow */}
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5">
              <h3 className="truncate text-[15px] font-medium leading-tight text-sg-ink">
                {plugin.name}
              </h3>
              {plugin.error ? (
                <span
                  className="text-xs text-sg-err"
                  title={plugin.error}
                  aria-label={plugin.error}
                >
                  ⚠
                </span>
              ) : null}
            </div>
            <div className="mt-1 font-mono text-[10.5px] uppercase tracking-[0.08em] text-sg-ink-4">
              v{plugin.version} · {plugin.origin}
            </div>
          </div>
          <ArrowUpRight
            className={cn(
              "h-4 w-4 flex-shrink-0 text-sg-ink-4",
              "transition-[color,transform] duration-200",
              "group-hover:text-sg-accent",
              !reduced && "group-hover:translate-x-0.5 group-hover:-translate-y-0.5",
            )}
            aria-hidden
          />
        </div>

        {/* Row 2 — optional description */}
        {plugin.description ? (
          <p className="line-clamp-2 text-[12.5px] leading-[1.5] text-sg-ink-2">
            {plugin.description}
          </p>
        ) : null}

        {/* Row 3 — status + tool count + origin */}
        <div className="mt-auto flex flex-wrap items-center gap-2 pt-1">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-sg-border bg-sg-inset px-2 py-[3px] font-mono text-[10.5px] tracking-wide">
            <span className={cn("h-[5px] w-[5px] rounded-full", tone.dot)} aria-hidden />
            <span className={tone.text}>{tone.label}</span>
          </span>

          <span className="inline-flex items-center rounded-full border border-sg-border bg-sg-inset px-2 py-[3px] font-mono text-[10.5px] tracking-wide text-sg-ink-3">
            {toolCount > 0
              ? t("plugins.tp.cardToolCount", { count: toolCount })
              : t("plugins.tp.cardNoTools")}
          </span>

          <span className="ml-auto font-mono text-[10.5px] text-sg-ink-4">
            {lastTouchedLabel}
          </span>
        </div>
      </GlassPanel>
    </Link>
  );
}

export default PluginCard;
