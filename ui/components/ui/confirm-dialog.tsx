"use client";

/**
 * AlertDialog-equivalent built on the existing `@radix-ui/react-dialog`
 * primitive so we don't need to add `@radix-ui/react-alert-dialog` as a
 * new dependency. Renders a focus-trapped modal with a title, description,
 * and two action buttons (cancel + confirm). The confirm button defaults
 * to the destructive variant — every consumer so far is a destructive
 * action (delete one session / clear all sessions).
 */

import * as React from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: React.ReactNode;
  description: React.ReactNode;
  confirmLabel: React.ReactNode;
  cancelLabel: React.ReactNode;
  onConfirm: () => void | Promise<void>;
  /** When false, the dialog renders a default `default` confirm button. */
  destructive?: boolean;
  /** Optional test id for the confirm button (cancel mirrors `${testId}-cancel`). */
  testId?: string;
  busy?: boolean;
}

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  cancelLabel,
  onConfirm,
  destructive = true,
  testId,
  busy = false,
}: ConfirmDialogProps) {
  const [running, setRunning] = React.useState(false);
  const inFlight = busy || running;

  async function handleConfirm() {
    setRunning(true);
    try {
      await onConfirm();
    } finally {
      setRunning(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        role="alertdialog"
        aria-modal="true"
        data-testid={testId ? `${testId}-content` : undefined}
      >
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription className="whitespace-pre-line text-sm text-tp-ink-3">
            {description}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter className="gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => onOpenChange(false)}
            disabled={inFlight}
            data-testid={testId ? `${testId}-cancel` : undefined}
          >
            {cancelLabel}
          </Button>
          <Button
            type="button"
            variant={destructive ? "destructive" : "default"}
            size="sm"
            onClick={handleConfirm}
            disabled={inFlight}
            data-testid={testId ? `${testId}-confirm` : undefined}
          >
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
