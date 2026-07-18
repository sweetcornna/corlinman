"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { formatNumber } from "@/lib/format";

/**
 * Single-line summary strip below the control bar.
 *
 * "1,842 events · [1,412 ok] [378 info] [38 warn] [14 err] · across 9 subsystems · 3 unique trace_ids"
 *
 * Values are pre-computed by the page from the current visible/ring set.
 * Chips read tone from the same --sg-{ok|warn|err} palette as the rest
 * of the Logs surface.
 */

export interface LogStatsStripProps {
  total: number;
  ok: number;
  info: number;
  warn: number;
  err: number;
  subsystems: number;
  traceIds: number;
  className?: string;
}

export function LogStatsStrip(props: LogStatsStripProps) {
  const { t } = useTranslation();
  const {
    total,
    ok,
    info,
    warn,
    err,
    subsystems,
    traceIds,
    className,
  } = props;

  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        "flex flex-wrap items-center gap-x-5 gap-y-2 px-4",
        "text-[12.5px] text-sg-ink-3",
        className,
      )}
    >
      <span>
        <b className="font-medium tabular-nums text-sg-ink">
          {formatNumber(total)}
        </b>{" "}
        {t("logs.tp.statEvents")}
      </span>
      <span className="text-sg-ink-5">·</span>
      <Chip tone="ok" count={ok} label={t("logs.tp.sevOk")} />
      <Chip tone="info" count={info} label={t("logs.tp.sevInfo")} />
      <Chip tone="warn" count={warn} label={t("logs.tp.sevWarn")} />
      <Chip tone="err" count={err} label={t("logs.tp.sevErr")} />
      <span className="text-sg-ink-5">·</span>
      <span>
        {t("logs.tp.statAcross")}{" "}
        <b className="font-medium tabular-nums text-sg-ink">{subsystems}</b>{" "}
        {t("logs.tp.statSubsystems")}
      </span>
      <span className="text-sg-ink-5">·</span>
      <span>
        <b className="font-medium tabular-nums text-sg-ink">{traceIds}</b>{" "}
        {t("logs.tp.statTraceIds")}
      </span>
    </div>
  );
}

function Chip({
  tone,
  count,
  label,
}: {
  tone: "ok" | "info" | "warn" | "err";
  count: number;
  label: string;
}) {
  const dotClass: Record<typeof tone, string> = {
    ok: "bg-sg-ok",
    info: "bg-sg-ink-4",
    warn: "bg-sg-warn",
    err: "bg-sg-err",
  };
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-[2px]",
        "bg-sg-inset border-sg-border font-mono text-[11px] text-sg-ink-3",
      )}
    >
      <span aria-hidden className={cn("h-[5px] w-[5px] rounded-full", dotClass[tone])} />
      <span className="tabular-nums">{formatNumber(count)}</span>
      <span>{label}</span>
    </span>
  );
}

export default LogStatsStrip;
