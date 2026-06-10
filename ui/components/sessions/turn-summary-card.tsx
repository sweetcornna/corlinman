/**
 * `TurnSummaryCard` — Phase 4 W2.2 drill-down header.
 *
 * Top-of-page glass card summarising a single turn. Reads the Turn from
 * the timeline store via `useTimeline()`; non-store fields (the user
 * input preview pulled from the `TurnStart` event and the finish reason
 * from `TurnComplete`) come in as props since the reducer doesn't track
 * them.
 *
 * Status badge tones mirror `ToolWidget` (accent=running, ok=done,
 * err=errored, warn=cancelling) so the visual language stays consistent
 * across live and replay views.
 *
 * Missing fields render as an em-dash, never zero — operators read this
 * card to figure out *why* a turn behaved a certain way, so an "0
 * tokens" cell would mis-imply the model returned an empty completion.
 */
"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { AlertTriangle, CircleCheck, Loader2, Octagon } from "lucide-react";

import { useTimeline, type Part, type Turn } from "@/lib/sessions/store";
import { formatCost, formatDuration } from "./cost-footer";

const USER_INPUT_PREVIEW_LIMIT = 200;

export interface TurnSummaryCardProps {
  turnId: string;
  /** Truncated user prompt — extracted from the first `TurnStart` payload. */
  userInput?: string | null;
  /** `stop` / `cancel` / `error` / etc. — from the `TurnComplete` payload. */
  finishReason?: string | null;
}

/** Count the tool-use parts in a turn (for the summary pill). */
function countTools(parts: Part[]): number {
  let n = 0;
  for (const p of parts) {
    if (p.kind === "tool_use") n += 1;
  }
  return n;
}

/** Truncate a long user prompt, preserving trailing ellipsis. */
function truncate(s: string, limit: number): string {
  if (s.length <= limit) return s;
  return `${s.slice(0, limit).trimEnd()}…`;
}

export function TurnSummaryCard({
  turnId,
  userInput,
  finishReason,
}: TurnSummaryCardProps) {
  const { t } = useTranslation();
  const { state } = useTimeline();
  const turn: Turn | undefined = state.turns[turnId];

  // Until the seed dispatch fires the turn may not exist yet; render a
  // skeleton-flavoured placeholder so the layout doesn't jump.
  if (!turn) {
    return (
      <article
        data-testid="turn-summary-card"
        className="animate-pulse rounded-sg-xl border border-sg-border bg-sg-card-grad p-4 shadow-sg-2"
      >
        <div className="h-4 w-24 rounded bg-sg-inset" />
      </article>
    );
  }

  const toolCount = countTools(turn.parts);
  const inputTokens = turn.usage?.input_tokens;
  const outputTokens = turn.usage?.output_tokens;
  const totalTokens =
    inputTokens !== undefined || outputTokens !== undefined
      ? (inputTokens ?? 0) + (outputTokens ?? 0)
      : undefined;
  const elapsedMs =
    turn.endedAt !== undefined ? turn.endedAt - turn.startedAt : undefined;

  const trimmedInput =
    userInput && userInput.trim().length > 0
      ? truncate(userInput.trim(), USER_INPUT_PREVIEW_LIMIT)
      : null;

  return (
    <article
      data-testid="turn-summary-card"
      className="rounded-sg-xl border border-sg-border bg-sg-card-grad p-4 shadow-sg-2"
    >
      <header className="flex items-center gap-2">
        <StatusBadge status={turn.status} />
        <div className="ml-auto flex items-center gap-2 text-[11px] text-sg-ink-4">
          <span className="font-mono text-sg-ink-3">{turnId.slice(0, 12)}</span>
        </div>
      </header>

      <dl className="mt-3 grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
        <Field label={t("sessions.turn.started")}>
          <time
            dateTime={new Date(turn.startedAt).toISOString()}
            title={new Date(turn.startedAt).toLocaleString()}
          >
            {new Date(turn.startedAt).toLocaleTimeString()}
          </time>
        </Field>
        <Field label={t("sessions.turn.elapsed")}>
          {elapsedMs !== undefined ? formatDuration(elapsedMs) : "—"}
        </Field>
        <Field label={t("sessions.turn.tokens")}>
          {totalTokens !== undefined ? (
            <span
              title={
                inputTokens !== undefined && outputTokens !== undefined
                  ? `in ${inputTokens.toLocaleString()} · out ${outputTokens.toLocaleString()}`
                  : undefined
              }
            >
              {totalTokens.toLocaleString()}
            </span>
          ) : (
            "—"
          )}
        </Field>
        <Field label={t("sessions.turn.toolCalls")}>{toolCount}</Field>
        <Field label={t("sessions.cost.total")}>
          {turn.costUsd !== undefined ? formatCost(turn.costUsd) : "—"}
        </Field>
        <Field label={t("sessions.turn.finishReason")}>
          {finishReason ? (
            <span className="font-mono text-sg-accent">{finishReason}</span>
          ) : (
            "—"
          )}
        </Field>
      </dl>

      {trimmedInput && (
        <div className="mt-4">
          <div className="text-[11px] uppercase tracking-wide text-sg-ink-4">
            {t("sessions.turn.userInput")}
          </div>
          <div
            data-testid="turn-summary-user-input"
            className="mt-1 whitespace-pre-wrap break-words rounded-sg-md border border-sg-accent/30 bg-sg-accent-soft px-3 py-2 text-sm leading-relaxed text-sg-ink"
          >
            {trimmedInput}
          </div>
        </div>
      )}

      {turn.errorMessage && (
        <div
          data-testid="turn-summary-error"
          className="mt-3 rounded-sg-sm border border-sg-err/30 bg-sg-err-soft px-2 py-1.5 text-xs text-sg-err"
        >
          {turn.errorMessage}
        </div>
      )}
    </article>
  );
}

/* -------------------------------------------------------------- */
/*                          Sub-components                        */
/* -------------------------------------------------------------- */

function StatusBadge({ status }: { status: Turn["status"] }) {
  const { t } = useTranslation();
  // Mirrors the badge palette in event-timeline.tsx + tool-widget.tsx so
  // the drill-down page reads as the same surface as the live view.
  switch (status) {
    case "streaming":
      return (
        <span
          data-testid="turn-summary-badge"
          data-status="streaming"
          className="inline-flex items-center gap-1 rounded-sg-sm border border-sg-accent/30 bg-sg-accent-soft px-1.5 py-0.5 text-[11px] font-medium text-sg-accent"
        >
          <Loader2 className="size-3 animate-spin" aria-hidden />
          {t("sessions.timeline.streaming")}
        </span>
      );
    case "complete":
      return (
        <span
          data-testid="turn-summary-badge"
          data-status="complete"
          className="inline-flex items-center gap-1 rounded-sg-sm border border-sg-ok/30 bg-sg-ok-soft px-1.5 py-0.5 text-[11px] font-medium text-sg-ok"
        >
          <CircleCheck className="size-3" aria-hidden />
          {t("sessions.timeline.complete")}
        </span>
      );
    case "errored":
      return (
        <span
          data-testid="turn-summary-badge"
          data-status="errored"
          className="inline-flex items-center gap-1 rounded-sg-sm border border-sg-err/30 bg-sg-err-soft px-1.5 py-0.5 text-[11px] font-medium text-sg-err"
        >
          <AlertTriangle className="size-3" aria-hidden />
          {t("sessions.timeline.errored")}
        </span>
      );
    case "cancelling":
      return (
        <span
          data-testid="turn-summary-badge"
          data-status="cancelling"
          className="inline-flex items-center gap-1 rounded-sg-sm border border-sg-warn/30 bg-sg-warn-soft px-1.5 py-0.5 text-[11px] font-medium text-sg-warn"
        >
          <Octagon className="size-3" aria-hidden />
          {t("sessions.timeline.cancelling")}
        </span>
      );
  }
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-[11px] uppercase tracking-wide text-sg-ink-4">
        {label}
      </dt>
      <dd className="font-mono text-sm text-sg-ink">{children}</dd>
    </div>
  );
}
