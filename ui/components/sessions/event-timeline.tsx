/**
 * Live session timeline.
 *
 * - Mounts the SSE stream via `openLiveEventStream`.
 * - Batches incoming events through `requestAnimationFrame` so we don't
 *   re-render the store on every byte of a TextDelta.
 * - Renders one card per turn; each turn shows ordered Parts (text,
 *   reasoning, tool calls).
 *
 * Tidepool: warm amber + faint glassmorphism.
 */
"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { AlertTriangle, Loader2 } from "lucide-react";

import { cn } from "@/lib/utils";
import { openLiveEventStream, type LiveEvent } from "@/lib/sessions/event-stream";
import {
  TimelineProvider,
  useTimeline,
  type Part,
  type Turn,
} from "@/lib/sessions/store";
import { ReasoningBlock } from "./reasoning-block";
import { ToolWidget } from "./tool-widget";

export interface EventTimelineProps {
  sessionKey: string;
  className?: string;
  /**
   * `live` (default) opens the SSE stream and renders all turns in
   * insertion order. `replay` skips the SSE connection entirely — the
   * caller seeds the store via `seedEvents` once and we render the
   * resulting turns statically. Used by the per-turn drill-down page
   * (W2.2) to get pixel-identical rendering to the live view.
   */
  mode?: "live" | "replay";
  /**
   * When set, the inner timeline filters its rendered turn cards to the
   * matching `turn_id`. The store still holds every turn the consumer
   * dispatched, we just hide the others. Useful for the drill-down page.
   */
  turnIdFilter?: string;
  /**
   * Pre-fetched events to seed the store with on mount. Only consumed by
   * `mode='replay'` — passing this in `mode='live'` is a no-op.
   */
  seedEvents?: LiveEvent[];
}

/* -------------------------------------------------------------- */
/*                          Part renderer                         */
/* -------------------------------------------------------------- */

function renderPart(part: Part): React.ReactNode {
  switch (part.kind) {
    case "text":
      return (
        <div
          key={part.block_id}
          className={cn(
            "whitespace-pre-wrap break-words text-sm leading-relaxed",
            "text-amber-950 dark:text-amber-50",
          )}
        >
          {part.text}
          {!part.done && (
            <span
              className="ml-0.5 inline-block h-3.5 w-1 animate-pulse bg-amber-500/80 align-middle"
              aria-hidden
            />
          )}
        </div>
      );
    case "reasoning":
      return (
        <ReasoningBlock key={part.block_id} text={part.text} streaming={!part.done} />
      );
    case "tool_use":
      return <ToolWidget key={part.block_id} part={part} />;
  }
}

/* -------------------------------------------------------------- */
/*                          Turn card                             */
/* -------------------------------------------------------------- */

function TurnCard({ turn }: { turn: Turn }) {
  const { t } = useTranslation();

  return (
    <article
      data-testid="timeline-turn-card"
      data-turn-id={turn.turn_id}
      data-turn-status={turn.status}
      className={cn(
        "rounded-2xl border border-amber-200/50 bg-white/60 p-4 backdrop-blur-md",
        "dark:border-white/10 dark:bg-black/30",
        "shadow-sm",
      )}
    >
      <header className="mb-2 flex items-center gap-2 text-[11px] text-amber-700/70 dark:text-amber-200/60">
        <span className="font-mono opacity-70">#{turn.turn_id.slice(0, 8)}</span>
        <span className="opacity-50">·</span>
        <span>{new Date(turn.startedAt).toLocaleTimeString()}</span>
        {turn.status === "streaming" && (
          <span className="ml-auto inline-flex items-center gap-1 rounded-sm bg-amber-200/40 px-1.5 py-0.5 font-medium text-amber-900 dark:bg-amber-700/30 dark:text-amber-200">
            <Loader2 className="size-3 animate-spin" aria-hidden />
            {t("sessions.timeline.streaming")}
          </span>
        )}
        {turn.status === "complete" && (
          <span className="ml-auto rounded-sm bg-emerald-200/40 px-1.5 py-0.5 font-medium text-emerald-900 dark:bg-emerald-700/30 dark:text-emerald-200">
            {t("sessions.timeline.complete")}
          </span>
        )}
        {turn.status === "errored" && (
          <span className="ml-auto inline-flex items-center gap-1 rounded-sm bg-red-200/40 px-1.5 py-0.5 font-medium text-red-900 dark:bg-red-700/30 dark:text-red-200">
            <AlertTriangle className="size-3" aria-hidden />
            {t("sessions.timeline.errored")}
          </span>
        )}
        {turn.status === "cancelling" && (
          <span className="ml-auto rounded-sm bg-amber-300/40 px-1.5 py-0.5 font-medium text-amber-900 dark:bg-amber-600/30 dark:text-amber-100">
            {t("sessions.timeline.cancelling")}
          </span>
        )}
      </header>
      <div className="space-y-2">
        {turn.parts.length === 0 ? (
          <div className="text-xs italic opacity-50">{t("sessions.timeline.empty")}</div>
        ) : (
          turn.parts.map(renderPart)
        )}
      </div>
      {turn.errorMessage && (
        <div className="mt-3 rounded-md border border-red-200/60 bg-red-50/40 px-2 py-1.5 text-xs text-red-900 dark:border-red-400/20 dark:bg-red-950/30 dark:text-red-200">
          {turn.errorMessage}
        </div>
      )}
    </article>
  );
}

/* -------------------------------------------------------------- */
/*                          Inner timeline                        */
/* -------------------------------------------------------------- */

interface TimelineInnerProps {
  sessionKey: string;
  mode: "live" | "replay";
  turnIdFilter?: string;
  seedEvents?: LiveEvent[];
}

/**
 * Replay-mode seeder. Dispatches the pre-fetched event batch once on
 * mount so the existing reducer can fold them into the same Turn shape
 * the live view uses. No SSE is opened.
 */
function useReplaySeed(seedEvents: LiveEvent[] | undefined) {
  const { dispatch } = useTimeline();
  React.useEffect(() => {
    if (!seedEvents || seedEvents.length === 0) return;
    dispatch({ type: "events", events: seedEvents });
    return () => {
      dispatch({ type: "reset" });
    };
  }, [seedEvents, dispatch]);
}

function TimelineInner({
  sessionKey,
  mode,
  turnIdFilter,
  seedEvents,
}: TimelineInnerProps) {
  const { t } = useTranslation();
  const { state } = useTimeline();
  // Hooks must run unconditionally — branch *inside* each hook instead.
  const live = useLiveStreamMaybe(sessionKey, mode === "live");
  useReplaySeed(mode === "replay" ? seedEvents : undefined);

  const allTurns = state.turnOrder
    .map((id) => state.turns[id])
    .filter(Boolean) as Turn[];
  const turns = turnIdFilter
    ? allTurns.filter((t) => t.turn_id === turnIdFilter)
    : allTurns;

  return (
    <div className="space-y-3">
      {mode === "live" ? (
        <div className="flex items-center gap-2 text-[11px] text-amber-700/70 dark:text-amber-200/60">
          <span
            className={cn(
              "inline-block size-2 rounded-full",
              live.connected
                ? "bg-emerald-500 shadow-[0_0_4px] shadow-emerald-400/60"
                : "bg-amber-400 animate-pulse",
            )}
            aria-hidden
          />
          <span>
            {live.connected
              ? t("sessions.timeline.connected")
              : live.error
                ? t("sessions.timeline.reconnecting")
                : t("sessions.timeline.connecting")}
          </span>
        </div>
      ) : null}
      {turns.length === 0 ? (
        <div
          className={cn(
            "rounded-2xl border border-dashed border-amber-300/60 bg-amber-50/30",
            "dark:border-white/10 dark:bg-black/20",
            "p-6 text-center text-sm italic text-amber-700/60 dark:text-amber-200/60",
          )}
        >
          {mode === "replay"
            ? t("sessions.turn.empty")
            : t("sessions.timeline.waiting")}
        </div>
      ) : (
        turns.map((turn) => <TurnCard key={turn.turn_id} turn={turn} />)
      )}
    </div>
  );
}

/**
 * Live-stream hook that no-ops when disabled. Keeps the hook order
 * stable for both modes without resorting to conditional hook calls.
 */
function useLiveStreamMaybe(
  sessionKey: string,
  enabled: boolean,
): { error: string | null; connected: boolean } {
  const { dispatch } = useTimeline();
  const [error, setError] = React.useState<string | null>(null);
  const [connected, setConnected] = React.useState<boolean>(false);

  React.useEffect(() => {
    if (!enabled) return;
    let queue: LiveEvent[] = [];
    let raf: number | null = null;

    const flush = () => {
      raf = null;
      if (queue.length === 0) return;
      const batch = queue;
      queue = [];
      dispatch({ type: "events", events: batch });
    };

    const close = openLiveEventStream(sessionKey, {
      onEvent: (ev) => {
        setConnected(true);
        setError(null);
        queue.push(ev);
        if (raf == null && typeof window !== "undefined") {
          raf = window.requestAnimationFrame(flush);
        }
      },
      onError: () => {
        setConnected(false);
        setError("disconnected");
      },
    });

    return () => {
      if (raf != null && typeof window !== "undefined") {
        window.cancelAnimationFrame(raf);
      }
      close();
      dispatch({ type: "reset" });
    };
  }, [enabled, sessionKey, dispatch]);

  return { error, connected };
}

/* -------------------------------------------------------------- */
/*                          Public export                         */
/* -------------------------------------------------------------- */

export function EventTimeline({
  sessionKey,
  className,
  mode = "live",
  turnIdFilter,
  seedEvents,
}: EventTimelineProps) {
  return (
    <div className={className} data-testid="event-timeline" data-mode={mode}>
      <TimelineProvider>
        <TimelineInner
          sessionKey={sessionKey}
          mode={mode}
          turnIdFilter={turnIdFilter}
          seedEvents={seedEvents}
        />
      </TimelineProvider>
    </div>
  );
}

/**
 * Provider-less variant of {@link EventTimeline} for callers that own
 * their own `<TimelineProvider>` and want to share the store with
 * sibling components (e.g. the W2.2 drill-down page, which renders a
 * `<TurnSummaryCard>` next to the timeline body that needs to read the
 * same `Turn` record).
 */
export function EventTimelineBody({
  sessionKey,
  className,
  mode = "live",
  turnIdFilter,
  seedEvents,
}: EventTimelineProps) {
  return (
    <div className={className} data-testid="event-timeline-body" data-mode={mode}>
      <TimelineInner
        sessionKey={sessionKey}
        mode={mode}
        turnIdFilter={turnIdFilter}
        seedEvents={seedEvents}
      />
    </div>
  );
}
