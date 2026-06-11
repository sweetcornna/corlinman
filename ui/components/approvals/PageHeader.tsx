"use client";

import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";

/**
 * Approvals page header — mirrors the Dashboard hero's quiet prose pattern
 * (big title + one-sentence summary, no glass panel container so the page
 * breathes vertically on tall screens).
 *
 * Two states:
 *   - Pending rows present: `"N tool calls wait for your judgment. Oldest
 *     held 4.3s."` — amber-toned "oldest" reads as mild urgency.
 *   - Queue empty: `"The queue is empty. Agents proceed autonomously for
 *     tool calls on the safelist."`
 *
 * Render the live count inline so it updates on every SSE nudge without
 * requiring a full re-render of the page.
 */
export interface PageHeaderProps {
  pendingCount: number;
  oldestHeldMs: number | null;
}

export function PageHeader({ pendingCount, oldestHeldMs }: PageHeaderProps) {
  const { t } = useTranslation();
  const hasPending = pendingCount > 0;
  return (
    <header className="flex flex-col gap-3">
      <h1
        className={cn(
          "font-sans text-[30px] font-semibold leading-[1.12] tracking-[-0.025em] text-sg-ink",
          "sm:text-[34px]",
        )}
      >
        {t("approvals.tp.heroTitle")}
      </h1>
      <p className="max-w-[58ch] text-[14px] leading-[1.6] text-sg-ink-2">
        {hasPending ? (
          <>
            <InlineMetric tone="warn">
              {t("approvals.tp.heroLead", { n: pendingCount })}
            </InlineMetric>
            {oldestHeldMs !== null ? (
              <span className="ml-1 text-sg-ink-3">
                {t("approvals.tp.heroLeadOldest", {
                  s: Math.max(1, Math.floor(oldestHeldMs / 1000)),
                })}
              </span>
            ) : null}
          </>
        ) : (
          <>
            <span className="text-sg-ink">{t("approvals.tp.heroQuiet")}</span>
            <span className="ml-1 text-sg-ink-3">
              {t("approvals.tp.heroQuietSub")}
            </span>
          </>
        )}
      </p>
    </header>
  );
}

function InlineMetric({
  children,
  tone,
}: {
  children: React.ReactNode;
  tone: "neutral" | "warn";
}) {
  return (
    <span
      className={cn(
        "whitespace-nowrap rounded-md border px-1.5 py-px font-mono text-[12.5px] font-medium tabular-nums",
        tone === "warn"
          ? "border-sg-warn/30 bg-sg-warn-soft text-sg-warn"
          : "border-sg-border bg-sg-inset-strong text-sg-ink",
      )}
    >
      {children}
    </span>
  );
}
