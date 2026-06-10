"use client";

import { Inbox, History } from "lucide-react";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import type { Tab } from "./types";

/** Friendly empty state for the approvals list.
 *
 * Tidepool (Phase 5a) refresh: retuned to the warm-glass aesthetic. Text
 * content unchanged so the existing EmptyState test remains authoritative.
 */
export function ApprovalsEmptyState({ tab }: { tab: Tab }) {
  const { t } = useTranslation();
  if (tab === "pending") {
    return (
      <div
        className={cn(
          "flex flex-col items-center justify-center gap-2.5 py-12 text-center",
          "rounded-2xl border border-dashed border-sg-border bg-sg-inset/40",
        )}
      >
        <Inbox className="h-8 w-8 text-sg-ink-4" aria-hidden />
        <p className="font-sans text-[14px] font-medium text-sg-ink">
          {t("approvals.emptyPendingTitle")}
        </p>
        <p className="max-w-[36ch] text-[12px] text-sg-ink-3">
          {t("approvals.emptyPendingHint")}
        </p>
      </div>
    );
  }
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-2.5 py-12 text-center",
        "rounded-2xl border border-dashed border-sg-border bg-sg-inset/40",
      )}
    >
      <History className="h-8 w-8 text-sg-ink-4" aria-hidden />
      <p className="font-sans text-[14px] font-medium text-sg-ink">
        {t("approvals.emptyHistoryTitle")}
      </p>
      <p className="max-w-[36ch] text-[12px] text-sg-ink-3">
        {t("approvals.emptyHistoryHint")}
      </p>
    </div>
  );
}
