"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

/** Controlled deny-with-reason dialog.
 *
 * Required minimum length = 5 chars, matching the copy in the task spec.
 * Reason travels to the Rust `DecideBody { approve: false, reason }` which
 * is stored alongside the decision (see `approvals.rs::decide_approval`).
 *
 * Tidepool (Phase 5a) refresh: glass dialog card with warm-orange primary
 * affordances and ember destructive. API is unchanged so the existing
 * component test stays authoritative.
 */
export interface DenyReasonDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Human label for the action — already localized by the caller. */
  targetLabel: string;
  onConfirm: (reason: string) => void;
  submitting?: boolean;
}

const MIN_REASON = 5;

export function DenyReasonDialog({
  open,
  onOpenChange,
  targetLabel,
  onConfirm,
  submitting = false,
}: DenyReasonDialogProps) {
  const { t } = useTranslation();
  const [reason, setReason] = React.useState("");

  React.useEffect(() => {
    if (!open) setReason("");
  }, [open]);

  const trimmed = reason.trim();
  const tooShort = trimmed.length < MIN_REASON;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className={cn(
          "max-w-md rounded-2xl border-sg-border bg-sg-card-strong p-6",
          "backdrop-blur-glass-strong backdrop-saturate-glass-strong",
          "shadow-sg-3",
        )}
      >
        <DialogHeader>
          <DialogTitle className="font-sans text-[18px] font-medium tracking-[-0.01em] text-sg-ink">
            {t("approvals.denyDialogTitle", { target: targetLabel })}
          </DialogTitle>
          <DialogDescription className="text-[13px] text-sg-ink-3">
            {t("approvals.denyDialogBody", { min: MIN_REASON })}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          <Label
            htmlFor="deny-reason"
            className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-sg-ink-4"
          >
            {t("approvals.denyReasonLabel")}
          </Label>
          <input
            id="deny-reason"
            value={reason}
            autoFocus
            onChange={(e) => setReason(e.target.value)}
            placeholder={t("approvals.denyReasonPlaceholder")}
            disabled={submitting}
            className={cn(
              "flex h-9 w-full rounded-lg border px-3 py-1 text-sm",
              "bg-sg-inset border-sg-border text-sg-ink placeholder:text-sg-ink-4",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/45",
              "disabled:cursor-not-allowed disabled:opacity-50",
            )}
          />
        </div>
        <DialogFooter className="gap-2">
          <button
            type="button"
            onClick={() => onOpenChange(false)}
            disabled={submitting}
            className={cn(
              "inline-flex h-9 items-center justify-center rounded-lg border px-3 text-sm font-medium",
              "border-sg-border bg-sg-inset text-sg-ink-2",
              "hover:bg-sg-inset-hover hover:text-sg-ink",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40",
              "disabled:pointer-events-none disabled:opacity-50",
            )}
          >
            {t("approvals.denyCancel")}
          </button>
          <button
            type="button"
            onClick={() => onConfirm(trimmed)}
            disabled={tooShort || submitting}
            className={cn(
              "inline-flex h-9 items-center justify-center rounded-lg border px-3 text-sm font-medium",
              "border-sg-err/40 bg-sg-err-soft text-sg-err",
              "hover:bg-[color-mix(in_oklch,var(--sg-err)_14%,transparent)]",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-err/50",
              "disabled:pointer-events-none disabled:opacity-50",
            )}
          >
            {t("approvals.denyConfirm")}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
