"use client";

/**
 * Past-turns pill row (W2.3 — UI fix wave).
 *
 * Renders a horizontally-scrollable row of up to ~10 pills at the top of
 * `/admin/sessions/[key]`. Each pill is a link to the per-turn drill-down
 * page (`/admin/sessions/{key}/turns/{turn_id}`) and shows the short turn
 * id, elapsed time, and a small status glyph.
 *
 * Data source: `GET /admin/sessions/{key}/turns?limit=10`. Cursor
 * pagination is wired through a "Load more" pill at the tail — clicking
 * it walks the next page in-place (additive, not replace), so the row
 * grows as the operator scrolls back through history.
 *
 * No row is rendered at all when the session has zero turns; that's the
 * empty state and dropping the chrome avoids visual noise above the live
 * timeline.
 *
 * The 503 `observability_disabled` envelope from the gateway falls through
 * here as a non-fatal silent failure — the timeline below still renders
 * fine, and the operator just doesn't get the past-turn shortcut.
 */

import * as React from "react";
import Link from "next/link";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Loader2 } from "lucide-react";

import { cn } from "@/lib/utils";
import {
  listSessionTurns,
  type SessionTurnRow,
  type SessionTurnsResponse,
} from "@/lib/api";

const PILL_PAGE_SIZE = 10;

export interface PastTurnsPillsProps {
  sessionKey: string;
  /** Highlight this turn id (matches the route's `turn_id` param when the
   * detail page is mounted under a per-turn drill-down). */
  currentTurnId?: string;
  className?: string;
}

/** Format milliseconds as a short human-readable string ("1.2s", "340ms"). */
function formatElapsed(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const totalSec = Math.round(ms / 1000);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return `${m}m${s.toString().padStart(2, "0")}s`;
}

/** Truncate a turn id to the first 8 chars so the pill stays compact. */
function shortTurnId(id: string): string {
  return id.length > 8 ? id.slice(0, 8) : id;
}

function StatusGlyph({ status }: { status: string }) {
  const s = (status ?? "").toLowerCase();
  if (s === "completed" || s === "complete") {
    return (
      <CheckCircle2
        className="h-3 w-3 text-sg-ok"
        aria-label={status}
      />
    );
  }
  if (s === "errored" || s === "failed" || s === "error") {
    return (
      <AlertTriangle
        className="h-3 w-3 text-sg-err"
        aria-label={status}
      />
    );
  }
  if (s === "running" || s === "in_progress" || s === "cancelling") {
    return (
      <Loader2
        className="h-3 w-3 animate-spin text-sg-accent"
        aria-label={status}
      />
    );
  }
  // Unknown / pending → tiny dot.
  return (
    <span
      aria-label={status || "unknown"}
      className="inline-block h-1.5 w-1.5 rounded-full bg-sg-ink-4"
    />
  );
}

function SkeletonPill() {
  return (
    <div
      aria-hidden="true"
      className="inline-flex h-7 w-32 shrink-0 animate-pulse rounded-full border border-sg-border bg-sg-inset"
    />
  );
}

interface PillProps {
  sessionKey: string;
  turn: SessionTurnRow;
  active: boolean;
}

function TurnPill({ sessionKey, turn, active }: PillProps) {
  const href = `/admin/sessions/turn?key=${encodeURIComponent(sessionKey)}&turn=${encodeURIComponent(
    turn.turn_id,
  )}`;
  return (
    <Link
      href={href}
      data-testid={`past-turn-pill-${turn.turn_id}`}
      data-active={active || undefined}
      className={cn(
        "inline-flex h-7 shrink-0 items-center gap-1.5 rounded-full border px-3 font-mono text-[11px] transition-colors",
        "border-sg-border bg-sg-card-grad text-sg-ink-3 hover:bg-sg-accent-soft hover:text-sg-ink",
        active &&
          "border-sg-accent/40 bg-sg-accent-soft text-sg-ink shadow-[inset_0_0_0_1px_var(--sg-accent-glow)]",
      )}
      title={turn.user_text_preview ?? undefined}
    >
      <StatusGlyph status={turn.status} />
      <span>Turn {shortTurnId(turn.turn_id)}</span>
      <span className="text-sg-ink-4">· {formatElapsed(turn.elapsed_ms)}</span>
    </Link>
  );
}

export function PastTurnsPills({
  sessionKey,
  currentTurnId,
  className,
}: PastTurnsPillsProps) {
  const { t } = useTranslation();

  // Pages are accumulated client-side so "Load more" appends rather than
  // swaps. Each page is the verbatim response from listSessionTurns.
  const [pages, setPages] = React.useState<SessionTurnsResponse[]>([]);
  const [loadingMore, setLoadingMore] = React.useState(false);
  const [loadMoreError, setLoadMoreError] = React.useState<string | null>(null);

  const firstPage = useQuery<SessionTurnsResponse>({
    queryKey: ["admin", "sessions", sessionKey, "turns", "first"],
    queryFn: () => listSessionTurns(sessionKey, { limit: PILL_PAGE_SIZE }),
    enabled: !!sessionKey,
    // Silent failure on 503 / 404 — the rest of the page still works.
    retry: false,
    staleTime: 30_000,
  });

  // Reset paginated state whenever the session key changes (we navigated
  // to a different session).
  React.useEffect(() => {
    setPages([]);
    setLoadingMore(false);
    setLoadMoreError(null);
  }, [sessionKey]);

  // Seed the local pages list from the first-page query.
  React.useEffect(() => {
    if (!firstPage.data) return;
    setPages([firstPage.data]);
  }, [firstPage.data]);

  const lastPage = pages[pages.length - 1];
  const nextCursor = lastPage?.next_cursor ?? null;
  const turns = React.useMemo<SessionTurnRow[]>(
    () => pages.flatMap((p) => p.turns ?? []),
    [pages],
  );

  async function handleLoadMore() {
    if (!nextCursor || loadingMore) return;
    setLoadingMore(true);
    setLoadMoreError(null);
    try {
      const next = await listSessionTurns(sessionKey, {
        limit: PILL_PAGE_SIZE,
        before_turn_id: nextCursor,
      });
      setPages((prev) => [...prev, next]);
    } catch (err) {
      setLoadMoreError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoadingMore(false);
    }
  }

  // Loading state — 3 skeleton pills, sized like the real thing.
  if (firstPage.isPending) {
    return (
      <div
        className={cn("flex gap-2 overflow-x-auto", className)}
        data-testid="past-turns-pills-loading"
      >
        <SkeletonPill />
        <SkeletonPill />
        <SkeletonPill />
      </div>
    );
  }

  // 503 / 404 / network — render nothing rather than a scary error banner.
  // The live timeline below still works without this navigator.
  if (firstPage.isError) return null;

  // Empty state — no chrome at all.
  if (turns.length === 0) return null;

  return (
    <div
      className={cn(
        "flex items-center gap-2 overflow-x-auto pb-1",
        className,
      )}
      data-testid="past-turns-pills"
      role="navigation"
      aria-label={t("sessions.pastTurns.label")}
    >
      {turns.map((turn) => (
        <TurnPill
          key={turn.turn_id}
          sessionKey={sessionKey}
          turn={turn}
          active={!!currentTurnId && turn.turn_id === currentTurnId}
        />
      ))}
      {nextCursor ? (
        <button
          type="button"
          data-testid="past-turns-pills-load-more"
          onClick={handleLoadMore}
          disabled={loadingMore}
          className={cn(
            "inline-flex h-7 shrink-0 items-center gap-1.5 rounded-full border px-3 text-[11px] transition-colors",
            "border-dashed border-sg-border bg-transparent text-sg-ink-3 hover:bg-sg-accent-soft hover:text-sg-ink",
            "disabled:cursor-not-allowed disabled:opacity-60",
          )}
          aria-label={t("sessions.pastTurns.loadMore")}
        >
          {loadingMore ? (
            <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
          ) : null}
          {t("sessions.pastTurns.loadMore")}
        </button>
      ) : null}
      {loadMoreError ? (
        <span
          className="ml-2 truncate text-[11px] text-sg-err"
          role="alert"
        >
          {loadMoreError}
        </span>
      ) : null}
    </div>
  );
}

export default PastTurnsPills;
