"use client";

/**
 * `<UpgradeConfirmModal>` — one-click confirm dialog for the upgrade
 * (sub2api-style: a single "Update now" button, no typed-tag friction;
 * the audit log records who clicked).
 *
 * Layout:
 *   - Title with the target tag substituted
 *   - Current → target line
 *   - Amber warning callout (restart impending)
 *   - Optional release-notes excerpt (2 lines max)
 *   - Cancel + Update now buttons; mid-flight POST renders a
 *     "Starting…" state; 409 surfaces the in-flight info inline and
 *     leaves the modal open
 *
 * On a 202 the caller's `onUpgradeStarted(request_id)` fires and the
 * modal closes. The page that mounted it typically routes the user to
 * `?upgrade=<id>` so `<UpgradeProgress>` takes over.
 */

import * as React from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  CorlinmanApiError,
  startSystemUpgrade,
  type UpgradeStartResponse,
} from "@/lib/api";

export interface UpgradeConfirmModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  tag: string; // e.g. "v1.2.1"
  currentVersion: string; // e.g. "1.2.0"
  releaseNotesExcerpt?: string | null;
  onUpgradeStarted: (response: UpgradeStartResponse) => void;
}

interface InFlightInfo {
  request_id?: string;
  tag?: string;
  state?: string;
}

function parseInFlight(err: CorlinmanApiError): InFlightInfo | null {
  // The 409 body shape: { detail?, in_flight: { request_id, tag, state } }.
  // `CorlinmanApiError` stores the raw response body in `err.message` —
  // try to JSON-parse it; tolerate failure (some backends return plain text).
  try {
    const parsed: unknown = JSON.parse(err.message);
    if (parsed && typeof parsed === "object") {
      const inflight = (parsed as { in_flight?: InFlightInfo }).in_flight;
      if (inflight && typeof inflight === "object") return inflight;
    }
  } catch {
    /* not JSON — fall through to null */
  }
  return null;
}

export function UpgradeConfirmModal({
  open,
  onOpenChange,
  tag,
  currentVersion,
  releaseNotesExcerpt,
  onUpgradeStarted,
}: UpgradeConfirmModalProps) {
  const { t } = useTranslation();
  const [submitting, setSubmitting] = React.useState(false);
  const [inFlight, setInFlight] = React.useState<InFlightInfo | null>(null);
  const [unavailable, setUnavailable] = React.useState(false);

  // Reset transient state when the modal toggles open.
  React.useEffect(() => {
    if (open) {
      setSubmitting(false);
      setInFlight(null);
      setUnavailable(false);
    }
  }, [open]);

  async function handleSubmit() {
    if (submitting) return;
    setSubmitting(true);
    setInFlight(null);
    setUnavailable(false);
    try {
      const res = await startSystemUpgrade(tag);
      onUpgradeStarted(res);
      onOpenChange(false);
    } catch (err) {
      if (err instanceof CorlinmanApiError && err.status === 409) {
        setInFlight(parseInFlight(err));
      } else if (
        err instanceof CorlinmanApiError &&
        err.status === 503 &&
        err.message.includes("upgrader_unavailable")
      ) {
        // One-click upgrade isn't wired on this deployment (e.g. a
        // root-owned native box that upgrades via the manual runbook).
        // Surface a clear path to the copy-paste commands instead of a
        // cryptic toast — keep the modal open so the message is read.
        setUnavailable(true);
      } else {
        const msg = err instanceof Error ? err.message : String(err);
        toast.error(msg);
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        data-testid="upgrade-confirm-modal"
        className="max-w-md"
      >
        <DialogHeader>
          <DialogTitle>
            {t("system.upgrade.confirm.title", { tag })}
          </DialogTitle>
          <DialogDescription>
            {t("system.upgrade.confirm.subtitle", { current: currentVersion })}
          </DialogDescription>
        </DialogHeader>

        <Alert variant="warning">{t("system.upgrade.confirm.warning")}</Alert>

        {releaseNotesExcerpt ? (
          <p className="line-clamp-2 text-xs text-sg-ink-3">
            {releaseNotesExcerpt}
          </p>
        ) : null}

        {inFlight ? (
          <Alert variant="danger" data-testid="upgrade-confirm-conflict">
            {t("system.upgrade.confirm.alreadyRunning", {
              tag: inFlight.tag ?? "?",
            })}
          </Alert>
        ) : null}

        {unavailable ? (
          <Alert
            variant="warning"
            title={t("system.upgrade.confirm.unavailableTitle")}
            data-testid="upgrade-confirm-unavailable"
          >
            <p className="text-xs">
              {t("system.upgrade.confirm.unavailableBody")}
            </p>
          </Alert>
        ) : null}

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={submitting}
          >
            {t("system.upgrade.confirm.cancel")}
          </Button>
          <Button
            type="button"
            data-testid="upgrade-confirm-submit"
            onClick={handleSubmit}
            disabled={submitting}
            autoFocus
          >
            {submitting
              ? t("system.upgrade.confirm.submitting")
              : t("system.upgrade.confirm.submit")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
