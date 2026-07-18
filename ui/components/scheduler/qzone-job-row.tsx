"use client";

/**
 * `<QzoneJobRow>` — one `qzone.daily_publish` scheduler job rendered as a
 * table row.
 *
 * Factored out of the qzone scheduler page's inline row so the page rework
 * (PR-F4) can compose it, and extended from the page's single "run now"
 * button to the full operator action cluster: run now / edit / pause·resume
 * / delete. Every action is a callback prop (`on*`) taking the job name —
 * this component holds no mutation state of its own.
 *
 * `source === "config"` jobs come from `[[scheduler.jobs]]` TOML and are
 * read-only here: edit / toggle / delete are disabled with a tooltip
 * (operators edit those in the config file). "Run now" stays enabled for
 * every row. `source === "runtime"` (operator-created) rows are fully
 * interactive. The displayed columns mirror the existing page row (name ·
 * persona · cron · state · last run · actions).
 */

import * as React from "react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { TableCell, TableRow } from "@/components/ui/table";
import { ExternalLink, Pause, Pencil, Play, Trash2 } from "@/components/icons";
import { formatDateTime } from "@/lib/format";
import type { SchedulerJobRow } from "@/lib/api/scheduler";

export interface QzoneJobRowProps {
  job: SchedulerJobRow;
  /** Fire the job now (enabled for every row). */
  onTrigger: (name: string) => void;
  /** Open the edit form for a runtime job. */
  onEdit: (name: string) => void;
  /** Flip enabled ↔ paused for a runtime job. */
  onToggleEnabled: (name: string) => void;
  /** Delete a runtime job. */
  onDelete: (name: string) => void;
  /** True while a manual "run now" is in flight for this row. */
  triggering?: boolean;
}

export function QzoneJobRow({
  job,
  onTrigger,
  onEdit,
  onToggleEnabled,
  onDelete,
  triggering = false,
}: QzoneJobRowProps) {
  const { t } = useTranslation();
  // Config-derived rows come from TOML and can't be mutated from the UI.
  const isConfig = job.source === "config";
  const configTitle = t("schedulerQzone.row.configReadonly");
  const lastRunDate =
    job.last_run_at_ms !== null && job.last_run_at_ms !== undefined
      ? new Date(job.last_run_at_ms)
      : null;

  return (
    <TableRow
      data-testid={`qzone-job-row-${job.name}`}
      data-source={job.source ?? "runtime"}
    >
      <TableCell className="font-mono text-xs">{job.name}</TableCell>
      <TableCell>{job.persona_id ?? "—"}</TableCell>
      <TableCell className="font-mono text-xs">{job.cron}</TableCell>
      <TableCell>
        {job.enabled ? (
          <Badge variant="default">{t("schedulerQzone.row.enabled")}</Badge>
        ) : (
          <Badge variant="secondary">{t("schedulerQzone.row.paused")}</Badge>
        )}
      </TableCell>
      <TableCell className="space-y-1 text-xs">
        {lastRunDate === null ? (
          <span className="text-sg-ink-4">
            {t("schedulerQzone.row.neverRun")}
          </span>
        ) : (
          <>
            <div className="flex items-center gap-1.5">
              {job.last_run_ok ? (
                <Badge variant="default">{t("schedulerQzone.row.ok")}</Badge>
              ) : (
                <Badge variant="destructive">
                  {t("schedulerQzone.row.error")}
                </Badge>
              )}
              <span className="text-sg-ink-4">{formatDateTime(lastRunDate)}</span>
            </div>
            {job.last_run_ok && job.last_qzone_url ? (
              <a
                href={job.last_qzone_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-sg-accent hover:underline"
              >
                <ExternalLink className="h-3 w-3" aria-hidden />
                {t("schedulerQzone.row.viewQzone")}
              </a>
            ) : null}
            {!job.last_run_ok && job.last_error ? (
              <p className="text-sg-err">{job.last_error}</p>
            ) : null}
          </>
        )}
      </TableCell>
      <TableCell className="text-right">
        <div className="flex items-center justify-end gap-1">
          <IconButton
            label={t("schedulerQzone.row.runNow")}
            onClick={() => onTrigger(job.name)}
            disabled={triggering}
            testId={`qzone-job-trigger-${job.name}`}
          >
            <Play
              className={cn("h-3.5 w-3.5", triggering && "animate-pulse")}
              aria-hidden
            />
          </IconButton>
          <IconButton
            label={t("schedulerQzone.row.edit")}
            onClick={() => onEdit(job.name)}
            disabled={isConfig}
            title={isConfig ? configTitle : t("schedulerQzone.row.edit")}
            testId={`qzone-job-edit-${job.name}`}
          >
            <Pencil className="h-3.5 w-3.5" aria-hidden />
          </IconButton>
          <IconButton
            label={
              job.enabled
                ? t("schedulerQzone.row.pause")
                : t("schedulerQzone.row.resume")
            }
            onClick={() => onToggleEnabled(job.name)}
            disabled={isConfig}
            title={
              isConfig
                ? configTitle
                : job.enabled
                  ? t("schedulerQzone.row.pause")
                  : t("schedulerQzone.row.resume")
            }
            testId={`qzone-job-toggle-${job.name}`}
          >
            {job.enabled ? (
              <Pause className="h-3.5 w-3.5" aria-hidden />
            ) : (
              <Play className="h-3.5 w-3.5" aria-hidden />
            )}
          </IconButton>
          <IconButton
            label={t("schedulerQzone.row.delete")}
            onClick={() => onDelete(job.name)}
            disabled={isConfig}
            title={isConfig ? configTitle : t("schedulerQzone.row.delete")}
            testId={`qzone-job-delete-${job.name}`}
          >
            <Trash2 className="h-3.5 w-3.5" aria-hidden />
          </IconButton>
        </div>
      </TableCell>
    </TableRow>
  );
}

// ─── Icon button primitive (mirrors scheduler-row's) ─────────────────────

interface IconButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  label: string;
  testId?: string;
}

function IconButton({
  label,
  testId,
  disabled,
  className,
  children,
  title,
  ...rest
}: IconButtonProps) {
  return (
    <button
      type="button"
      aria-label={label}
      title={title ?? label}
      data-testid={testId}
      disabled={disabled}
      className={cn(
        "inline-flex h-7 w-7 items-center justify-center rounded-md",
        "text-sg-ink-3 transition-colors",
        "hover:bg-sg-inset-hover hover:text-sg-ink",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40",
        "disabled:pointer-events-none disabled:opacity-40",
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}
