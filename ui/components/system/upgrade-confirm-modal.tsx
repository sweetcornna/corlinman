"use client";

/**
 * `<UpgradeConfirmModal>` — typed-confirmation dialog that gates the
 * one-click upgrade behind explicit operator intent.
 *
 * Layout:
 *   - Title with the target tag substituted
 *   - Current version line
 *   - Amber warning callout (restart impending)
 *   - Optional release-notes excerpt (2 lines max)
 *   - Text input — operator must type the tag exactly; submit stays
 *     disabled otherwise
 *   - Cancel + Upgrade buttons; mid-flight POST renders a "Starting…"
 *     state; 409 surfaces the in-flight info inline and leaves the
 *     modal open
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
import { Input } from "@/components/ui/input";
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
  const [typed, setTyped] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [inFlight, setInFlight] = React.useState<InFlightInfo | null>(null);

  // Reset transient state when the modal toggles open.
  React.useEffect(() => {
    if (open) {
      setTyped("");
      setSubmitting(false);
      setInFlight(null);
    }
  }, [open]);

  const matches = typed === tag;
  const submitDisabled = !matches || submitting;

  async function handleSubmit() {
    if (!matches || submitting) return;
    setSubmitting(true);
    setInFlight(null);
    try {
      const res = await startSystemUpgrade(tag, typed);
      onUpgradeStarted(res);
      onOpenChange(false);
    } catch (err) {
      if (err instanceof CorlinmanApiError && err.status === 409) {
        setInFlight(parseInFlight(err));
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
          <p className="line-clamp-2 text-xs text-tp-ink-3">
            {releaseNotesExcerpt}
          </p>
        ) : null}

        <div className="space-y-1.5">
          <label
            htmlFor="upgrade-confirm-input"
            className="text-xs font-medium uppercase tracking-wide text-tp-ink-3"
          >
            {t("system.upgrade.confirm.typeLabel", { tag })}
          </label>
          <Input
            id="upgrade-confirm-input"
            data-testid="upgrade-confirm-input"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder={t("system.upgrade.confirm.typePlaceholder", { tag })}
            autoFocus
            autoComplete="off"
            spellCheck={false}
            className="font-mono"
          />
        </div>

        {inFlight ? (
          <Alert variant="danger" data-testid="upgrade-confirm-conflict">
            {t("system.upgrade.confirm.alreadyRunning", {
              tag: inFlight.tag ?? "?",
            })}
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
            disabled={submitDisabled}
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
