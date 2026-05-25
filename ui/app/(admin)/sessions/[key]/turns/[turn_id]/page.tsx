"use client";

/**
 * `/admin/sessions/[key]/turns/[turn_id]` — Phase 4 W2.2 per-turn drill-down.
 *
 * Static replay of a single turn re-rendered through the *same* timeline
 * components the live view (`/admin/sessions/[key]`) uses. This mirrors
 * Claude Code's transcript-JSONL pattern: history files are replayed
 * through the live renderer so a finished turn looks pixel-identical to
 * the one that's still streaming.
 *
 * Flow:
 *   1. Mount → paginate `loadTurnEvents` until `next_cursor === null` so
 *      we have every event for the turn locally.
 *   2. Stash the full event batch in state + extract a few flat fields
 *      (`userInput` from `TurnStart`, `finishReason` from `TurnComplete`)
 *      for the summary card.
 *   3. Render: own `<TimelineProvider>` → `<TurnSummaryCard>` +
 *      `<EventTimelineBody mode="replay" turnIdFilter={turn_id} ...>`.
 *      The body skips opening the SSE and instead dispatches our seed
 *      batch through the existing reducer; the summary card reads the
 *      resulting `Turn` from the same store.
 *
 * Loading/error states are local to this page — there's no global
 * loading shell at the (admin) layout level.
 */

import * as React from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useTranslation } from "react-i18next";
import { AlertTriangle, ChevronLeft, ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { loadTurnEvents, type LiveEvent } from "@/lib/api";
import { TimelineProvider } from "@/lib/sessions/store";
import { EventTimelineBody } from "@/components/sessions/event-timeline";
import { TurnSummaryCard } from "@/components/sessions/turn-summary-card";

const REPLAY_PAGE_SIZE = 5000;

/** Pull `user_text` out of the first `TurnStart` event, if present. */
function extractUserInput(events: LiveEvent[]): string | null {
  for (const ev of events) {
    if (ev.event_type === "TurnStart") {
      const p = ev.payload as { user_text?: unknown } | null;
      if (p && typeof p.user_text === "string") return p.user_text;
      return null;
    }
  }
  return null;
}

/** Pull `finish_reason` out of the `TurnComplete` event (last write wins). */
function extractFinishReason(events: LiveEvent[]): string | null {
  let last: string | null = null;
  for (const ev of events) {
    if (ev.event_type === "TurnComplete") {
      const p = ev.payload as { finish_reason?: unknown } | null;
      if (p && typeof p.finish_reason === "string") last = p.finish_reason;
    }
  }
  return last;
}

interface LoadState {
  status: "loading" | "ready" | "error";
  events: LiveEvent[];
  error?: Error;
}

export default function TurnDetailPage() {
  const params = useParams<{ key: string; turn_id: string }>();
  const rawKey = Array.isArray(params?.key) ? params.key[0] : params?.key;
  const rawTurnId = Array.isArray(params?.turn_id)
    ? params.turn_id[0]
    : params?.turn_id;
  const sessionKey = rawKey ? decodeURIComponent(rawKey) : "";
  const turnId = rawTurnId ? decodeURIComponent(rawTurnId) : "";

  const [state, setState] = React.useState<LoadState>({
    status: "loading",
    events: [],
  });
  const [attempt, setAttempt] = React.useState(0);

  React.useEffect(() => {
    if (!sessionKey || !turnId) return;
    let cancelled = false;
    const controller = new AbortController();

    (async () => {
      setState({ status: "loading", events: [] });
      try {
        const all: LiveEvent[] = [];
        let cursor: number | undefined = undefined;
        // Loop the cursor until exhausted — a long turn can easily blow
        // past a single page.
        // eslint-disable-next-line no-constant-condition
        while (true) {
          const page = await loadTurnEvents(sessionKey, turnId, {
            afterSequence: cursor,
            limit: REPLAY_PAGE_SIZE,
            signal: controller.signal,
          });
          if (cancelled) return;
          all.push(...page.events);
          if (page.next_cursor == null) break;
          // Guard against a buggy server returning the same cursor.
          if (cursor !== undefined && page.next_cursor <= cursor) break;
          cursor = page.next_cursor;
        }
        if (!cancelled) {
          setState({ status: "ready", events: all });
        }
      } catch (err) {
        if (cancelled) return;
        setState({
          status: "error",
          events: [],
          error: err instanceof Error ? err : new Error(String(err)),
        });
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [sessionKey, turnId, attempt]);

  const userInput = React.useMemo(
    () => extractUserInput(state.events),
    [state.events],
  );
  const finishReason = React.useMemo(
    () => extractFinishReason(state.events),
    [state.events],
  );

  return (
    <div className="flex min-h-[60vh] flex-col gap-4">
      <Breadcrumb sessionKey={sessionKey} turnId={turnId} />

      {state.status === "loading" ? (
        <LoadingSkeleton />
      ) : state.status === "error" ? (
        <ErrorBox
          message={state.error?.message ?? "unknown"}
          onRetry={() => setAttempt((n) => n + 1)}
        />
      ) : (
        <TimelineProvider>
          <TurnSummaryCard
            turnId={turnId}
            userInput={userInput}
            finishReason={finishReason}
          />
          <section
            className={cn(
              "flex-1 rounded-lg border border-tp-glass-edge bg-tp-glass",
              "p-4 sm:p-6",
            )}
          >
            <EventTimelineBody
              sessionKey={sessionKey}
              mode="replay"
              turnIdFilter={turnId}
              seedEvents={state.events}
            />
          </section>
        </TimelineProvider>
      )}
    </div>
  );
}

/* -------------------------------------------------------------- */
/*                          Sub-components                        */
/* -------------------------------------------------------------- */

function Breadcrumb({
  sessionKey,
  turnId,
}: {
  sessionKey: string;
  turnId: string;
}) {
  const { t } = useTranslation();
  return (
    <header className="flex flex-col gap-3">
      <Button
        asChild
        variant="ghost"
        size="sm"
        className="-ml-2 h-7 w-fit px-2 text-tp-ink-3 hover:text-tp-ink"
      >
        <Link href={`/admin/sessions/${encodeURIComponent(sessionKey)}`}>
          <ChevronLeft className="h-3.5 w-3.5" aria-hidden="true" />
          {sessionKey || t("sessions.title")}
        </Link>
      </Button>
      <nav
        aria-label="breadcrumb"
        className="flex flex-wrap items-center gap-1 text-xs text-tp-ink-3"
      >
        <Link
          href="/admin/sessions"
          className="hover:text-tp-ink hover:underline"
        >
          {t("sessions.title")}
        </Link>
        <ChevronRight className="h-3 w-3 opacity-50" aria-hidden="true" />
        <Link
          href={`/admin/sessions/${encodeURIComponent(sessionKey)}`}
          className="font-mono hover:text-tp-ink hover:underline"
        >
          {sessionKey}
        </Link>
        <ChevronRight className="h-3 w-3 opacity-50" aria-hidden="true" />
        <span className="font-mono text-tp-ink">
          {t("sessions.turn.breadcrumb")} {turnId.slice(0, 12)}
        </span>
      </nav>
      <h1 className="text-2xl font-semibold tracking-tight">
        {t("sessions.turn.title")}
      </h1>
    </header>
  );
}

function LoadingSkeleton() {
  return (
    <>
      <div className="rounded-2xl border border-tp-glass-edge bg-tp-glass p-4">
        <Skeleton className="h-4 w-24" />
        <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={`fld-${i}`} className="flex flex-col gap-1">
              <Skeleton className="h-3 w-16" />
              <Skeleton className="h-4 w-20" />
            </div>
          ))}
        </div>
      </div>
      <section className="space-y-3 rounded-lg border border-tp-glass-edge bg-tp-glass p-4 sm:p-6">
        {Array.from({ length: 3 }).map((_, i) => (
          <div
            key={`row-${i}`}
            className="rounded-2xl border border-tp-glass-edge bg-tp-glass-inner p-4"
          >
            <Skeleton className="h-3 w-32" />
            <Skeleton className="mt-3 h-3 w-3/4" />
            <Skeleton className="mt-2 h-3 w-1/2" />
          </div>
        ))}
      </section>
    </>
  );
}

function ErrorBox({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div
      role="alert"
      data-testid="turn-load-error"
      className={cn(
        "flex flex-col gap-3 rounded-2xl border px-4 py-4 sm:flex-row sm:items-center",
        "border-red-300/60 bg-red-50/60 text-red-900",
        "dark:border-red-400/30 dark:bg-red-950/30 dark:text-red-200",
      )}
    >
      <AlertTriangle
        className="h-4 w-4 shrink-0 text-red-500"
        aria-hidden="true"
      />
      <div className="flex-1 text-sm">
        <div className="font-medium">{t("sessions.turn.loadError")}</div>
        <div className="mt-0.5 break-words font-mono text-xs opacity-80">
          {message}
        </div>
      </div>
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={onRetry}
        data-testid="turn-load-retry"
      >
        {t("sessions.turn.retry")}
      </Button>
    </div>
  );
}
