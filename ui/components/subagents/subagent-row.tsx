"use client";

/**
 * `<SubagentRow>` — one row in the `/admin/subagents` live table.
 *
 * Receives a flat `SubagentStatusResponse` (the wire shape exported from
 * `lib/api.ts`) plus parent callbacks for row-click and kill. The
 * elapsed counter ticks once a second while the row is in-flight
 * (`queued`/`running`/`stalled`) and freezes otherwise — the parent
 * page reuses the same component for completed rows when the
 * "Include completed" toggle is on.
 *
 * Tidepool: glass-on-glass — inherits its surface from the page's
 * `<Card>` body so we don't double up the panel chrome here.
 */

import * as React from "react";
import Link from "next/link";
import { useTranslation } from "react-i18next";
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Loader2,
  OctagonX,
  Pause,
  XOctagon,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import type { SubagentState, SubagentStatusResponse } from "@/lib/api";

export interface SubagentRowProps {
  data: SubagentStatusResponse;
  onSelect: (requestId: string) => void;
  onKill: (requestId: string) => void;
}

const IN_FLIGHT_STATES: ReadonlySet<SubagentState> = new Set([
  "queued",
  "running",
  "stalled",
]);

/** State → (icon, classes for the pill chrome). Maps onto the Spatial
 * Glass status tokens `<ToolWidget>` uses: in-flight → warn, succeeded →
 * ok, failed/killed/timeout → err. The sg tokens are theme-aware so the
 * pill flips between light/dark automatically — no `dark:` variants. */
function statePresentation(state: SubagentState): {
  Icon: React.ComponentType<{ className?: string }>;
  className: string;
} {
  switch (state) {
    case "queued":
      return {
        Icon: Pause,
        className: "border border-sg-warn/30 bg-sg-warn-soft text-sg-warn",
      };
    case "running":
      return {
        Icon: Loader2,
        className: "border border-sg-warn/30 bg-sg-warn-soft text-sg-warn",
      };
    case "stalled":
      return {
        Icon: Clock,
        className: "border border-sg-warn/40 bg-sg-warn-soft text-sg-warn",
      };
    case "succeeded":
      return {
        Icon: CheckCircle2,
        className: "border border-sg-ok/30 bg-sg-ok-soft text-sg-ok",
      };
    case "failed":
      return {
        Icon: AlertTriangle,
        className: "border border-sg-err/30 bg-sg-err-soft text-sg-err",
      };
    case "killed":
      return {
        Icon: OctagonX,
        className: "border border-sg-err/40 bg-sg-err-soft text-sg-err",
      };
    case "timeout":
      return {
        Icon: XOctagon,
        className: "border border-sg-err/30 bg-sg-err-soft text-sg-err",
      };
  }
}

/** Formats `ms` into a compact `14s` / `2m 31s` / `1h 04m` string. */
function formatElapsed(ms: number): string {
  if (ms <= 0) return "0s";
  const totalSec = Math.floor(ms / 1000);
  if (totalSec < 60) return `${totalSec}s`;
  const mins = Math.floor(totalSec / 60);
  const secs = totalSec % 60;
  if (mins < 60) return `${mins}m ${secs.toString().padStart(2, "0")}s`;
  const hours = Math.floor(mins / 60);
  const remMins = mins % 60;
  return `${hours}h ${remMins.toString().padStart(2, "0")}m`;
}

/** Live elapsed counter — ticks every second when in-flight, freezes
 * on terminal. Computes from `started_at` (epoch-ms) when present,
 * falls back to the precomputed `elapsed_ms` snapshot otherwise. */
function useElapsed(
  data: SubagentStatusResponse,
): number {
  const inFlight = IN_FLIGHT_STATES.has(data.state);
  const [now, setNow] = React.useState<number>(() => Date.now());

  React.useEffect(() => {
    if (!inFlight) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [inFlight]);

  if (data.started_at == null) return data.elapsed_ms;
  if (!inFlight) {
    // Terminal — prefer the server's frozen elapsed_ms when set,
    // otherwise derive from finished_at − started_at.
    if (data.elapsed_ms > 0) return data.elapsed_ms;
    if (data.finished_at != null) return data.finished_at - data.started_at;
    return 0;
  }
  return now - data.started_at;
}

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max - 1)}…`;
}

export function SubagentRow({
  data,
  onSelect,
  onKill,
}: SubagentRowProps): React.JSX.Element {
  const { t } = useTranslation();
  const elapsed = useElapsed(data);
  const { Icon: StateIcon, className: stateClass } = statePresentation(
    data.state,
  );
  const inFlight = IN_FLIGHT_STATES.has(data.state);
  const task = truncate(
    data.description ?? data.subagent_type ?? "—",
    80,
  );
  const parentShort = `${data.parent_session_key.slice(0, 12)}…`;

  function handleSelect() {
    onSelect(data.request_id);
  }

  function handleKill(e: React.MouseEvent<HTMLButtonElement>) {
    e.stopPropagation();
    const message = t("subagents.action.killConfirm", {
      type: data.subagent_type,
    });
    // Browser-native confirm is enough for an admin-only destructive
    // op; the page can always upgrade to a `<ConfirmDialog>` later.
    if (typeof window !== "undefined" && !window.confirm(message)) return;
    onKill(data.request_id);
  }

  return (
    <tr
      data-testid="subagent-row"
      data-state={data.state}
      data-request-id={data.request_id}
      onClick={handleSelect}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          handleSelect();
        }
      }}
      tabIndex={0}
      className={cn(
        "cursor-pointer border-b border-sg-border text-sm text-sg-ink",
        "transition-colors hover:bg-sg-inset",
        "focus:outline-none focus-visible:bg-sg-inset",
      )}
    >
      <td className="whitespace-nowrap px-3 py-2.5">
        <span
          data-testid="subagent-type-pill"
          className="inline-flex items-center rounded-sg-sm border border-sg-border bg-sg-inset px-2 py-0.5 font-mono text-[11px] text-sg-ink"
        >
          {data.subagent_type}
        </span>
      </td>
      <td className="max-w-[340px] truncate px-3 py-2.5 text-sg-ink-2">
        {task}
      </td>
      <td className="whitespace-nowrap px-3 py-2.5">
        <Link
          href={`/sessions/detail?key=${encodeURIComponent(
            data.parent_session_key,
          )}`}
          onClick={(e) => e.stopPropagation()}
          className="font-mono text-[11px] text-sg-ink-3 underline-offset-2 hover:text-sg-ink hover:underline"
        >
          {parentShort}
        </Link>
      </td>
      <td className="whitespace-nowrap px-3 py-2.5">
        <span
          data-testid="subagent-state-pill"
          className={cn(
            "inline-flex items-center gap-1 rounded-sg-sm px-2 py-0.5 text-[11px] font-medium",
            stateClass,
          )}
        >
          <StateIcon
            className={cn(
              "h-3 w-3",
              data.state === "running" && "animate-spin",
            )}
            aria-hidden
          />
          {t(`subagents.state.${data.state}`)}
        </span>
      </td>
      <td
        data-testid="subagent-elapsed"
        className="whitespace-nowrap px-3 py-2.5 font-mono text-[11px] text-sg-ink-2"
      >
        {formatElapsed(elapsed)}
      </td>
      <td className="whitespace-nowrap px-3 py-2.5 font-mono text-[11px] text-sg-ink-2">
        {data.tool_calls_made}
      </td>
      <td className="whitespace-nowrap px-3 py-2.5 text-right">
        {inFlight ? (
          <Button
            type="button"
            size="sm"
            variant="destructive"
            data-testid="subagent-kill-button"
            onClick={handleKill}
            className="h-7 px-2.5 text-[11px]"
          >
            {t("subagents.action.kill")}
          </Button>
        ) : (
          <span className="text-[11px] text-sg-ink-3">—</span>
        )}
      </td>
    </tr>
  );
}
