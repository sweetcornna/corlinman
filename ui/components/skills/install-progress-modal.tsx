"use client";

/**
 * `<InstallProgressModal>` — drives a single hub-install flow (W2.2).
 *
 * Lifecycle:
 *   1. mount → POST `/admin/skills/hub/install` (`postHubInstall`)
 *   2. open SSE via `streamHubInstallEvents(request_id)` and render the
 *      phase progression as a 3-stage bar:
 *          download.started → extract.started → installed
 *   3. on terminal `state === "installed"` → toast.success, invalidate the
 *      `["skills"]` query so the Installed tab refetches, and close.
 *   4. on `state === "failed"` → render the error inline + Retry button
 *      that re-runs the POST (new request_id).
 *
 * The modal mirrors `<UpgradeProgress>` (system upgrade SSE pattern) but
 * collapses the phase set to the three the install pipeline guarantees.
 */

import * as React from "react";
import { useTranslation } from "react-i18next";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { CheckCircle2, Circle, Loader2, XCircle } from "lucide-react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  CorlinmanApiError,
  postHubInstall,
  streamHubInstallEvents,
  type HubInstallStatusOut,
} from "@/lib/api";

/** Three install phases the backend pipeline guarantees, in order. */
export const INSTALL_PHASES = [
  "download.started",
  "extract.started",
  "installed",
] as const;
type Phase = (typeof INSTALL_PHASES)[number];

function isKnownPhase(p: string): p is Phase {
  return (INSTALL_PHASES as readonly string[]).includes(p);
}

const TERMINAL_STATES = new Set<HubInstallStatusOut["state"]>([
  "installed",
  "failed",
]);

export interface InstallProgressModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Hub slug to install. */
  slug: string;
  /** Optional skill display name (drives the title copy). */
  name?: string;
  /** Optional pinned version. Omit → backend installs latest. */
  version?: string;
  /** Optional profile slug. Omit → backend installs to active. */
  profile?: string;
}

type ModalState =
  | { kind: "starting" }
  | { kind: "running"; requestId: string; status: HubInstallStatusOut | null }
  | { kind: "done"; status: HubInstallStatusOut }
  | { kind: "failed"; error: string; status?: HubInstallStatusOut };

export function InstallProgressModal({
  open,
  onOpenChange,
  slug,
  name,
  version,
  profile,
}: InstallProgressModalProps): React.JSX.Element {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [state, setState] = React.useState<ModalState>({ kind: "starting" });
  const esRef = React.useRef<EventSource | null>(null);
  const closedRef = React.useRef(false);

  // The kick-off: POST install → open SSE. Re-runs when the user hits
  // Retry (we increment an `attempt` counter to retrigger the effect).
  const [attempt, setAttempt] = React.useState(0);

  React.useEffect(() => {
    if (!open) return;
    closedRef.current = false;
    setState({ kind: "starting" });

    let cancelled = false;
    (async () => {
      try {
        const { request_id } = await postHubInstall({
          slug,
          version,
          profile,
        });
        if (cancelled) return;
        setState({ kind: "running", requestId: request_id, status: null });

        const es = streamHubInstallEvents(request_id, (frame) => {
          if (closedRef.current) return;
          setState((prev) => {
            // Keep accumulating into running until we see terminal.
            if (TERMINAL_STATES.has(frame.state)) {
              closedRef.current = true;
              esRef.current?.close();
              if (frame.state === "installed") {
                toast.success(
                  t("skills.hub.install.toastSuccess", {
                    name: frame.name ?? slug,
                  }),
                );
                qc.invalidateQueries({ queryKey: ["skills"] });
                return { kind: "done", status: frame };
              }
              return {
                kind: "failed",
                error: frame.error ?? t("skills.hub.install.errorUnknown"),
                status: frame,
              };
            }
            return {
              kind: "running",
              requestId: request_id,
              status: frame,
            };
          });
        });
        esRef.current = es;
        // Best-effort hard-failure surfacing when SSE itself errors out
        // (network blip, gateway 500). The browser will auto-reconnect
        // for the recoverable case; this listener exists for the case
        // where the stream closes without a terminal frame.
        es.addEventListener("error", () => {
          if (closedRef.current) return;
          // Don't flip to failed on transient errors — only if the
          // EventSource enters the CLOSED readyState (browser gave up).
          if (es.readyState === 2 /* CLOSED */) {
            closedRef.current = true;
            setState({
              kind: "failed",
              error: t("skills.hub.install.errorStream"),
            });
          }
        });
      } catch (e) {
        if (cancelled) return;
        const msg =
          e instanceof CorlinmanApiError
            ? `${e.status ?? "?"} · ${e.message}`
            : e instanceof Error
              ? e.message
              : String(e);
        setState({ kind: "failed", error: msg });
      }
    })();

    return () => {
      cancelled = true;
      closedRef.current = true;
      esRef.current?.close();
      esRef.current = null;
    };
    // attempt is part of the dep array so Retry kicks the effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, slug, version, profile, attempt]);

  const close = React.useCallback(() => {
    onOpenChange(false);
  }, [onOpenChange]);

  const retry = React.useCallback(() => {
    setAttempt((n) => n + 1);
  }, []);

  const currentPhase: string | null = (() => {
    if (state.kind === "running" && state.status) return state.status.phase;
    if (state.kind === "done") return "installed";
    if (state.kind === "failed" && state.status) return state.status.phase;
    return null;
  })();

  const isTerminal = state.kind === "done" || state.kind === "failed";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-w-md"
        data-testid="install-progress-modal"
      >
        <DialogHeader>
          <DialogTitle>
            {state.kind === "done"
              ? t("skills.hub.install.titleDone")
              : state.kind === "failed"
                ? t("skills.hub.install.titleFailed")
                : t("skills.hub.install.titleRunning")}
          </DialogTitle>
          <DialogDescription>
            {t("skills.hub.install.subtitle", { name: name ?? slug })}
          </DialogDescription>
        </DialogHeader>

        {/* 3-stage progress bar */}
        <div
          className="flex flex-col gap-2"
          data-testid="install-progress-phases"
        >
          {INSTALL_PHASES.map((phase) => {
            const phaseIdx = INSTALL_PHASES.indexOf(phase);
            const currentIdx =
              currentPhase && isKnownPhase(currentPhase)
                ? INSTALL_PHASES.indexOf(currentPhase)
                : -1;
            const isCurrent =
              currentPhase === phase && !isTerminal;
            const isPast =
              currentIdx > phaseIdx ||
              (state.kind === "done" && phase !== "installed") ||
              (state.kind === "done" && phase === "installed");
            const isFailed =
              state.kind === "failed" && currentPhase === phase;

            return (
              <div
                key={phase}
                data-testid={`install-phase-${phase}`}
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
                  "inline-flex items-center gap-2 rounded-md border px-3 py-2 text-sm",
                  isFailed && "border-red-500/60 bg-red-500/10 text-red-600",
                  isPast &&
                    !isFailed &&
                    "border-emerald-500/40 bg-emerald-500/10 text-emerald-700",
                  isCurrent &&
                    "border-tp-amber/60 bg-tp-amber/10 text-tp-amber",
                  !isFailed &&
                    !isPast &&
                    !isCurrent &&
                    "border-tp-glass-edge text-tp-ink-3",
                )}
              >
                {isCurrent ? (
                  <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                ) : isPast && !isFailed ? (
                  <CheckCircle2 className="h-4 w-4" aria-hidden />
                ) : isFailed ? (
                  <XCircle className="h-4 w-4" aria-hidden />
                ) : (
                  <Circle className="h-4 w-4" aria-hidden />
                )}
                {t(`skills.hub.install.phase.${phase}`)}
              </div>
            );
          })}
        </div>

        {/* Inline status / message rail */}
        {state.kind === "running" && state.status?.message ? (
          <p
            className="text-xs text-tp-ink-3"
            data-testid="install-progress-message"
          >
            {state.status.message}
          </p>
        ) : null}

        {state.kind === "failed" ? (
          <div
            role="alert"
            className="rounded-md border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-700"
            data-testid="install-progress-error"
          >
            <p className="font-medium">{t("skills.hub.install.errorTitle")}</p>
            <p className="break-words text-xs">{state.error}</p>
          </div>
        ) : null}

        <DialogFooter>
          {state.kind === "failed" ? (
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={retry}
              data-testid="install-progress-retry"
            >
              {t("skills.hub.install.retry")}
            </Button>
          ) : null}
          <Button
            type="button"
            variant={state.kind === "done" ? "default" : "outline"}
            size="sm"
            onClick={close}
            data-testid="install-progress-close"
          >
            {state.kind === "done"
              ? t("skills.hub.install.done")
              : t("skills.hub.install.close")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default InstallProgressModal;
