"use client";

import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { DetailDrawer } from "@/components/ui/detail-drawer";
import type { SchedulerHistory, SchedulerJob } from "@/lib/api";
import { formatRelative, type SchedulerStatus } from "./scheduler-util";

/**
 * Right-rail drawer for the Scheduler list — surfaces the selected job's
 * summary plus its history log. The history query is already a thin
 * snapshot on the gateway (it returns the recent-attempts ring), so we
 * filter client-side by `job.name` to keep the wire format as-is.
 *
 * Closing delegates to the parent (same "click the selected row again"
 * pattern as Hooks / Approvals).
 */

export interface SchedulerHistoryDrawerProps {
  job: SchedulerJob;
  status: SchedulerStatus;
  history: SchedulerHistory[];
  className?: string;
}

export function SchedulerHistoryDrawer({
  job,
  status,
  history,
  className,
}: SchedulerHistoryDrawerProps) {
  const { t } = useTranslation();

  const meta = (
    <>
      <StatusPill status={status} />
      <span className="font-mono text-[11px] text-sg-ink-3">
        {job.action_kind}
      </span>
      {job.timezone ? (
        <span className="font-mono text-[11px] text-sg-ink-4">
          · {job.timezone}
        </span>
      ) : null}
    </>
  );

  return (
    <DetailDrawer
      title={<span data-testid="scheduler-drawer-name">{job.name}</span>}
      subsystem={job.cron}
      meta={meta}
      className={className}
    >
      <DetailDrawer.Section label={t("scheduler.tp.sectionSchedule")}>
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 text-[12.5px]">
          <dt className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-sg-ink-4">
            {t("scheduler.tp.scheduleNext")}
          </dt>
          <dd className="font-mono tabular-nums text-sg-ink-2">
            {job.next_fire_at ?? t("scheduler.tp.scheduleNone")}
          </dd>
          <dt className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-sg-ink-4">
            {t("scheduler.tp.scheduleLast")}
          </dt>
          <dd className="font-mono text-sg-ink-2">
            {job.last_status ?? t("scheduler.tp.scheduleNoLast")}
          </dd>
        </dl>
      </DetailDrawer.Section>

      <DetailDrawer.Section label={t("scheduler.tp.sectionHistory")}>
        {history.length === 0 ? (
          <div
            className={cn(
              "rounded-lg border border-dashed border-sg-border",
              "bg-sg-inset p-4 text-center",
              "font-mono text-[11.5px] text-sg-ink-4",
            )}
          >
            {t("scheduler.tp.historyEmpty")}
          </div>
        ) : (
          <ol className="flex flex-col gap-1.5" data-testid="scheduler-history-list">
            {history.map((h, i) => (
              <li
                key={`${h.at}-${i}`}
                className={cn(
                  "rounded-lg border border-sg-border bg-sg-inset px-3 py-2",
                  "flex flex-col gap-1",
                )}
              >
                <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                  <HistoryStatus status={h.status} />
                  <span className="font-mono text-[10.5px] text-sg-ink-3">
                    {h.source}
                  </span>
                  <span className="ml-auto font-mono text-[10.5px] text-sg-ink-4">
                    {formatRelative(h.at, t)}
                  </span>
                </div>
                <div className="font-mono text-[11px] text-sg-ink-4">{h.at}</div>
                {h.message ? (
                  <div className="text-[12px] text-sg-ink-2">{h.message}</div>
                ) : null}
              </li>
            ))}
          </ol>
        )}
      </DetailDrawer.Section>
    </DetailDrawer>
  );
}

function StatusPill({ status }: { status: SchedulerStatus }) {
  const { t } = useTranslation();
  const cls =
    status === "errored"
      ? "border-sg-err/35 bg-sg-err-soft text-sg-err"
      : status === "paused"
        ? "border-sg-border bg-sg-inset text-sg-ink-3"
        : "border-sg-ok/30 bg-sg-ok-soft text-sg-ok";
  const label =
    status === "errored"
      ? t("scheduler.tp.filterErrored")
      : status === "paused"
        ? t("scheduler.tp.filterPaused")
        : t("scheduler.tp.filterEnabled");
  return (
    <span
      className={cn(
        "rounded-md border px-2 py-[2px]",
        "font-mono text-[10px] font-medium tracking-[0.04em]",
        cls,
      )}
    >
      {label}
    </span>
  );
}

function HistoryStatus({ status }: { status: string }) {
  const s = status.toLowerCase();
  const cls =
    s.includes("ok") || s.includes("success")
      ? "text-sg-ok"
      : s.includes("err") || s.includes("fail")
        ? "text-sg-err"
        : "text-sg-ink-3";
  return (
    <span className={cn("font-mono text-[11px] font-medium", cls)}>{status}</span>
  );
}

export default SchedulerHistoryDrawer;
