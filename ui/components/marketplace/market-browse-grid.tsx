"use client";

/**
 * `<MarketBrowseGrid>` — shared Browse-grid scaffold for the MCP and Plugin
 * market surfaces. Owns the debounced client-side search filter, the offline
 * banner + retry, the skeleton, and the empty states. The parent supplies the
 * rows + offline flag (one `useQuery`) and a `renderCard` callback so MCP and
 * Plugins can pass their own card (transport chip on/off, detail wiring).
 *
 * The market endpoints return the full page at once (cursor pagination is
 * available but the first page is plenty for the admin browse hub), so the
 * search is a pure client-side filter over name/slug/description/tags — no
 * extra round-trips.
 */

import * as React from "react";
import { useTranslation } from "react-i18next";
import { RefreshCw, Search, WifiOff } from "lucide-react";

import { Button } from "@/components/ui/button";
import { GlassPanel } from "@/components/ui/glass-panel";
import type { McpMarketItem } from "@/lib/api";

export interface MarketBrowseGridProps {
  rows: McpMarketItem[];
  offline: boolean;
  pending: boolean;
  onRetry: () => void;
  renderCard: (item: McpMarketItem) => React.ReactNode;
  testId?: string;
}

function matches(item: McpMarketItem, q: string): boolean {
  if (!q) return true;
  return (
    item.name.toLowerCase().includes(q) ||
    item.slug.toLowerCase().includes(q) ||
    item.description.toLowerCase().includes(q) ||
    item.tags.some((tag) => tag.toLowerCase().includes(q))
  );
}

export function MarketBrowseGrid({
  rows,
  offline,
  pending,
  onRetry,
  renderCard,
  testId = "market-browse",
}: MarketBrowseGridProps): React.JSX.Element {
  const { t } = useTranslation();
  const [search, setSearch] = React.useState("");

  const filtered = React.useMemo(() => {
    const q = search.trim().toLowerCase();
    return rows.filter((row) => matches(row, q));
  }, [rows, search]);

  const hasSearch = search.trim().length > 0;

  return (
    <section className="flex flex-col gap-4" data-testid={testId}>
      {/* Search */}
      <div className="flex flex-wrap items-center gap-3">
        <label className="relative flex min-w-[220px] flex-1 items-center sm:max-w-[360px]">
          <Search
            className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-tp-ink-4"
            aria-hidden
          />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("marketplace.common.searchPlaceholder")}
            aria-label={t("marketplace.common.searchPlaceholder")}
            data-testid={`${testId}-search`}
            className="h-9 w-full rounded-lg border border-tp-glass-edge bg-tp-glass-inner pl-8 pr-3 text-[13px] text-tp-ink placeholder:text-tp-ink-4 transition-colors hover:bg-tp-glass-inner-hover focus:outline-none focus:ring-2 focus:ring-tp-amber/40"
          />
        </label>
      </div>

      {/* Offline banner */}
      {offline ? (
        <GlassPanel
          variant="soft"
          className="flex flex-wrap items-center justify-between gap-3 p-4"
          data-testid={`${testId}-offline-banner`}
        >
          <div className="flex items-center gap-3">
            <span className="inline-flex h-8 w-8 items-center justify-center rounded-full border border-red-500/30 bg-red-500/10 text-red-600">
              <WifiOff className="h-4 w-4" aria-hidden />
            </span>
            <div className="flex flex-col">
              <span className="text-[13px] font-medium text-tp-ink">
                {t("marketplace.common.offlineTitle")}
              </span>
              <span className="text-[12px] text-tp-ink-3">
                {t("marketplace.common.offlineHint")}
              </span>
            </div>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={onRetry}
            data-testid={`${testId}-offline-retry`}
          >
            <RefreshCw className="h-3.5 w-3.5" aria-hidden />
            {t("marketplace.common.retry")}
          </Button>
        </GlassPanel>
      ) : null}

      {/* Grid / skeleton / empty */}
      {pending ? (
        <GridSkeleton testId={testId} />
      ) : !offline && filtered.length === 0 ? (
        <GlassPanel
          variant="subtle"
          className="flex flex-col items-center gap-2 p-8 text-center"
          data-testid={`${testId}-empty`}
        >
          <div className="text-[14px] font-medium text-tp-ink">
            {hasSearch
              ? t("marketplace.common.emptySearchTitle")
              : t("marketplace.common.emptyFeaturedTitle")}
          </div>
          <p className="text-[13px] text-tp-ink-3">
            {hasSearch
              ? t("marketplace.common.emptySearchHint")
              : t("marketplace.common.emptyFeaturedHint")}
          </p>
        </GlassPanel>
      ) : filtered.length > 0 ? (
        <section
          aria-label={t("marketplace.common.gridLabel")}
          className="grid gap-3 grid-cols-[repeat(auto-fill,minmax(280px,1fr))]"
          data-testid={`${testId}-grid`}
        >
          {filtered.map((item) => (
            <React.Fragment key={item.slug}>{renderCard(item)}</React.Fragment>
          ))}
        </section>
      ) : null}
    </section>
  );
}

function GridSkeleton({ testId }: { testId: string }) {
  return (
    <section
      aria-hidden
      className="grid gap-3 grid-cols-[repeat(auto-fill,minmax(280px,1fr))]"
      data-testid={`${testId}-skeleton`}
    >
      {Array.from({ length: 6 }).map((_, i) => (
        <GlassPanel
          key={i}
          variant="soft"
          className="flex h-[148px] flex-col gap-3 p-4"
        >
          <div className="flex items-center gap-2.5">
            <div className="h-9 w-9 rounded-full bg-tp-glass-inner-strong" />
            <div className="flex-1 space-y-1.5">
              <div className="h-3.5 w-2/3 rounded bg-tp-glass-inner-strong" />
              <div className="h-2.5 w-1/3 rounded bg-tp-glass-inner" />
            </div>
          </div>
          <div className="h-3 w-5/6 rounded bg-tp-glass-inner" />
          <div className="mt-auto flex gap-1.5">
            <div className="h-4 w-16 rounded bg-tp-glass-inner" />
            <div className="h-4 w-20 rounded bg-tp-glass-inner" />
            <div className="h-4 w-12 rounded bg-tp-glass-inner" />
          </div>
        </GlassPanel>
      ))}
    </section>
  );
}

export default MarketBrowseGrid;
