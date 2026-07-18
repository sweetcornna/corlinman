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
import { useTranslation } from "react-i18next";
import { Loader2, LinkIcon, AlertTriangle, WifiOff } from "lucide-react";

import { cn } from "@/lib/utils";
import type { LiveEvent } from "@/lib/sessions/event-stream";
import { TimelineProvider } from "@/lib/sessions/store";
import { EventTimelineBody } from "@/components/sessions/event-timeline";
import {
  fetchStatusSnapshot,
  openStatusEventStream,
  personaAvatarUrl,
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
      className="font-mono text-sm tabular-nums text-sg-ink-2"
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

/**
 * Full-bleed deep-space backdrop for the standalone status card. This page
 * lives OUTSIDE the admin shell, so it can't borrow the admin layout's
 * <AuroraBackground />; it paints its own layered nebula glow + noise here.
 * All layers are decorative (aria-hidden, pointer-events-none) and sit behind
 * the content; the base deep-space gradient itself is painted on <html> in
 * globals.css (theme-flipping, pre-hydration), so we only add depth on top.
 */
function StatusBackdrop() {
  return (
    <div
      aria-hidden="true"
      className="fixed inset-0 -z-10 overflow-hidden pointer-events-none"
    >
      {/* Nebula glow blobs — soft accent-hued radials, slow drift + hue drift. */}
      <div
        className="absolute inset-0 sg-drift pointer-events-none"
        style={{
          backgroundImage:
            "radial-gradient(900px 560px at 15% 8%, var(--sg-nebula-1), transparent 60%), " +
            "radial-gradient(760px 540px at 86% 18%, var(--sg-nebula-2), transparent 60%), " +
            "radial-gradient(680px 460px at 52% 96%, var(--sg-nebula-3), transparent 62%)",
        }}
      />
      {/* Twinkling starfield (dark theme only — hidden in daylight via CSS). */}
      <div className="absolute inset-0 pointer-events-none" />
      {/* Depth vignette — fade toward the edges so corners keep spatial depth. */}
      <div
        className="absolute inset-0 pointer-events-none"
        style={{
          background:
            "radial-gradient(120% 90% at 50% 30%, transparent 0%, var(--sg-space-3) 78%, var(--sg-space-0) 100%)",
        }}
      />
      {/* Fractal noise — breaks gradient banding at ~3%. */}
      <div className="absolute inset-0 opacity-[0.03] pointer-events-none" />
    </div>
  );
}

function CenteredCard({
  children,
  tone = "glass",
}: {
  children: React.ReactNode;
  /** "err" paints the overlay as an error-soft glass band. */
  tone?: "glass" | "err";
}) {
  return (
    <div className="relative flex min-h-dvh items-center justify-center px-6 py-12">
      <StatusBackdrop />
      <div
        className={cn(
          "sg-glass-overlay w-full max-w-md rounded-sg-xl p-8 text-center shadow-sg-4 animate-sg-rise",
          tone === "err" && "border-sg-err/30",
        )}
        style={
          tone === "err"
            ? { backgroundColor: "var(--sg-err-soft)" }
            : undefined
        }
      >
        {children}
      </div>
    </div>
  );
}

function ExpiredState() {
  const { t } = useTranslation();
  return (
    <CenteredCard tone="err">
      <div
        data-testid="status-expired"
        className="flex flex-col items-center gap-3"
      >
        <span className="inline-flex size-12 items-center justify-center rounded-full border border-sg-err/30 bg-sg-err-soft text-sg-err">
          <LinkIcon className="size-6" aria-hidden />
        </span>
        <h1 className="text-lg font-semibold tracking-tight text-sg-ink">
          {t("status.expiredTitle")}
        </h1>
        <p className="max-w-xs text-sm text-sg-ink-3">
          {t("status.expiredMessage")}
        </p>
      </div>
    </CenteredCard>
  );
}

function LoadingState() {
  const { t } = useTranslation();
  return (
    <CenteredCard>
      <div className="flex flex-col items-center gap-3">
        <Loader2 className="size-6 animate-spin text-sg-accent" aria-hidden />
        <p className="text-sm text-sg-ink-3">{t("status.loading")}</p>
      </div>
    </CenteredCard>
  );
}

function ErrorState({ onRetry }: { onRetry: () => void }) {
  const { t } = useTranslation();
  return (
    <CenteredCard tone="err">
      <div className="flex flex-col items-center gap-3">
        <span className="inline-flex size-12 items-center justify-center rounded-full border border-sg-err/30 bg-sg-err-soft text-sg-err">
          <AlertTriangle className="size-6" aria-hidden />
        </span>
        <h1 className="text-base font-semibold tracking-tight text-sg-ink">
          {t("status.errorTitle")}
        </h1>
        <p className="max-w-xs text-sm text-sg-ink-3">
          {t("status.errorMessage")}
        </p>
        <button
          type="button"
          onClick={onRetry}
          className={cn(
            "mt-1 rounded-sg-md border border-sg-border-strong bg-sg-inset px-4 py-2",
            "text-sm font-medium text-sg-ink transition-colors hover:bg-sg-inset-hover",
          )}
        >
          {t("status.tryAgain")}
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
  const { t } = useTranslation();
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
    <div className="relative min-h-dvh">
      <StatusBackdrop />
      <div className="mx-auto flex w-full max-w-xl flex-col gap-5 px-4 py-8 sm:px-6 sm:py-12">
        {/* Hero card — the showcase surface: a floating matte panel
            (opaque charcoal, strong moon edge, elev-4). Persona avatar,
            live-ticking elapsed timer in font-mono. No admin nav, no
            shell: a standalone, mobile-friendly public card. */}
        <header
          className="sg-glass-overlay relative overflow-hidden rounded-sg-xl p-6 animate-sg-rise sm:p-7"
        >
          <div className="flex items-start justify-between gap-3">
            <div className="flex min-w-0 items-center gap-3.5">
              <PersonaAvatar personaId={snapshot.persona_id} />
              <div className="min-w-0">
                <h1 className="text-sg-ink truncate text-xl font-semibold tracking-tight sm:text-2xl">
                  {t("status.agentStatus")}
                </h1>
                <span className="mt-0.5 block truncate font-mono text-[11px] text-sg-ink-4">
                  {snapshot.session_key.slice(0, 12)}
                </span>
              </div>
            </div>
            <ConnectionDot connected={connected} degraded={degraded} />
          </div>

          <div className="mt-5 flex flex-wrap items-center gap-3">
            <StatusPill state={snapshot.status} />
            <ElapsedReadout startMs={startMs} active={active} />
          </div>

          {/* Activity bar — accent gradient fill on a sunken inset track.
              While active it animates as an indeterminate sweep; when the
              run is settled it rests as a full accent rail. */}
          <ActivityBar active={active} />
        </header>

        {/* Read-only trajectory — reuses the admin replay timeline. Faux-glass
            content card (NO blur — the SSE feed scrolls behind it). */}
        <section className="sg-card rounded-sg-lg p-3 shadow-sg-2 sm:p-5">
          <TimelineProvider>
            <EventTimelineBody
              sessionKey={snapshot.session_key}
              mode="replay"
              seedEvents={events}
            />
          </TimelineProvider>
        </section>

        <footer className="pt-1 text-center text-[11px] text-sg-ink-4">
          {t("status.footer")}
        </footer>
      </div>
    </div>
  );
}

/**
 * Slim activity rail under the hero status row. Accent gradient fill
 * (sg-accent → sg-accent-2) on a sunken inset track. While the conversation
 * is active it sweeps as an indeterminate progress shimmer; once settled it
 * rests as a calm full-width accent rail.
 */
function ActivityBar({ active }: { active: boolean }) {
  return (
    <div
      aria-hidden="true"
      className="mt-5 h-1.5 w-full overflow-hidden rounded-full bg-sg-inset shadow-[inset_0_1px_2px_oklch(0_0_0_/_0.2)]"
    >
      <div
        className={cn(
          "h-full rounded-full bg-gradient-to-r from-sg-accent to-sg-accent-2",
          active ? "w-1/3 shimmer animate-pulse-glow" : "w-full",
        )}
      />
    </div>
  );
}

/**
 * Persona avatar chip (F2). Renders the bound persona's art (emoji else
 * reference 立绘) via the public `/public/personas/{id}/avatar` route. Renders
 * nothing when no persona is bound (`personaId` absent) or after the image
 * fails to load (the persona has no art / the blob 404s) — the card degrades
 * cleanly to its persona-less layout.
 */
function PersonaAvatar({ personaId }: { personaId: string | undefined }) {
  const src = personaAvatarUrl(personaId);
  const [failed, setFailed] = React.useState(false);
  React.useEffect(() => setFailed(false), [src]);
  if (!src || failed) return null;
  // A plain <img> (not next/image): this is a static-export page served by the
  // gateway, where next/image's loader/optimizer doesn't apply, and the src is
  // a public capability URL on the gateway origin. Wrapped in an accent ring
  // glow so the bound persona reads as the card's focal point.
  return (
    <span className="relative inline-flex size-12 shrink-0 items-center justify-center rounded-full bg-sg-accent-soft p-[2px] shadow-sg-glow ring-1 ring-sg-accent/40">
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={src}
        alt={`${personaId} avatar`}
        data-testid="status-persona-avatar"
        onError={() => setFailed(true)}
        className="size-full rounded-full border border-sg-border object-cover"
      />
    </span>
  );
}

function ConnectionDot({
  connected,
  degraded,
}: {
  connected: boolean;
  degraded: boolean;
}) {
  const { t } = useTranslation();
  if (connected) {
    return (
      <span
        data-testid="status-connection"
        data-connected="true"
        className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-sg-ok/30 bg-sg-ok-soft py-1 pl-2 pr-2.5 text-[11px] font-medium text-sg-ok"
      >
        <span
          className="sg-breathe h-[6px] w-[6px] rounded-full bg-sg-ok"
          aria-hidden
        />
        {t("status.connLive")}
      </span>
    );
  }
  return (
    <span
      data-testid="status-connection"
      data-connected="false"
      className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-sg-border bg-sg-inset py-1 pl-2 pr-2.5 text-[11px] text-sg-ink-4"
    >
      <WifiOff className="size-3" aria-hidden />
      {degraded ? t("status.connReconnecting") : t("status.connConnecting")}
    </span>
  );
}

export default StatusClient;
