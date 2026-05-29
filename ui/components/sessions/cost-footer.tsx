"use client";

/**
 * `CostFooter` — Phase 4 Wave 2 / W2.3 sticky cost footer.
 *
 * Renders a glass-card row of warm-amber pills at the bottom of the session
 * detail scroll area summarising cumulative cost + turn timing for a single
 * session. Mirrors Claude Code's `cost-tracker.ts` sticky footer (`src/cost-tracker.ts:228-244`)
 * and hermes' `session_estimated_cost_usd` columns (`hermes_state.py:190-221`).
 *
 * Pills (left → right): total USD, turn count, average turn time, tool call
 * count, "last turn N ago". When `cost_status_breakdown.unknown > 0` we mark
 * the total pill with an info dot and a tooltip warning the figure is an
 * estimate only.
 *
 * Polling: every 15s via `setInterval` + a `visibilitychange` listener that
 * fires a refetch the moment the tab regains focus. Hidden entirely when the
 * session has zero recorded turns + zero cost (brand-new sessions shouldn't
 * show a noisy footer).
 *
 * The fetcher is the W2.1-owned `loadSessionCost` export from `@/lib/api`.
 * Until that lands we fall back to an inline `fetch` against the gateway —
 * remove `_loadCostInline` once `loadSessionCost` is exported.
 */

import * as React from "react";
import { useTranslation } from "react-i18next";
import { Coins, Clock, Repeat, Wrench, History, Info } from "lucide-react";

import { cn } from "@/lib/utils";
import { GATEWAY_BASE_URL } from "@/lib/api";

// TODO: depends on W2.1 loadSessionCost export — once `@/lib/api` exports
// `loadSessionCost(key)` switch to importing it and delete `_loadCostInline`.
async function _loadCostInline(key: string): Promise<SessionCostResponse> {
  const res = await fetch(
    `${GATEWAY_BASE_URL}/admin/sessions/${encodeURIComponent(key)}/cost`,
    { credentials: "include" },
  );
  if (!res.ok) throw new Error(`cost fetch failed: ${res.status}`);
  return res.json();
}

/* ------------------------------------------------------------------ */
/*                              Types                                  */
/* ------------------------------------------------------------------ */

export interface CostStatusBreakdown {
  estimated: number;
  billed: number;
  unknown: number;
}

export interface SessionCostResponse {
  session_key: string;
  turn_count: number;
  total_elapsed_ms: number;
  total_cost_usd: number;
  cost_status_breakdown: CostStatusBreakdown;
  total_tool_calls: number;
  last_turn_at_ms: number | null;
  avg_turn_ms: number;
  /**
   * Optional — if a future backend round adds a "last tool used" hint we
   * surface it on the list rows. Not present today.
   */
  last_tool_name?: string | null;
}

/* ------------------------------------------------------------------ */
/*                          Formatting helpers                         */
/* ------------------------------------------------------------------ */

/** Cost — show 4 decimal places under $1, 2 above so cents are readable. */
export function formatCost(usd: number): string {
  if (!Number.isFinite(usd) || usd <= 0) return "$0.00";
  if (usd < 1) return `$${usd.toFixed(4)}`;
  return `$${usd.toFixed(2)}`;
}

/** ms → "12.1s" / "3m 02s" / "1h 24m" */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return "0s";
  const totalSec = Math.round(ms / 100) / 10;
  if (totalSec < 60) return `${totalSec.toFixed(1)}s`;
  const totalSecInt = Math.round(ms / 1000);
  const m = Math.floor(totalSecInt / 60);
  const s = totalSecInt % 60;
  if (m < 60) return `${m}m ${s.toString().padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return `${h}h ${mm.toString().padStart(2, "0")}m`;
}

/** Relative "N ago" suitable for the sticky footer last-turn pill. */
export function formatRelativeAgo(ms: number | null, now: number): string {
  if (ms === null || !Number.isFinite(ms)) return "—";
  const diff = Math.max(0, now - ms);
  if (diff < 60_000) return `${Math.max(1, Math.round(diff / 1000))}s ago`;
  if (diff < 3_600_000) return `${Math.round(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.round(diff / 3_600_000)}h ago`;
  return `${Math.round(diff / 86_400_000)}d ago`;
}

/* ------------------------------------------------------------------ */
/*                            Component                                */
/* ------------------------------------------------------------------ */

const POLL_MS = 15_000;

export interface CostFooterProps {
  sessionKey: string;
  /** Override the fetcher in tests. */
  fetcher?: (key: string) => Promise<SessionCostResponse>;
}

export function CostFooter({ sessionKey, fetcher }: CostFooterProps) {
  const { t } = useTranslation();
  const fetch_ = fetcher ?? _loadCostInline;

  const [data, setData] = React.useState<SessionCostResponse | null>(null);
  const [error, setError] = React.useState<Error | null>(null);
  const [now, setNow] = React.useState<number>(() => Date.now());

  // Keep the "N ago" pill ticking without re-fetching.
  React.useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(id);
  }, []);

  const load = React.useCallback(async () => {
    try {
      const next = await fetch_(sessionKey);
      setData(next);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err : new Error(String(err)));
    }
  }, [fetch_, sessionKey]);

  React.useEffect(() => {
    void load();
    const id = window.setInterval(() => {
      void load();
    }, POLL_MS);
    const onVis = () => {
      if (document.visibilityState === "visible") void load();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [load]);

  // Hide the footer entirely until we have a meaningful number to show —
  // brand-new sessions shouldn't render a row of zeros.
  if (error && !data) return null;
  if (!data) return null;
  if (data.total_cost_usd === 0 && data.turn_count === 0) return null;

  const hasUnknown = data.cost_status_breakdown.unknown > 0;
  const totalLabel = formatCost(data.total_cost_usd);
  const totalPillTitle = hasUnknown
    ? `${t("sessions.cost.estimatedPrefix")} ${t("sessions.cost.unknownTooltip")}`
    : undefined;

  return (
    <div
      data-testid="cost-footer"
      className={cn(
        // sticky inside the scroll container — the page wrapper handles positioning.
        "sticky bottom-0 z-20 mt-4",
        "border-t border-tp-glass-edge bg-tp-glass/95 backdrop-blur",
        "px-4 py-3",
      )}
      role="status"
      aria-live="polite"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Pill
          icon={<Coins className="h-3.5 w-3.5" aria-hidden="true" />}
          label={t("sessions.cost.total")}
          value={
            <span className="inline-flex items-center gap-1">
              {hasUnknown ? `~${totalLabel}` : totalLabel}
              {hasUnknown ? (
                <Info
                  className="h-3 w-3 text-amber-300/80"
                  aria-label={t("sessions.cost.unknownTooltip")}
                />
              ) : null}
            </span>
          }
          title={totalPillTitle}
          tone="primary"
          testId="cost-footer-total"
        />
        <Pill
          icon={<Repeat className="h-3.5 w-3.5" aria-hidden="true" />}
          label={t("sessions.cost.totalTurns")}
          value={String(data.turn_count)}
          testId="cost-footer-turns"
        />
        <Pill
          icon={<Clock className="h-3.5 w-3.5" aria-hidden="true" />}
          label={t("sessions.cost.avgTurnTime")}
          value={formatDuration(data.avg_turn_ms)}
          testId="cost-footer-avg-turn"
        />
        <Pill
          icon={<Wrench className="h-3.5 w-3.5" aria-hidden="true" />}
          label={t("sessions.cost.totalTools")}
          value={String(data.total_tool_calls)}
          testId="cost-footer-tools"
        />
        <Pill
          icon={<History className="h-3.5 w-3.5" aria-hidden="true" />}
          label={t("sessions.cost.lastTurnAt")}
          value={formatRelativeAgo(data.last_turn_at_ms, now)}
          testId="cost-footer-last"
        />
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*                              Pill                                   */
/* ------------------------------------------------------------------ */

interface PillProps {
  icon: React.ReactNode;
  label: string;
  value: React.ReactNode;
  tone?: "primary" | "default";
  title?: string;
  testId?: string;
}

function Pill({ icon, label, value, tone = "default", title, testId }: PillProps) {
  return (
    <div
      data-testid={testId}
      title={title}
      className={cn(
        // Warm-amber glass pill — primary tone for the headline cost figure.
        "group inline-flex items-center gap-2 rounded-full",
        "border px-3 py-1 text-xs",
        "transition-all duration-150 ease-out",
        "hover:-translate-y-px hover:shadow-[0_4px_14px_-6px_rgba(245,158,11,0.4)]",
        tone === "primary"
          ? "border-amber-400/40 bg-amber-500/10 text-amber-100"
          : "border-tp-glass-edge bg-tp-glass-inner text-tp-ink-2",
      )}
    >
      <span
        className={cn(
          tone === "primary" ? "text-amber-300" : "text-tp-ink-3",
        )}
      >
        {icon}
      </span>
      <span className="text-tp-ink-3">{label}</span>
      <span
        className={cn(
          "font-mono",
          tone === "primary" ? "text-amber-50" : "text-tp-ink",
        )}
      >
        {value}
      </span>
    </div>
  );
}
