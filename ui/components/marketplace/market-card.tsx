"use client";

/**
 * `<MarketCard>` — one cell in a Marketplace browse grid (MCP or Plugins).
 *
 * Mirrors `<HubSkillCard>` from the Skills hub so the grid rhythm reads the
 * same across all three Marketplace surfaces:
 *   row 1 — emoji badge + name + version chip
 *   row 2 — description, `line-clamp-2`
 *   footer — stars · downloads · "updated 3h ago" + optional transport chip
 *
 * Click anywhere (or Enter/Space) → `onSelect(item)` so the parent can mount
 * the relevant detail drawer. Pure presentation: no network calls.
 */

import * as React from "react";
import { Download, Star } from "lucide-react";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";
import { useMotion } from "@/components/ui/motion-safe";
import type { McpMarketItem } from "@/lib/api";

export interface MarketCardProps {
  item: McpMarketItem;
  onSelect: (item: McpMarketItem) => void;
  /** When true, render the MCP transport chip in the footer. */
  showTransport?: boolean;
  className?: string;
}

/** Compact "N ago" formatter. Mirrors the Skills hub helper. */
function relativeAgo(iso: string, now: number): string {
  const ts = Date.parse(iso);
  if (Number.isNaN(ts)) return "—";
  const diff = Math.max(0, now - ts);
  if (diff < 60_000) return `${Math.max(1, Math.round(diff / 1000))}s ago`;
  if (diff < 3_600_000) return `${Math.round(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.round(diff / 3_600_000)}h ago`;
  return `${Math.round(diff / 86_400_000)}d ago`;
}

/** Compact `1.2k` formatter for star/download counts. */
function compactCount(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "0";
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

export function MarketCard({
  item,
  onSelect,
  showTransport = false,
  className,
}: MarketCardProps): React.JSX.Element {
  const { reduced } = useMotion();
  const now = React.useMemo(() => Date.now(), []);
  const updated = relativeAgo(item.updated_at, now);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onSelect(item);
    }
  };

  return (
    <div
      data-testid={`market-card-${item.slug}`}
      className={cn(
        "group block focus-visible:outline-none",
        !reduced && "lg-gel hover:-translate-y-0.5",
        className,
      )}
    >
      <GlassPanel
        variant="soft"
        lively
        role="button"
        tabIndex={0}
        aria-label={`Open ${item.name} details`}
        onClick={() => onSelect(item)}
        onKeyDown={handleKeyDown}
        className={cn(
          "flex h-full cursor-pointer flex-col gap-3 p-4",
          "transition-[box-shadow,border-color] duration-200 ease-sg-ease-out",
          "group-hover:shadow-sg-primary",
          "focus-visible:shadow-sg-primary focus-visible:ring-2 focus-visible:ring-sg-accent/50",
        )}
      >
        {/* Row 1 — emoji + name + latest_version chip */}
        <div className="flex items-start gap-2.5">
          <div
            aria-hidden
            className={cn(
              "flex h-9 w-9 shrink-0 items-center justify-center rounded-full",
              "border border-sg-accent/25 bg-sg-accent-soft text-[17px] leading-none",
            )}
          >
            <span className="opacity-85">{item.emoji ?? "✦"}</span>
          </div>
          <div className="min-w-0 flex-1">
            <h3 className="truncate text-[15px] font-medium leading-tight text-sg-ink">
              {item.name}
            </h3>
            <div className="mt-1 flex items-center gap-1.5 font-mono text-[10.5px] text-sg-ink-4">
              <span className="truncate normal-case tracking-normal">
                {item.slug}
              </span>
            </div>
          </div>
          <span
            className="inline-flex shrink-0 items-center rounded-full border border-sg-border bg-sg-inset px-2 py-[2px] font-mono text-[10.5px] text-sg-ink-3"
            title={item.latest_version}
          >
            v{item.latest_version}
          </span>
        </div>

        {/* Row 2 — description (clamped) */}
        <p className="line-clamp-2 text-[12.5px] leading-[1.5] text-sg-ink-2">
          {item.description}
        </p>

        {/* Footer — stars · downloads · updated + transport */}
        <div className="mt-auto flex flex-wrap items-center gap-x-2 gap-y-1 pt-1 font-mono text-[10.5px] text-sg-ink-4">
          <span
            className="inline-flex items-center gap-1"
            data-testid="market-card-stars"
          >
            <Star className="h-3 w-3" aria-hidden />
            {compactCount(item.stars)}
          </span>
          <span aria-hidden>·</span>
          <span
            className="inline-flex items-center gap-1"
            data-testid="market-card-downloads"
          >
            <Download className="h-3 w-3" aria-hidden />
            {compactCount(item.downloads)}
          </span>
          <span aria-hidden>·</span>
          <span data-testid="market-card-updated">{updated}</span>
          {showTransport && item.transport ? (
            <>
              <span aria-hidden>·</span>
              <span
                className="inline-flex items-center rounded-full border border-sg-border bg-sg-inset px-1.5 py-[1px] normal-case text-sg-ink-3"
                data-testid="market-card-transport"
              >
                {item.transport}
              </span>
            </>
          ) : null}
        </div>
      </GlassPanel>
    </div>
  );
}

export default MarketCard;
