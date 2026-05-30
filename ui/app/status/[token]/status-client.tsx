"use client";

/**
 * Client body of the public status card.
 *
 * Lifecycle:
 *   1. Read the token from the URL at runtime (one static shell serves every
 *      token — see `page.tsx`).
 *   2. `GET /status/{token}` for the initial snapshot. 403 → terminal
 *      "link expired or invalid" empty state.
 *   3. Subscribe to `GET /status/{token}/events/live` (SSE) for live updates
 *      (#31). Each frame is a LiveEvent envelope we accumulate and feed to the
 *      shared read-only timeline reducer.
 *   4. If SSE drops (transport error), fall back to ~3s polling of the
 *      snapshot until the socket recovers.
 *
 * The trajectory is rendered by `EventTimelineBody mode="replay"` — the SAME
 * component the admin session-detail page uses, but with every kill / approve
 * / admin control already absent (the replay path renders pure display cards).
 */

import * as React from "react";
import { useParams } from "next/navigation";
import { Loader2, LinkIcon, AlertTriangle, WifiOff } from "lucide-react";

import { cn } from "@/lib/utils";
import type { LiveEvent } from "@/lib/sessions/event-stream";
import { TimelineProvider } from "@/lib/sessions/store";
import { EventTimelineBody } from "@/components/sessions/event-timeline";
import {
  fetchStatusSnapshot,
  openStatusEventStream,
  StatusFetchError,
  type StatusSnapshot,
  type StatusState,
} from "@/lib/status";
import { StatusPill } from "./status-pill";

/** Polling cadence when SSE is unavailable / dropped. */
const POLL_INTERVAL_MS = 3_000;

/* -------------------------------------------------------------- */
/*                       Token extraction                         */
/* -------------------------------------------------------------- */

/**
 * Resolve the real token. `useParams()` returns the build-time placeholder
 * (`__shell__`) under static export, so we prefer the live URL path. We parse
 * `…/status/<token>` from `window.location.pathname`, falling back to the
 * route param only if the path can't be read (SSR / tests).
 */
function useStatusToken(): string | null {
  const params = useParams<{ token: string }>();
  const [token, setToken] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (typeof window === "undefined") return;
    const path = window.location.pathname;
    // Match the LAST `/status/<segment>` so an asset-prefixed deploy
    // (`/a/v123/status/<token>`) still resolves correctly.
    const m = path.match(/\/status\/([^/?#]+)\/?$/);
    const fromPath = m?.[1] ? decodeURIComponent(m[1]) : null;
    const raw = params?.token;
    const fromParam =
      raw && raw !== "__shell__"
        ? Array.isArray(raw)
          ? raw[0]
          : raw
        : null;
    setToken(fromPath ?? fromParam ?? null);
  }, [params]);

  return token;
}

/* -------------------------------------------------------------- */
/*                          Elapsed timer                         */
/* -------------------------------------------------------------- */

function formatElapsed(ms: number): string {
  if (ms < 0) ms = 0;
  const totalSec = Math.floor(ms / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  const two = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${two(m)}:${two(s)}` : `${m}:${two(s)}`;
}

/** Earliest known start time for the elapsed readout: explicit
 *  `started_at_ms` if the backend sent one, else the first event's
 *  timestamp. Returns null when neither is available. */
function resolveStartMs(snapshot: StatusSnapshot | null): number | null {
  if (!snapshot) return null;
  if (typeof snapshot.started_at_ms === "number") return snapshot.started_at_ms;
  const firstTurn = snapshot.turns.find(
    (t) => typeof t.started_at_ms === "number",
  );
  if (firstTurn?.started_at_ms != null) return firstTurn.started_at_ms;
  const firstEvent = snapshot.events[0];
  if (firstEvent && typeof firstEvent.timestamp_ms === "number") {
    return firstEvent.timestamp_ms;
  }
  return null;
}

/** Whether the conversation is still active (drives the live-ticking timer). */
function isActiveState(state: StatusState): boolean {
  return (
    state === "running" || state === "streaming" || state === "cancelling"
  );
}

function ElapsedReadout({
  startMs,
  active,
}: {
  startMs: number | null;
  active: boolean;
}) {
  const [now, setNow] = React.useState(() => Date.now());

  React.useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [active]);

  if (startMs == null) return null;
  return (
    <span
      data-testid="status-elapsed"
      className="font-mono text-xs tabular-nums text-tp-ink-3"
    >
      {formatElapsed((active ? now : Math.max(now, startMs)) - startMs)}
    </span>
  );
}

/* -------------------------------------------------------------- */
/*                         Live event feed                        */
/* -------------------------------------------------------------- */

/**
 * Accumulate events from the initial snapshot + the live SSE stream into a
 * single ordered array. The timeline reducer is idempotent on `sequence`
 * (it drops stale/duplicate events), so re-seeding the whole array on each
 * batch is safe; we keep the array identity stable except when it grows so
 * `EventTimelineBody`'s replay-seeder only re-dispatches on real change.
 */
function useStatusEvents(token: string | null, seed: LiveEvent[]) {
  const [events, setEvents] = React.useState<LiveEvent[]>(seed);
  const [connected, setConnected] = React.useState(false);
  // True once SSE has errored and we've armed the polling fallback.
  const [degraded, setDegraded] = React.useState(false);

  // Re-seed whenever the snapshot seed changes (token swap / refetch).
  React.useEffect(() => {
    setEvents(seed);
  }, [seed]);

  const pushBatch = React.useCallback((batch: LiveEvent[]) => {
    if (batch.length === 0) return;
    setEvents((prev) => {
      // Dedupe by composite (turn_id:sequence) so a poll-then-SSE-recover
      // overlap doesn't double-render. Cheap: status feeds are small.
      const seen = new Set(prev.map((e) => `${e.turn_id}:${e.sequence}`));
      const additions = batch.filter(
        (e) => !seen.has(`${e.turn_id}:${e.sequence}`),
      );
      return additions.length ? [...prev, ...additions] : prev;
    });
  }, []);

  // SSE subscription (#31) + 3s polling fallback when it drops.
  React.useEffect(() => {
    if (!token) return;
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    let cancelled = false;

    const startPolling = () => {
      if (pollTimer) return;
      pollTimer = setInterval(async () => {
        try {
          const snap = await fetchStatusSnapshot(token);
          if (cancelled) return;
          pushBatch(snap.events);
        } catch {
          // Swallow — the snapshot fetch on the page owns terminal errors.
        }
      }, POLL_INTERVAL_MS);
    };
    const stopPolling = () => {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    };

    const close = openStatusEventStream(token, {
      onOpen: () => {
        if (cancelled) return;
        setConnected(true);
        setDegraded(false);
        stopPolling();
      },
      onEvent: (ev) => {
        if (cancelled) return;
        setConnected(true);
        pushBatch([ev]);
      },
      onError: () => {
        if (cancelled) return;
        setConnected(false);
        setDegraded(true);
        startPolling();
      },
    });

    return () => {
      cancelled = true;
      stopPolling();
      close();
    };
  }, [token, pushBatch]);

  return { events, connected, degraded };
}

/* -------------------------------------------------------------- */
/*                       Empty / error states                     */
/* -------------------------------------------------------------- */

function CenteredCard({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-dvh items-center justify-center bg-background px-6 py-12">
      <div
        className={cn(
          "w-full max-w-md rounded-2xl border border-tp-glass-edge bg-tp-glass p-8 text-center",
        )}
      >
        {children}
      </div>
    </div>
  );
}

function ExpiredState() {
  return (
    <CenteredCard>
      <div
        data-testid="status-expired"
        className="flex flex-col items-center gap-3"
      >
        <LinkIcon className="size-8 text-tp-ink-4" aria-hidden />
        <h1 className="text-lg font-semibold tracking-tight text-tp-ink">
          Link expired or invalid
        </h1>
        <p className="max-w-xs text-sm text-tp-ink-3">
          This status link is no longer valid. Ask the assistant for a fresh
          link to follow along.
        </p>
      </div>
    </CenteredCard>
  );
}

function LoadingState() {
  return (
    <CenteredCard>
      <div className="flex flex-col items-center gap-3">
        <Loader2 className="size-6 animate-spin text-tp-ink-3" aria-hidden />
        <p className="text-sm text-tp-ink-3">Loading status…</p>
      </div>
    </CenteredCard>
  );
}

function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <CenteredCard>
      <div className="flex flex-col items-center gap-3">
        <AlertTriangle className="size-7 text-tp-warn" aria-hidden />
        <h1 className="text-base font-semibold tracking-tight text-tp-ink">
          Couldn&apos;t load status
        </h1>
        <p className="max-w-xs text-sm text-tp-ink-3">
          The status service didn&apos;t respond. It may be a brief hiccup.
        </p>
        <button
          type="button"
          onClick={onRetry}
          className={cn(
            "mt-1 rounded-md border border-tp-glass-edge bg-tp-glass-inner px-4 py-2",
            "text-sm font-medium text-tp-ink hover:bg-tp-glass-inner-hover",
          )}
        >
          Try again
        </button>
      </div>
    </CenteredCard>
  );
}

/* -------------------------------------------------------------- */
/*                          Main client                           */
/* -------------------------------------------------------------- */

type LoadState =
  | { kind: "loading" }
  | { kind: "expired" }
  | { kind: "error" }
  | { kind: "ready"; snapshot: StatusSnapshot };

export function StatusClient() {
  const token = useStatusToken();
  const [state, setState] = React.useState<LoadState>({ kind: "loading" });
  const [attempt, setAttempt] = React.useState(0);

  // Initial snapshot fetch (+ retry on `attempt` bump).
  React.useEffect(() => {
    if (!token) return;
    let cancelled = false;
    const controller = new AbortController();
    setState({ kind: "loading" });
    (async () => {
      try {
        const snapshot = await fetchStatusSnapshot(token, {
          signal: controller.signal,
        });
        if (cancelled) return;
        setState({ kind: "ready", snapshot });
      } catch (err) {
        if (cancelled || controller.signal.aborted) return;
        if (err instanceof StatusFetchError && err.expired) {
          setState({ kind: "expired" });
        } else {
          setState({ kind: "error" });
        }
      }
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [token, attempt]);

  if (token === null) return <LoadingState />;
  if (state.kind === "loading") return <LoadingState />;
  if (state.kind === "expired") return <ExpiredState />;
  if (state.kind === "error") {
    return <ErrorState onRetry={() => setAttempt((n) => n + 1)} />;
  }

  return <StatusReady token={token} snapshot={state.snapshot} />;
}

function StatusReady({
  token,
  snapshot,
}: {
  token: string;
  snapshot: StatusSnapshot;
}) {
  const { events, connected, degraded } = useStatusEvents(
    token,
    snapshot.events,
  );

  // Derive the headline state: prefer the snapshot rollup, but if any
  // streamed turn is still live we surface "running" so the pill stays
  // honest between snapshot refreshes.
  const startMs = React.useMemo(
    () => resolveStartMs({ ...snapshot, events }),
    [snapshot, events],
  );
  const active = isActiveState(snapshot.status);

  return (
    <div className="min-h-dvh bg-background">
      <div className="mx-auto flex w-full max-w-2xl flex-col gap-4 px-4 py-6 sm:px-6 sm:py-10">
        {/* Header — status pill + elapsed + live/offline indicator. No
            admin nav, no shell: this is a standalone mobile-friendly page. */}
        <header className="flex flex-col gap-3">
          <div className="flex items-center justify-between gap-2">
            <h1 className="text-sm font-semibold tracking-tight text-tp-ink">
              Agent status
            </h1>
            <ConnectionDot connected={connected} degraded={degraded} />
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <StatusPill state={snapshot.status} />
            <ElapsedReadout startMs={startMs} active={active} />
            <span className="font-mono text-[11px] text-tp-ink-4">
              {snapshot.session_key.slice(0, 12)}
            </span>
          </div>
        </header>

        {/* Read-only trajectory — reuses the admin replay timeline. */}
        <section
          className={cn(
            "rounded-2xl border border-tp-glass-edge bg-tp-glass p-3 sm:p-5",
          )}
        >
          <TimelineProvider>
            <EventTimelineBody
              sessionKey={snapshot.session_key}
              mode="replay"
              seedEvents={events}
            />
          </TimelineProvider>
        </section>

        <footer className="pt-2 text-center text-[11px] text-tp-ink-4">
          Read-only · live updates · corlinman
        </footer>
      </div>
    </div>
  );
}

function ConnectionDot({
  connected,
  degraded,
}: {
  connected: boolean;
  degraded: boolean;
}) {
  if (connected) {
    return (
      <span
        data-testid="status-connection"
        data-connected="true"
        className="inline-flex items-center gap-1.5 text-[11px] text-tp-ok"
      >
        <span className="tp-breathe h-[6px] w-[6px] rounded-full bg-tp-ok" aria-hidden />
        Live
      </span>
    );
  }
  return (
    <span
      data-testid="status-connection"
      data-connected="false"
      className="inline-flex items-center gap-1.5 text-[11px] text-tp-ink-4"
    >
      <WifiOff className="size-3" aria-hidden />
      {degraded ? "Reconnecting…" : "Connecting…"}
    </span>
  );
}

export default StatusClient;
