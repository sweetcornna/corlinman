"use client";

/**
 * `<UpgradeProgress>` — follows a running one-click upgrade.
 *
 * Why a spinner and not a progress bar: on the native (systemd) deploy the
 * upgrade **restarts the gateway** mid-flight, which drops the SSE stream
 * and orphans the in-memory status store, so a determinate bar keyed to
 * backend phases can never fill (the NativeUpgrader also emits no
 * sub-phases). We instead show an indeterminate "updating…" spinner and
 * detect completion the way sub2api does — by watching the server go down
 * and come back **on a new version**, then reloading the page.
 *
 * Signals:
 *   - SSE `GET /admin/system/upgrade/{id}/events` + a status poll — a
 *     best-effort source of the live phase/log and, crucially, of an
 *     *early* failure (validation / download) reported while the gateway
 *     is still up (no restart happens on those).
 *   - A reconnect poll of the UNAUTHENTICATED `GET /health` — the
 *     authoritative *success* signal since v1.28: the gateway reports a
 *     release-spaced `version`, and we only reload once it EQUALS the
 *     target tag (healthy-but-wrong-version keeps waiting; the backend
 *     finalizer will flip the record to failed). Pre-v1.28 backends have
 *     no `version` on `/health` → fall back to the `/admin/system/info`
 *     "version changed / came back with no update" heuristic.
 */

import * as React from "react";
import { useTranslation } from "react-i18next";
import { Loader2, RefreshCcw } from "lucide-react";
import { toast } from "sonner";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  cancelUpgrade,
  CorlinmanApiError,
  fetchHealthRaw,
  fetchSystemInfo,
  fetchUpgradeStatus,
  streamUpgradeEvents,
  type UpgradeStatusResponse,
} from "@/lib/api";

/** Terminal outcome of the upgrade as this component understands it. */
export type UpgradeOutcome = "pending" | "succeeded" | "failed";

/**
 * Decide, from the reconnect-poll signals, whether the upgrade has
 * completed successfully. Pure + exported for unit tests.
 *
 * Success is inferred from `/admin/system/info` coming back after the
 * restart:
 *   - the reported `current` differs from the version we captured before
 *     the upgrade (the new code is running), OR
 *   - we observed the server go unreachable (a restart) and it now
 *     reports no update available.
 */
export function detectUpgradeOutcome(args: {
  infoCurrent?: string | null;
  infoAvailable?: boolean | null;
  currentBefore?: string | null;
  sawServerDown: boolean;
}): "succeeded" | "pending" {
  const { infoCurrent, infoAvailable, currentBefore, sawServerDown } = args;
  if (
    infoCurrent != null &&
    currentBefore != null &&
    infoCurrent !== currentBefore
  ) {
    return "succeeded";
  }
  if (sawServerDown && infoAvailable === false) return "succeeded";
  return "pending";
}

function stripV(version: string): string {
  return version.startsWith("v") || version.startsWith("V")
    ? version.slice(1)
    : version;
}

/**
 * Strict restart-window verdict. Pure + exported for unit tests.
 *
 * When the (unauthenticated) `/health` probe reports a `version` AND we
 * know the target tag, ONLY an exact match (modulo the leading `v`)
 * counts as success — a healthy gateway on the wrong version keeps the
 * spinner (the backend's boot finalizer will fail the record). Without
 * both strict inputs, defer to the legacy `/info` heuristic
 * (`detectUpgradeOutcome`).
 */
export function resolveRestartOutcome(args: {
  healthVersion?: string | null;
  targetTag?: string | null;
  infoCurrent?: string | null;
  infoAvailable?: boolean | null;
  currentBefore?: string | null;
  sawServerDown: boolean;
}): "succeeded" | "pending" {
  const { healthVersion, targetTag } = args;
  if (healthVersion && targetTag) {
    return stripV(healthVersion) === stripV(targetTag)
      ? "succeeded"
      : "pending";
  }
  return detectUpgradeOutcome(args);
}

const RECONNECT_POLL_MS = 2500;
/** After this long with no terminal, surface a "taking longer" hint +
 * manual reload affordance (but keep polling). */
const SLOW_AFTER_MS = 5 * 60_000;
/** Small grace so the success banner is visible before the reload. */
const RELOAD_GRACE_MS = 1800;

export interface UpgradeProgressProps {
  requestId: string;
  /** The "current" version shown before the upgrade started (from
   * `/admin/system/info`). Lets us detect the server returning on a new
   * version. */
  currentVersion?: string | null;
  /** Target release tag (e.g. `v1.28.0`). Enables the strict
   * health-version assertion; when omitted it's learned from the first
   * status frame carrying `target_tag`/`tag`. */
  targetTag?: string | null;
  /** Fires once with the terminal outcome (not for `cancelled`). */
  onTerminal?: (outcome: UpgradeOutcome) => void;
}

export function UpgradeProgress({
  requestId,
  currentVersion,
  targetTag,
  onTerminal,
}: UpgradeProgressProps) {
  const { t } = useTranslation();

  const [outcome, setOutcome] = React.useState<UpgradeOutcome>("pending");
  const [error, setError] = React.useState<string | null>(null);
  const [logExcerpt, setLogExcerpt] = React.useState<string>("");
  const [restarting, setRestarting] = React.useState(false);
  const [slow, setSlow] = React.useState(false);
  const [cancelled, setCancelled] = React.useState(false);
  const [startedAt] = React.useState(() => Date.now());
  const [now, setNow] = React.useState(() => Date.now());

  const doneRef = React.useRef(false);
  const cancelledRef = React.useRef(false);
  const sawServerDownRef = React.useRef(false);
  const stopAllRef = React.useRef<(() => void) | null>(null);
  const onTerminalRef = React.useRef(onTerminal);
  onTerminalRef.current = onTerminal;

  // Capture the pre-upgrade version once; never let a later re-render
  // (e.g. a background /info refetch that already shows the new version)
  // overwrite the baseline we compare against.
  const currentBeforeRef = React.useRef<string | null>(currentVersion ?? null);
  React.useEffect(() => {
    if (currentBeforeRef.current == null && currentVersion != null) {
      currentBeforeRef.current = currentVersion;
    }
  }, [currentVersion]);

  // Target tag for the strict health-version assertion — prop first,
  // else learned from the first status frame that carries it.
  const targetTagRef = React.useRef<string | null>(targetTag ?? null);
  React.useEffect(() => {
    if (targetTagRef.current == null && targetTag != null) {
      targetTagRef.current = targetTag;
    }
  }, [targetTag]);
  const [targetLabel, setTargetLabel] = React.useState<string | null>(
    targetTag ?? null,
  );

  const finish = React.useCallback(
    (next: "succeeded" | "failed", err?: string | null) => {
      if (doneRef.current || cancelledRef.current) return;
      doneRef.current = true;
      setOutcome(next);
      if (err) setError(err);
      stopAllRef.current?.();
      onTerminalRef.current?.(next);
      if (next === "succeeded") {
        window.setTimeout(() => window.location.reload(), RELOAD_GRACE_MS);
      }
    },
    [],
  );

  // Elapsed-time tick while pending.
  React.useEffect(() => {
    if (outcome !== "pending" || cancelled) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [outcome, cancelled]);

  // SSE (phase/log + early failure) + reconnect poll (success signal).
  React.useEffect(() => {
    doneRef.current = false;
    cancelledRef.current = false;
    sawServerDownRef.current = false;
    setOutcome("pending");
    setError(null);
    setLogExcerpt("");
    setRestarting(false);
    setSlow(false);
    setCancelled(false);

    let es: EventSource | null = null;
    let pollTimer: number | null = null;
    let slowTimer: number | null = null;

    const stopAll = () => {
      es?.close();
      es = null;
      if (pollTimer !== null) {
        window.clearInterval(pollTimer);
        pollTimer = null;
      }
      if (slowTimer !== null) {
        window.clearTimeout(slowTimer);
        slowTimer = null;
      }
    };
    stopAllRef.current = stopAll;

    // Fold an SSE / status snapshot into local state. Only failures (and a
    // rare same-process success) are terminal here; the success path is
    // usually driven by the reconnect poll below.
    const ingestStatus = (s: UpgradeStatusResponse) => {
      if (doneRef.current || cancelledRef.current) return;
      if (s.log_excerpt) setLogExcerpt(s.log_excerpt);
      const learnedTarget = s.target_tag ?? s.tag ?? null;
      if (targetTagRef.current == null && learnedTarget) {
        targetTagRef.current = learnedTarget;
        setTargetLabel(learnedTarget);
      }
      if (s.state === "failed" || s.state === "stalled") {
        finish("failed", s.error ?? null);
      } else if (s.state === "succeeded") {
        finish("succeeded");
      } else if (s.state === "cancelled") {
        cancelledRef.current = true;
        stopAllRef.current?.();
        setCancelled(true);
      }
    };

    try {
      es = streamUpgradeEvents(requestId);
      es.addEventListener("status", (ev) => {
        try {
          ingestStatus(JSON.parse((ev as MessageEvent).data));
        } catch {
          /* malformed frame — ignore */
        }
      });
      // Browser auto-reconnects the EventSource; a permanent drop (the
      // restart) is handled by the reconnect poll, so no error handler.
    } catch {
      /* EventSource unsupported / blocked — poll-only. */
    }

    pollTimer = window.setInterval(async () => {
      if (doneRef.current || cancelledRef.current) return;

      // Status snapshot: surfaces phase/log while the gateway is up and
      // catches a persisted terminal after restart. 404 (request gone
      // from a fresh process) is expected — swallow it.
      try {
        ingestStatus(await fetchUpgradeStatus(requestId));
        if (doneRef.current) return;
      } catch {
        /* mid-restart / not found — the /info probe below is the signal */
      }

      // Liveness + version: the authoritative success signal. The
      // unauthenticated /health probe survives the restart window's
      // cookie churn; only an exact version match reloads (strict mode).
      try {
        const health = await fetchHealthRaw();
        if (doneRef.current || cancelledRef.current) return;
        if (health.version && targetTagRef.current) {
          if (
            resolveRestartOutcome({
              healthVersion: health.version,
              targetTag: targetTagRef.current,
              sawServerDown: sawServerDownRef.current,
            }) === "succeeded"
          ) {
            finish("succeeded");
          }
          return; // strict signal available — never fall to the heuristic
        }
        // Pre-v1.28 backend (no version on /health) → legacy heuristic.
        try {
          const info = await fetchSystemInfo();
          if (doneRef.current || cancelledRef.current) return;
          const verdict = detectUpgradeOutcome({
            infoCurrent: info.current,
            infoAvailable: info.available,
            currentBefore: currentBeforeRef.current,
            sawServerDown: sawServerDownRef.current,
          });
          if (verdict === "succeeded") finish("succeeded");
        } catch {
          /* /info needs auth mid-restart — /health above is the liveness
             marker, so nothing to record here */
        }
      } catch {
        // /health unreachable → the gateway is restarting. Record it so
        // the next successful probe is read as "came back up".
        if (!sawServerDownRef.current) {
          sawServerDownRef.current = true;
          setRestarting(true);
        }
      }
    }, RECONNECT_POLL_MS);

    slowTimer = window.setTimeout(() => {
      if (!doneRef.current && !cancelledRef.current) setSlow(true);
    }, SLOW_AFTER_MS);

    return () => {
      cancelledRef.current = true;
      stopAll();
      stopAllRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestId, finish]);

  // Real abort first (v1.28 backend cancels queued/pulling work); when
  // the upgrade is past the point of no return (409) — or the backend
  // predates the endpoint — degrade to stop-watching, as before.
  const handleCancel = React.useCallback(async () => {
    try {
      await cancelUpgrade(requestId);
      toast.success(t("system.upgrade.cancelled.aborted"));
    } catch (err) {
      if (err instanceof CorlinmanApiError && err.status === 409) {
        toast.info(t("system.upgrade.cancelled.tooLate"));
      }
      /* any other error (404 fresh process, old backend) → just stop
         watching, matching the pre-v1.28 behaviour */
    }
    cancelledRef.current = true;
    stopAllRef.current?.();
    setCancelled(true);
  }, [requestId, t]);

  const elapsed = Math.max(0, Math.floor((now - startedAt) / 1000));
  const pending = outcome === "pending" && !cancelled;

  return (
    <section
      data-testid="upgrade-progress"
      className="space-y-4 rounded-lg border border-sg-border bg-sg-card p-4 sm:p-6"
    >
      <header className="flex items-center justify-between">
        <h2 className="text-lg font-semibold tracking-tight">
          {outcome === "succeeded"
            ? t("system.upgrade.succeeded.title")
            : outcome === "failed"
              ? t("system.upgrade.failed.title")
              : cancelled
                ? t("system.upgrade.cancelled.title")
                : t("system.upgrade.progress.title")}
        </h2>
        {pending ? (
          <span className="font-mono text-xs text-sg-ink-3">
            {t("system.upgrade.progress.elapsed", { seconds: elapsed })}
          </span>
        ) : null}
      </header>

      {/* Indeterminate spinner — no progress bar. */}
      {pending ? (
        <div
          role="status"
          aria-live="polite"
          data-testid="upgrade-progress-spinner"
          className="flex items-center gap-3"
        >
          <Loader2
            className="h-5 w-5 shrink-0 animate-spin text-sg-accent"
            aria-hidden
          />
          <div className="space-y-0.5">
            <p className="text-sm font-medium text-sg-ink">
              {restarting
                ? targetLabel
                  ? t("system.upgrade.progress.waitingVersion", {
                      version: stripV(targetLabel),
                    })
                  : t("system.upgrade.progress.restarting")
                : t("system.upgrade.progress.title")}
            </p>
            <p className="text-xs text-sg-ink-3">
              {slow
                ? t("system.upgrade.progress.slow")
                : t("system.upgrade.progress.subtitle")}
            </p>
          </div>
        </div>
      ) : null}

      {/* Log tail (best-effort). */}
      {logExcerpt ? (
        <pre
          data-testid="upgrade-progress-log"
          className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md border border-sg-border bg-sg-inset p-3 font-mono text-[11px] text-sg-ink-2"
        >
          {logExcerpt}
        </pre>
      ) : null}

      {/* Terminal banners */}
      {outcome === "succeeded" ? (
        <Alert
          variant="success"
          title={t("system.upgrade.succeeded.title")}
        >
          <p className="text-xs">
            {t("system.upgrade.succeeded.reloading")}
          </p>
        </Alert>
      ) : null}

      {outcome === "failed" ? (
        <Alert variant="danger" title={t("system.upgrade.failed.title")}>
          <p className="break-words text-xs">
            {t("system.upgrade.failed.subtitle", {
              error: error ?? "unknown",
            })}
          </p>
        </Alert>
      ) : null}

      {cancelled ? (
        <p className="text-xs text-sg-ink-3">
          {t("system.upgrade.cancelled.subtitle")}
        </p>
      ) : null}

      {/* Footer actions while pending: reload-now (after slow) + stop-watching. */}
      {pending ? (
        <div className="flex items-center justify-end gap-2">
          {slow ? (
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="gap-1.5"
              onClick={() => window.location.reload()}
              data-testid="upgrade-progress-reload"
            >
              <RefreshCcw className="h-3.5 w-3.5" aria-hidden />
              {t("system.upgrade.progress.reloadNow")}
            </Button>
          ) : null}
          <Button
            type="button"
            variant="ghost"
            size="sm"
            title={t("system.upgrade.progress.cancelHint")}
            onClick={handleCancel}
            data-testid="upgrade-progress-cancel"
          >
            {t("system.upgrade.progress.cancel")}
          </Button>
        </div>
      ) : null}
    </section>
  );
}
