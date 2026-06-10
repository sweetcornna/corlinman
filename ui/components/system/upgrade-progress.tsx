"use client";

/**
 * `<UpgradeProgress>` — live, SSE-driven panel that follows a running
 * one-click upgrade.
 *
 * Sources truth from `GET /admin/system/upgrade/{id}/events` (SSE);
 * falls back to polling `fetchUpgradeStatus(id)` every 2s if the
 * EventSource never opens. Closes on terminal state.
 *
 * Backend TODO — cancel is currently "stop watching" only; the
 * upgrade itself continues in the background. Mid-flight abort
 * is not yet supported by the protocol layer.
 */

import * as React from "react";
import { useTranslation } from "react-i18next";
import { CheckCircle2, Circle, Loader2, XCircle } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  fetchUpgradeStatus,
  streamUpgradeEvents,
  type UpgradeStatusResponse,
} from "@/lib/api";
import { cn } from "@/lib/utils";

/** Phase progression we know about. The current phase is whatever
 * `status.phase` says, even if it falls outside this list — in that
 * case the unknown phase renders as the leading pill verbatim. */
const PHASE_ORDER = [
  "validating",
  "pulling",
  "recreating",
  "healthcheck",
  "done",
] as const;

type Phase = (typeof PHASE_ORDER)[number];

function isKnownPhase(p: string): p is Phase {
  return (PHASE_ORDER as readonly string[]).includes(p);
}

const TERMINAL_STATES = new Set([
  "succeeded",
  "failed",
  "stalled",
  "cancelled",
]);

const AUTO_RELOAD_SECONDS = 5;

export interface UpgradeProgressProps {
  requestId: string;
  /** Fires once the stream reaches a terminal state. The parent typically
   * schedules a window.location.reload() ~5s later on success. */
  onTerminal?: (status: UpgradeStatusResponse) => void;
}

export function UpgradeProgress({
  requestId,
  onTerminal,
}: UpgradeProgressProps) {
  const { t } = useTranslation();
  const [status, setStatus] = React.useState<UpgradeStatusResponse | null>(
    null,
  );
  const [now, setNow] = React.useState(() => Date.now());
  const [reloadIn, setReloadIn] = React.useState<number | null>(null);
  const closedRef = React.useRef(false);
  const onTerminalRef = React.useRef(onTerminal);
  onTerminalRef.current = onTerminal;

  // Elapsed tick every 1s while pre-terminal.
  React.useEffect(() => {
    if (status && TERMINAL_STATES.has(status.state)) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [status]);

  // SSE + polling fallback.
  React.useEffect(() => {
    closedRef.current = false;
    let es: EventSource | null = null;
    let pollHandle: number | null = null;
    let sseOpened = false;

    function handleStatus(next: UpgradeStatusResponse) {
      if (closedRef.current) return;
      setStatus(next);
      if (TERMINAL_STATES.has(next.state)) {
        closedRef.current = true;
        if (next.state === "succeeded") setReloadIn(AUTO_RELOAD_SECONDS);
        onTerminalRef.current?.(next);
        es?.close();
        if (pollHandle !== null) window.clearInterval(pollHandle);
      }
    }

    try {
      es = streamUpgradeEvents(requestId);
      es.addEventListener("status", (ev) => {
        sseOpened = true;
        try {
          const data = JSON.parse((ev as MessageEvent).data);
          handleStatus(data as UpgradeStatusResponse);
        } catch {
          /* malformed frame — ignore */
        }
      });
      es.addEventListener("error", () => {
        // Browser handles auto-reconnect; if we never opened, fall
        // through to polling below.
      });
    } catch {
      // EventSource unsupported / blocked — polling fallback only.
    }

    // Start polling immediately as a belt-and-suspenders. If the SSE
    // opens and starts emitting, the polling effectively just
    // produces redundant snapshots — handleStatus is idempotent.
    pollHandle = window.setInterval(async () => {
      if (closedRef.current) return;
      // If the SSE is alive (we've seen at least one frame), skip
      // the polling beat to reduce load.
      if (sseOpened && status && !TERMINAL_STATES.has(status.state)) {
        return;
      }
      try {
        const snap = await fetchUpgradeStatus(requestId);
        handleStatus(snap);
      } catch {
        /* swallow — the next tick or the SSE will recover */
      }
    }, 2000);

    return () => {
      closedRef.current = true;
      es?.close();
      if (pollHandle !== null) window.clearInterval(pollHandle);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestId]);

  // Auto-reload countdown on success.
  React.useEffect(() => {
    if (reloadIn === null) return;
    if (reloadIn <= 0) {
      window.location.reload();
      return;
    }
    const id = window.setTimeout(() => setReloadIn(reloadIn - 1), 1000);
    return () => window.clearTimeout(id);
  }, [reloadIn]);

  const elapsed =
    status?.started_at != null
      ? Math.max(0, Math.floor((now - status.started_at) / 1000))
      : null;
  const terminal = status ? TERMINAL_STATES.has(status.state) : false;

  return (
    <section
      data-testid="upgrade-progress"
      className="space-y-4 rounded-lg border border-tp-glass-edge bg-tp-glass p-4 sm:p-6"
    >
      <header className="flex items-center justify-between">
        <h2 className="text-lg font-semibold tracking-tight">
          {terminal && status?.state === "succeeded"
            ? t("system.upgrade.succeeded.title")
            : terminal && status?.state === "failed"
              ? t("system.upgrade.failed.title")
              : terminal && status?.state === "stalled"
                ? t("system.upgrade.stalled.title")
                : t("system.upgrade.progress.title")}
        </h2>
        {!terminal && elapsed !== null ? (
          <span className="font-mono text-xs text-tp-ink-3">
            {t("system.upgrade.progress.elapsed", { seconds: elapsed })}
          </span>
        ) : null}
      </header>

      {/* Phase pills */}
      <div
        className="flex flex-wrap gap-2"
        data-testid="upgrade-progress-phases"
      >
        {PHASE_ORDER.map((p) => {
          const currentPhase = status?.phase;
          const currentIdx =
            currentPhase && isKnownPhase(currentPhase)
              ? PHASE_ORDER.indexOf(currentPhase)
              : -1;
          const thisIdx = PHASE_ORDER.indexOf(p);
          const isCurrent = currentPhase === p && !terminal;
          const isPast = currentIdx > thisIdx || (terminal && status?.state === "succeeded");
          const isFailed =
            terminal && status?.state === "failed" && currentPhase === p;
          return (
            <span
              key={p}
              data-testid={`upgrade-progress-phase-${p}`}
              data-state={
                isFailed
                  ? "failed"
                  : isPast
                    ? "past"
                    : isCurrent
                      ? "current"
                      : "pending"
              }
              className={cn(
                "inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-xs",
                isFailed && "border-sg-err/60 bg-sg-err-soft text-sg-err",
                isPast && "border-sg-ok/40 bg-sg-ok-soft text-sg-ok",
                isCurrent && "border-sg-accent/60 bg-sg-accent-soft text-sg-accent",
                !isFailed && !isPast && !isCurrent && "border-sg-border text-sg-ink-4",
              )}
            >
              {isCurrent ? (
                <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
              ) : isPast ? (
                <CheckCircle2 className="h-3 w-3" aria-hidden />
              ) : isFailed ? (
                <XCircle className="h-3 w-3" aria-hidden />
              ) : (
                <Circle className="h-3 w-3" aria-hidden />
              )}
              {t(`system.upgrade.phases.${p}`)}
            </span>
          );
        })}
        {/* Unknown phase fallback — show as leading pill verbatim */}
        {status?.phase && !isKnownPhase(status.phase) && !terminal ? (
          <span className="inline-flex items-center gap-1 rounded-full border border-sg-accent/60 bg-sg-accent-soft px-2.5 py-1 text-xs text-sg-accent">
            <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
            {status.phase}
          </span>
        ) : null}
      </div>

      {/* Log tail */}
      {status?.log_excerpt ? (
        <pre
          data-testid="upgrade-progress-log"
          className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md border border-tp-glass-edge bg-tp-glass-inner p-3 font-mono text-[11px] text-tp-ink-2"
        >
          {status.log_excerpt}
        </pre>
      ) : null}

      {/* Terminal banners */}
      {terminal && status?.state === "succeeded" ? (
        <Alert
          variant="success"
          title={t("system.upgrade.succeeded.title")}
          className="items-center justify-between gap-3"
        >
          <div className="flex items-center justify-between gap-3">
            <div>
              <p className="text-xs">
                {t("system.upgrade.succeeded.subtitle", { tag: status.tag })}
              </p>
              {reloadIn !== null ? (
                <p className="mt-1 text-xs opacity-80">
                  {t("system.upgrade.succeeded.autoReload", {
                    seconds: reloadIn,
                  })}
                </p>
              ) : null}
            </div>
            <Button
              type="button"
              onClick={() => window.location.reload()}
              size="sm"
            >
              {t("system.upgrade.succeeded.reload")}
            </Button>
          </div>
        </Alert>
      ) : null}

      {terminal && status?.state === "failed" ? (
        <Alert variant="danger" title={t("system.upgrade.failed.title")}>
          <p className="break-words text-xs">
            {t("system.upgrade.failed.subtitle", {
              error: status.error ?? "unknown",
            })}
          </p>
        </Alert>
      ) : null}

      {/* Cancel-as-stop-watching */}
      {!terminal ? (
        <div className="flex items-center justify-end">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            title={t("system.upgrade.progress.cancelHint")}
            onClick={() => {
              closedRef.current = true;
              setStatus((s) =>
                s ? { ...s, state: "cancelled" } : s,
              );
            }}
            data-testid="upgrade-progress-cancel"
          >
            {t("system.upgrade.progress.cancel")}
          </Button>
        </div>
      ) : null}
    </section>
  );
}
