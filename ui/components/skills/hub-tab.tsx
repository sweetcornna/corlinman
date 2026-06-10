"use client";

/**
 * `<HubTab>` — Browse Hub tab of the `/admin/skills` surface (W2.2).
 *
 * Layout:
 *   ┌─ debounced search input  · sort dropdown ─┐
 *   │ offline banner (when response.offline)    │
 *   │ grid of <HubSkillCard>                    │
 *   └───────────────────────────────────────────┘
 *
 * Behaviour:
 *   - Empty search → list `getHubFeatured(sort)`; non-empty → `searchHubSkills(q)`.
 *   - Search is debounced 300ms via useEffect + setTimeout cleanup.
 *   - `response.offline === true` → render banner + Retry button (no toast).
 *   - Clicking a card mounts `<HubSkillDetailDrawer>` (which owns Install).
 */

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { motion } from "framer-motion";
import { RefreshCw, Search, WifiOff } from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { GlassPanel } from "@/components/ui/glass-panel";
import { useMotionVariants } from "@/lib/motion";
import {
  listHubFeatured,
  searchHubSkills,
  type HubListResponse,
  type HubSearchResponse,
  type HubSkillSummary,
  type HubSortKey,
} from "@/lib/api";
import { HubSkillCard } from "./hub-skill-card";
import { HubSkillDetailDrawer } from "./hub-skill-detail-drawer";

const SORT_OPTIONS: HubSortKey[] = ["trending", "downloads", "stars", "updated"];

export function HubTab(): React.JSX.Element {
  const { t } = useTranslation();
  const variants = useMotionVariants();
  const [rawQuery, setRawQuery] = React.useState("");
  const [debouncedQuery, setDebouncedQuery] = React.useState("");
  const [sort, setSort] = React.useState<HubSortKey>("trending");
  const [selected, setSelected] = React.useState<HubSkillSummary | null>(null);

  // Debounce the search input by 300ms. Empty string takes effect
  // immediately so the user can blank out and see featured again.
  React.useEffect(() => {
    if (rawQuery.trim() === "") {
      setDebouncedQuery("");
      return;
    }
    const handle = window.setTimeout(() => {
      setDebouncedQuery(rawQuery.trim());
    }, 300);
    return () => window.clearTimeout(handle);
  }, [rawQuery]);

  // One query keyed by [mode, sort, debouncedQuery]. The mode determines
  // which endpoint to hit; the shape comes back as either Search or List.
  type Mode = "search" | "featured";
  const mode: Mode = debouncedQuery.length > 0 ? "search" : "featured";
  const query = useQuery<HubSearchResponse | HubListResponse>({
    queryKey: ["hub-skills", mode, sort, debouncedQuery],
    queryFn: () =>
      mode === "search"
        ? searchHubSkills(debouncedQuery)
        : listHubFeatured(sort),
    retry: false,
  });

  const rows = query.data?.rows ?? [];
  const offline = query.data?.offline === true;

  const handleRetry = React.useCallback(() => {
    void query.refetch();
  }, [query]);

  return (
    <section className="flex flex-col gap-4" data-testid="skills-hub-tab">
      {/* Search + sort */}
      <div className="flex flex-wrap items-center gap-3">
        <label className="relative flex min-w-[220px] flex-1 items-center sm:max-w-[360px]">
          <Search
            className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-sg-ink-4"
            aria-hidden
          />
          <input
            type="text"
            value={rawQuery}
            onChange={(e) => setRawQuery(e.target.value)}
            placeholder={t("skills.hub.search.placeholder")}
            aria-label={t("skills.hub.search.placeholder")}
            data-testid="hub-search-input"
            className="h-9 w-full rounded-lg border border-sg-border bg-sg-inset pl-8 pr-3 text-[13px] text-sg-ink placeholder:text-sg-ink-4 transition-colors hover:bg-sg-inset-hover focus:outline-none focus:ring-2 focus:ring-sg-accent/40"
          />
        </label>
        <div className="flex items-center gap-2">
          <label
            htmlFor="hub-sort-select"
            className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-sg-ink-4"
          >
            {t("skills.hub.sort.label")}
          </label>
          <select
            id="hub-sort-select"
            data-testid="hub-sort-select"
            value={sort}
            onChange={(e) => setSort(e.target.value as HubSortKey)}
            disabled={mode === "search"}
            className={cn(
              "h-9 rounded-lg border border-sg-border bg-sg-inset px-2.5 text-[13px] text-sg-ink",
              "hover:bg-sg-inset-hover focus:outline-none focus:ring-2 focus:ring-sg-accent/40",
              "disabled:cursor-not-allowed disabled:opacity-60",
            )}
          >
            {SORT_OPTIONS.map((opt) => (
              <option key={opt} value={opt}>
                {t(`skills.hub.sort.${opt}`)}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Offline banner */}
      {offline ? (
        <GlassPanel
          variant="soft"
          className="flex flex-wrap items-center justify-between gap-3 p-4"
          data-testid="hub-offline-banner"
        >
          <div className="flex items-center gap-3">
            <span className="inline-flex h-8 w-8 items-center justify-center rounded-full border border-sg-err/30 bg-sg-err-soft text-sg-err">
              <WifiOff className="h-4 w-4" aria-hidden />
            </span>
            <div className="flex flex-col">
              <span className="text-[13px] font-medium text-sg-ink">
                {t("skills.hub.offline.title")}
              </span>
              <span className="text-[12px] text-sg-ink-3">
                {t("skills.hub.offline.hint")}
              </span>
            </div>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={handleRetry}
            data-testid="hub-offline-retry"
          >
            <RefreshCw className="h-3.5 w-3.5" aria-hidden />
            {t("skills.hub.offline.retry")}
          </Button>
        </GlassPanel>
      ) : null}

      {/* Grid / skeleton / empty */}
      {query.isPending ? (
        <HubGridSkeleton />
      ) : !offline && rows.length === 0 ? (
        <GlassPanel
          variant="subtle"
          className="flex flex-col items-center gap-2 p-8 text-center"
          data-testid="hub-empty"
        >
          <div className="text-[14px] font-medium text-sg-ink">
            {mode === "search"
              ? t("skills.hub.empty.searchTitle")
              : t("skills.hub.empty.featuredTitle")}
          </div>
          <p className="text-[13px] text-sg-ink-3">
            {mode === "search"
              ? t("skills.hub.empty.searchHint")
              : t("skills.hub.empty.featuredHint")}
          </p>
        </GlassPanel>
      ) : rows.length > 0 ? (
        <motion.section
          aria-label={t("skills.hub.gridLabel")}
          className="grid gap-3 grid-cols-[repeat(auto-fill,minmax(280px,1fr))]"
          data-testid="hub-grid"
          variants={variants.liquidStagger}
          initial="hidden"
          animate="visible"
        >
          {rows.map((row) => (
            <motion.div key={row.slug} variants={variants.liquidRise}>
              <HubSkillCard summary={row} onSelect={setSelected} />
            </motion.div>
          ))}
        </motion.section>
      ) : null}

      <HubSkillDetailDrawer
        summary={selected}
        open={selected !== null}
        onOpenChange={(next) => {
          if (!next) setSelected(null);
        }}
      />
    </section>
  );
}

function HubGridSkeleton() {
  return (
    <section
      aria-hidden
      className="grid gap-3 grid-cols-[repeat(auto-fill,minmax(280px,1fr))]"
      data-testid="hub-grid-skeleton"
    >
      {Array.from({ length: 6 }).map((_, i) => (
        <GlassPanel
          key={i}
          variant="soft"
          className="flex h-[148px] flex-col gap-3 p-4"
        >
          <div className="flex items-center gap-2.5">
            <div className="h-9 w-9 rounded-full bg-sg-inset-strong" />
            <div className="flex-1 space-y-1.5">
              <div className="h-3.5 w-2/3 rounded bg-sg-inset-strong" />
              <div className="h-2.5 w-1/3 rounded bg-sg-inset" />
            </div>
          </div>
          <div className="h-3 w-5/6 rounded bg-sg-inset" />
          <div className="mt-auto flex gap-1.5">
            <div className="h-4 w-16 rounded bg-sg-inset" />
            <div className="h-4 w-20 rounded bg-sg-inset" />
            <div className="h-4 w-12 rounded bg-sg-inset" />
          </div>
        </GlassPanel>
      ))}
    </section>
  );
}

export default HubTab;
