"use client";

/**
 * `SessionCostCells` — Phase 4 Wave 2 / W2.3 list-row enrichment.
 *
 * Renders three lazy-fetched `<TableCell>` slots for the sessions list row:
 *   - Total cost (warm-amber chip, `~` prefix if any turns are estimates)
 *   - Average turn time
 *   - Last tool used (`—` when the cost endpoint doesn't surface it yet)
 *
 * The "last tool" hint is intentionally optional. The first round of the W2.3
 * cost endpoint only carries the aggregate breakdown — there's no
 * `last_tool_name` in the response. Until a follow-up adds it we render an
 * em-dash and leave a TODO; once the backend surfaces the field the rest of
 * the component already reads it.
 *
 * Caching: a module-level `Map<sessionKey, Promise<SessionCostResponse>>`
 * de-duplicates parallel mounts (e.g. the list is paginated and a row remounts).
 * We do NOT import a global SWR — the page already uses TanStack Query and we
 * don't want to introduce a second cache strategy for a low-priority cell.
 */

import * as React from "react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { GATEWAY_BASE_URL } from "@/lib/api";
import { TableCell } from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import {
  formatCost,
  formatDuration,
  type SessionCostResponse,
} from "@/components/sessions/cost-footer";

// TODO: depends on W2.1 loadSessionCost export — once `@/lib/api` exports
// `loadSessionCost(key)` switch to importing it and delete this inline fetch.
async function _loadCostInline(key: string): Promise<SessionCostResponse> {
  const res = await fetch(
    `${GATEWAY_BASE_URL}/admin/sessions/${encodeURIComponent(key)}/cost`,
    { credentials: "include" },
  );
  if (!res.ok) throw new Error(`cost fetch failed: ${res.status}`);
  return res.json();
}

/* ------------------------------------------------------------------ */
/*                       Module-level dedup cache                      */
/* ------------------------------------------------------------------ */

const CACHE = new Map<string, Promise<SessionCostResponse>>();

/** Test-only — wipe the cache between renders. */
export function __resetCostCache(): void {
  CACHE.clear();
}

function getCachedCost(
  key: string,
  fetcher: (k: string) => Promise<SessionCostResponse>,
): Promise<SessionCostResponse> {
  const hit = CACHE.get(key);
  if (hit) return hit;
  const p = fetcher(key).catch((err) => {
    // Drop the failed entry so retry next render doesn't get stuck.
    CACHE.delete(key);
    throw err;
  });
  CACHE.set(key, p);
  return p;
}

/* ------------------------------------------------------------------ */
/*                            Component                                */
/* ------------------------------------------------------------------ */

export interface SessionCostCellsProps {
  sessionKey: string;
  /** Optional — fall back when the parent already has a last-tool hint. */
  lastTool?: string | null;
  /** Override fetcher in tests. */
  fetcher?: (key: string) => Promise<SessionCostResponse>;
}

export function SessionCostCells({
  sessionKey,
  lastTool,
  fetcher,
}: SessionCostCellsProps) {
  const { t } = useTranslation();
  const fetch_ = fetcher ?? _loadCostInline;

  const [data, setData] = React.useState<SessionCostResponse | null>(null);
  const [errored, setErrored] = React.useState<boolean>(false);

  React.useEffect(() => {
    let cancelled = false;
    getCachedCost(sessionKey, fetch_)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch(() => {
        if (!cancelled) setErrored(true);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionKey, fetch_]);

  if (errored) {
    return (
      <>
        <TableCell className="font-mono text-xs text-tp-ink-3">—</TableCell>
        <TableCell className="font-mono text-xs text-tp-ink-3">—</TableCell>
        <TableCell className="font-mono text-xs text-tp-ink-3">—</TableCell>
      </>
    );
  }

  if (!data) {
    return (
      <>
        <TableCell>
          <Skeleton className="h-4 w-16" />
        </TableCell>
        <TableCell>
          <Skeleton className="h-4 w-12" />
        </TableCell>
        <TableCell>
          <Skeleton className="h-4 w-20" />
        </TableCell>
      </>
    );
  }

  const hasUnknown = data.cost_status_breakdown.unknown > 0;
  const totalStr = formatCost(data.total_cost_usd);
  // "Last tool" decision: prefer the list-API hint when present; otherwise
  // use the cost endpoint's `last_tool_name` if a future round adds it;
  // otherwise show "—". See file-header TODO.
  const lastToolValue =
    lastTool ?? data.last_tool_name ?? null;

  return (
    <>
      <TableCell
        className="font-mono text-xs"
        data-testid={`cost-cell-total-${sessionKey}`}
      >
        <span
          className={cn(
            "inline-flex items-center gap-1 rounded-full border px-2 py-0.5",
            data.total_cost_usd > 0
              ? "border-amber-400/40 bg-amber-500/10 text-amber-100"
              : "border-tp-glass-edge bg-tp-glass-inner text-tp-ink-3",
          )}
          title={
            hasUnknown
              ? `${t("sessions.cost.estimatedPrefix")} ${t("sessions.cost.unknownTooltip")}`
              : undefined
          }
        >
          {hasUnknown ? `~${totalStr}` : totalStr}
        </span>
      </TableCell>
      <TableCell
        className="font-mono text-xs text-tp-ink-2"
        data-testid={`cost-cell-avg-${sessionKey}`}
      >
        {data.turn_count > 0 ? formatDuration(data.avg_turn_ms) : "—"}
      </TableCell>
      <TableCell
        className="font-mono text-xs text-tp-ink-3"
        data-testid={`cost-cell-last-tool-${sessionKey}`}
      >
        {lastToolValue ?? "—"}
      </TableCell>
    </>
  );
}
