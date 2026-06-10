"use client";

/**
 * `<HubSkillCard>` — one cell in the Browse Hub grid (W2.2).
 *
 * Visual contract mirrors the local `<SkillCard>`:
 *   row 1 — emoji badge + name + version chip (latest_version)
 *   row 2 — description, `line-clamp-2`
 *   footer — stars · downloads · "updated 3h ago" mono metas
 *
 * Click anywhere (or Enter/Space) → `onSelect(summary)` so the parent can
 * mount `<HubSkillDetailDrawer>`. Pure presentation: no network calls.
 */

import * as React from "react";
import { Download, Star } from "lucide-react";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";
import { useMotion } from "@/components/ui/motion-safe";
import type { HubSkillSummary } from "@/lib/api";

export interface HubSkillCardProps {
  summary: HubSkillSummary;
  onSelect: (summary: HubSkillSummary) => void;
  className?: string;
}

/** Compact, locale-agnostic "N ago" formatter. Mirrors the helper used by
 * `cost-footer.tsx`; duplicated here so we don't drag the sessions module
 * into this surface. */
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

export function HubSkillCard({
  summary,
  onSelect,
  className,
}: HubSkillCardProps): React.JSX.Element {
  const { reduced } = useMotion();
  // Read Date.now() once at render so the "ago" string stays stable in tests.
  const now = React.useMemo(() => Date.now(), []);
  const updated = relativeAgo(summary.updated_at, now);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onSelect(summary);
    }
  };

  return (
    <div
      data-testid={`hub-skill-card-${summary.slug}`}
      className={cn(
        "group block focus-visible:outline-none",
        !reduced &&
          "transition-transform duration-200 ease-sg-ease-out hover:-translate-y-0.5",
        className,
      )}
    >
      <GlassPanel
        variant="soft"
        role="button"
        tabIndex={0}
        aria-label={`Open ${summary.name} hub skill details`}
        onClick={() => onSelect(summary)}
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
            <span className="opacity-85">{summary.emoji ?? "✦"}</span>
          </div>
          <div className="min-w-0 flex-1">
            <h3 className="truncate text-[15px] font-medium leading-tight text-sg-ink">
              {summary.name}
            </h3>
            <div className="mt-1 flex items-center gap-1.5 font-mono text-[10.5px] text-sg-ink-4">
              <span className="truncate normal-case tracking-normal">
                {summary.slug}
              </span>
            </div>
          </div>
          <span
            className="inline-flex shrink-0 items-center rounded-full border border-sg-border bg-sg-inset px-2 py-[2px] font-mono text-[10.5px] text-sg-ink-3"
            title={summary.latest_version}
          >
            v{summary.latest_version}
          </span>
        </div>

        {/* Row 2 — description (clamped) */}
        <p className="line-clamp-2 text-[12.5px] leading-[1.5] text-sg-ink-2">
          {summary.description}
        </p>

        {/* Footer — stars · downloads · updated */}
        <div className="mt-auto flex flex-wrap items-center gap-x-2 gap-y-1 pt-1 font-mono text-[10.5px] text-sg-ink-4">
          <span
            className="inline-flex items-center gap-1"
            data-testid="hub-skill-stars"
          >
            <Star className="h-3 w-3" aria-hidden />
            {compactCount(summary.stars)}
          </span>
          <span aria-hidden>·</span>
          <span
            className="inline-flex items-center gap-1"
            data-testid="hub-skill-downloads"
          >
            <Download className="h-3 w-3" aria-hidden />
            {compactCount(summary.downloads)}
          </span>
          <span aria-hidden>·</span>
          <span data-testid="hub-skill-updated">{updated}</span>
        </div>
      </GlassPanel>
    </div>
  );
}

export default HubSkillCard;
