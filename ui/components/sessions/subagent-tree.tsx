/**
 * `<SubagentTree>` — W3.2 nested-subagent renderer.
 *
 * Renders one ``SubagentSession`` from the store as an indented amber-trim
 * card under its spawning tool widget. The tree is recursive in two ways:
 *
 *   1. Per part — child parts (text / reasoning / tool_use) render via the
 *      same primitives the parent timeline uses (`<ReasoningBlock>`,
 *      `<ToolWidget>`, raw text). So a grandchild's *parts* "just render".
 *   2. Per session — when a child tool widget itself spawned its own
 *      subagent, `<ToolWidget>` mounts a nested `<SubagentTree>` from its
 *      `subagentSessions` array. The recursion bottoms out at
 *      `SUBAGENT_MAX_RENDER_DEPTH` (3); deeper sessions are dropped by the
 *      reducer before reaching us, so we never recurse unbounded.
 *
 * Default-collapsed once `status !== 'running'` so completed work doesn't
 * dominate the timeline; running sessions stay open so the live tail is
 * visible. Click the header to toggle.
 *
 * Tidepool: amber border-left + warm body card, matching reasoning-block
 * + tool-widget so the nested tree feels like part of the same surface.
 */
"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import {
  AlertTriangle,
  Bot,
  ChevronDown,
  ChevronRight,
  CircleCheck,
  Loader2,
} from "lucide-react";

import { cn } from "@/lib/utils";
import type { Part, SubagentSession } from "@/lib/sessions/store";
import { ReasoningBlock } from "./reasoning-block";
import { ToolWidget } from "./tool-widget";

export interface SubagentTreeProps {
  session: SubagentSession;
}

function formatElapsed(ms: number | undefined): string {
  if (ms == null) return "";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.floor((ms % 60_000) / 1000);
  return `${m}m${s.toString().padStart(2, "0")}s`;
}

function truncate(s: string, max: number): string {
  if (!s) return "";
  if (s.length <= max) return s;
  return s.slice(0, max) + "…";
}

/**
 * Recursive part renderer — same shape as `event-timeline.tsx`'s
 * `renderPart`, kept local on purpose so we don't add a public export
 * coupling. The renderers are unchanged, the lower-level components do
 * their own state handling.
 */
function renderSubPart(part: Part): React.ReactNode {
  switch (part.kind) {
    case "text":
      return (
        <div
          key={part.block_id}
          className={cn(
            "whitespace-pre-wrap break-words text-sm leading-relaxed",
            "text-amber-950 dark:text-amber-50",
          )}
        >
          {part.text}
          {!part.done && (
            <span
              className="ml-0.5 inline-block h-3.5 w-1 animate-pulse bg-amber-500/80 align-middle"
              aria-hidden
            />
          )}
        </div>
      );
    case "reasoning":
      return (
        <ReasoningBlock
          key={part.block_id}
          text={part.text}
          streaming={!part.done}
        />
      );
    case "tool_use":
      return <ToolWidget key={part.block_id} part={part} />;
  }
}

export function SubagentTree({ session }: SubagentTreeProps) {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState<boolean>(session.status === "running");

  // Auto-expand while running; respect user choice once complete.
  React.useEffect(() => {
    if (session.status === "running") setOpen(true);
  }, [session.status]);

  const eventCount = session.parts.length;
  const elapsedLabel = formatElapsed(session.elapsed_ms);

  return (
    <div
      data-testid="subagent-tree"
      data-status={session.status}
      data-depth={session.depth}
      className={cn(
        "mt-2 rounded-lg border-l-2 border-amber-400/70 bg-amber-50/30",
        "dark:border-amber-300/40 dark:bg-amber-950/15",
        "shadow-inner",
      )}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={cn(
          "flex w-full items-center gap-2 px-3 py-2 text-left text-xs",
          "text-amber-900 dark:text-amber-200",
          "hover:bg-amber-100/30 dark:hover:bg-amber-900/20",
          "transition-colors rounded-r-lg",
        )}
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown className="size-3.5 shrink-0 opacity-70" aria-hidden />
        ) : (
          <ChevronRight className="size-3.5 shrink-0 opacity-70" aria-hidden />
        )}
        <Bot
          className={cn(
            "size-3.5 shrink-0",
            session.status === "running" &&
              "animate-pulse text-amber-500 dark:text-amber-300",
          )}
          aria-hidden
        />
        <span
          className="font-mono font-semibold text-amber-900 dark:text-amber-200"
          data-testid="subagent-agent-id"
        >
          {session.child_agent_id}
        </span>
        <span className="shrink-0 rounded-sm bg-amber-200/30 px-1.5 py-0.5 font-mono text-[10px] opacity-70 dark:bg-amber-700/20">
          {t("sessions.subagent.depth")} {session.depth}
        </span>
        <span
          data-testid="subagent-status-badge"
          className={cn(
            "ml-auto inline-flex shrink-0 items-center gap-1 rounded-sm px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide",
            session.status === "running" &&
              "bg-amber-200/40 text-amber-900 dark:bg-amber-700/30 dark:text-amber-200",
            session.status === "complete" &&
              "bg-emerald-200/40 text-emerald-900 dark:bg-emerald-700/30 dark:text-emerald-200",
            session.status === "errored" &&
              "bg-red-200/40 text-red-900 dark:bg-red-700/30 dark:text-red-200",
          )}
        >
          {session.status === "running" && (
            <Loader2 className="size-2.5 animate-spin" aria-hidden />
          )}
          {session.status === "complete" && (
            <CircleCheck className="size-2.5" aria-hidden />
          )}
          {session.status === "errored" && (
            <AlertTriangle className="size-2.5" aria-hidden />
          )}
          {session.status === "running"
            ? t("sessions.subagent.running")
            : session.status === "complete"
              ? t("sessions.subagent.completed")
              : t("sessions.subagent.errored")}
        </span>
      </button>

      {/* Prompt preview line — visible whether expanded or collapsed. */}
      <div
        className="px-3 pb-1 pt-0 text-[11px] italic text-amber-800/70 dark:text-amber-200/60"
        title={session.prompt_preview}
      >
        <span className="not-italic opacity-60">
          {t("sessions.subagent.prompt")}:
        </span>{" "}
        {truncate(session.prompt_preview, 80)}
      </div>

      {!open ? (
        <div className="px-3 pb-2 text-[11px] text-amber-700/60 dark:text-amber-200/50">
          {eventCount > 0 ? (
            <span data-testid="subagent-collapsed-summary">
              {eventCount} {eventCount === 1 ? "event" : "events"}
              {elapsedLabel && ` · ${elapsedLabel}`}
            </span>
          ) : (
            <span className="italic opacity-50">
              {session.status === "running" ? "…" : ""}
            </span>
          )}
        </div>
      ) : (
        <div className="space-y-2 px-3 pb-3 pt-1">
          {session.parts.length === 0 ? (
            <div className="text-xs italic opacity-50">
              {session.status === "running" ? "…" : ""}
            </div>
          ) : (
            session.parts.map(renderSubPart)
          )}

          {/* Completed footer */}
          {session.status !== "running" && (
            <div
              data-testid="subagent-footer"
              className={cn(
                "mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 border-t pt-2 text-[11px]",
                "border-amber-200/40 text-amber-800/70",
                "dark:border-amber-300/10 dark:text-amber-200/60",
              )}
            >
              {session.finish_reason && (
                <span data-testid="subagent-finish-reason">
                  <span className="opacity-60">·</span>{" "}
                  <span className="font-mono">{session.finish_reason}</span>
                </span>
              )}
              {session.tool_calls_made != null && (
                <span>
                  {session.tool_calls_made} {t("sessions.subagent.tools")}
                </span>
              )}
              {elapsedLabel && (
                <span className="font-mono opacity-70">{elapsedLabel}</span>
              )}
              {session.summary && (
                <span
                  className="basis-full truncate opacity-70"
                  title={session.summary}
                >
                  {session.summary}
                </span>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Placeholder rendered in place of a sub-tree whose ``depth`` exceeds
 * ``SUBAGENT_MAX_RENDER_DEPTH``. The reducer drops events for such
 * sessions, so the placeholder is purely cosmetic — but emitting it lets
 * the user know there's a deeper layer that was elided rather than
 * silently missing.
 */
export function DeeperSubagentPlaceholder({ depth }: { depth: number }) {
  const { t } = useTranslation();
  return (
    <div
      className={cn(
        "mt-1 rounded-md border border-dashed border-amber-300/40 bg-amber-50/20",
        "dark:border-amber-300/15 dark:bg-amber-950/10",
        "px-2 py-1 text-[11px] italic text-amber-700/60 dark:text-amber-200/50",
      )}
    >
      {t("sessions.subagent.tooDeep")} (depth={depth})
    </div>
  );
}
