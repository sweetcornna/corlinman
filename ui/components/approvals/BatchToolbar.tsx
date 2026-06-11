"use client";

import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";

/**
 * Batch-action toolbar — pins to the viewport bottom when the operator has
 * selection. Tidepool (Phase 5a) refresh: glass surface, amber primary, ember
 * destructive, muted clear affordance. Keyboard hint appears on the clear
 * button (⌫).
 *
 * The component stays dumb: caller owns the selection set, this only shows
 * counts and fires callbacks.
 */
export interface BatchToolbarProps {
  selectedCount: number;
  onApproveAll: () => void;
  onDenyAll: () => void;
  onClear: () => void;
  disabled?: boolean;
}

export function BatchToolbar({
  selectedCount,
  onApproveAll,
  onDenyAll,
  onClear,
  disabled = false,
}: BatchToolbarProps) {
  const { t } = useTranslation();
  if (selectedCount === 0) return null;
  return (
    <div
      role="region"
      aria-label={t("approvals.batchActionsAria")}
      className={cn(
        "sticky bottom-4 z-20 mx-auto flex w-full max-w-3xl items-center gap-3",
        "rounded-full border border-sg-border bg-sg-card-strong",
        "px-4 py-2.5 text-[13px] text-sg-ink-2",
        " shadow-sg-3",
      )}
    >
      <span
        className={cn(
          "inline-flex items-center gap-2 font-mono text-[11px] tracking-wide",
          "rounded-full border border-sg-accent/25 bg-sg-accent-soft px-2.5 py-[3px] text-sg-accent",
        )}
      >
        <span className="h-1.5 w-1.5 rounded-full bg-sg-accent" aria-hidden />
        {t("approvals.tp.batchBarLabel", { n: selectedCount })}
      </span>
      <div className="flex-1" />
      <button
        type="button"
        onClick={onApproveAll}
        disabled={disabled}
        aria-label={t("approvals.batchApprove")}
        className={cn(
          "inline-flex items-center gap-2 rounded-full px-3.5 py-1.5 text-[12.5px] font-medium",
          "bg-sg-accent text-primary-foreground shadow-sg-primary",
          "transition-transform duration-150 hover:-translate-y-[1px]",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/50",
          "disabled:pointer-events-none disabled:opacity-50",
        )}
      >
        {t("approvals.tp.batchBarApprove", { n: selectedCount })}
      </button>
      <button
        type="button"
        onClick={onDenyAll}
        disabled={disabled}
        aria-label={t("approvals.batchDeny")}
        className={cn(
          "inline-flex items-center gap-2 rounded-full border px-3.5 py-1.5 text-[12.5px] font-medium",
          "border-sg-err/40 bg-sg-err-soft text-sg-err",
          "hover:bg-[color-mix(in_oklch,var(--sg-err)_14%,transparent)]",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-err/40",
          "disabled:pointer-events-none disabled:opacity-50",
        )}
      >
        {t("approvals.tp.batchBarDeny", { n: selectedCount })}
      </button>
      <button
        type="button"
        onClick={onClear}
        disabled={disabled}
        aria-label={t("approvals.clear")}
        className={cn(
          "inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-[12px]",
          "bg-sg-inset text-sg-ink-3 hover:bg-sg-inset-hover hover:text-sg-ink",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40",
          "disabled:pointer-events-none disabled:opacity-50",
        )}
      >
        <kbd className="font-mono text-[10px] text-sg-ink-4">
          {t("approvals.tp.batchHintKey")}
        </kbd>
        {t("approvals.clear")}
      </button>
    </div>
  );
}
