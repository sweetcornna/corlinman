/**
 * Compact tool-call widget. Shows:
 *   - state icon (○ pending / ◐ running / ● completed / ● error)
 *   - tool name
 *   - one-line arg summary (first JSON key+value or first 64 chars)
 *   - elapsed (live-ticking while running)
 *   - state badge
 *
 * Click to expand → renders detailed view via tool-renderers dispatcher.
 *
 * Inspired by `opencode`'s InlineTool/BlockTool collapsing pattern.
 */
"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, ChevronRight, Circle, CircleCheck, CircleDot, CircleX } from "lucide-react";

import { cn } from "@/lib/utils";
import type { ToolPart, ToolPartState } from "@/lib/sessions/store";
import { rendererForTool } from "./tool-renderers";
import { SubagentTree } from "./subagent-tree";

export interface ToolWidgetProps {
  part: ToolPart;
}

function StateIcon({ state }: { state: ToolPartState }) {
  switch (state.kind) {
    case "pending":
      return <Circle className="size-3.5 shrink-0 text-amber-700/40" aria-hidden />;
    case "running":
      return (
        <CircleDot
          className="size-3.5 shrink-0 animate-pulse text-amber-600 dark:text-amber-400"
          aria-hidden
        />
      );
    case "completed":
      return (
        <CircleCheck
          className="size-3.5 shrink-0 text-emerald-600 dark:text-emerald-400"
          aria-hidden
        />
      );
    case "error":
      return <CircleX className="size-3.5 shrink-0 text-red-600 dark:text-red-400" aria-hidden />;
  }
}

function summarizeArgs(raw: string): string {
  if (!raw) return "";
  try {
    const obj = JSON.parse(raw) as Record<string, unknown>;
    const firstKey = Object.keys(obj)[0];
    if (!firstKey) return "";
    const v = obj[firstKey];
    if (typeof v === "string") return `${firstKey}=${v.length > 60 ? v.slice(0, 60) + "…" : v}`;
    if (typeof v === "number" || typeof v === "boolean") return `${firstKey}=${String(v)}`;
    return firstKey;
  } catch {
    return raw.length > 64 ? raw.slice(0, 64) + "…" : raw;
  }
}

function formatElapsed(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.floor((ms % 60_000) / 1000);
  return `${m}m${s.toString().padStart(2, "0")}s`;
}

function useElapsed(state: ToolPartState): string | null {
  // Re-render every second while running so the elapsed counter ticks.
  const [, setTick] = React.useState(0);
  const running = state.kind === "running";
  React.useEffect(() => {
    if (!running) return;
    const id = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [running]);

  switch (state.kind) {
    case "pending":
      return null;
    case "running":
      return formatElapsed(Date.now() - state.startedAt);
    case "completed":
    case "error":
      return formatElapsed(state.completedAt - state.startedAt);
  }
}

export function ToolWidget({ part }: ToolWidgetProps) {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState<boolean>(false);

  const elapsed = useElapsed(part.state);
  const Renderer = rendererForTool(part.tool_name);

  const stateLabelKey =
    part.state.kind === "pending"
      ? "sessions.tools.pending"
      : part.state.kind === "running"
        ? "sessions.tools.running"
        : part.state.kind === "completed"
          ? "sessions.tools.completed"
          : "sessions.tools.error";

  const output = part.state.kind === "completed" ? part.state.output : undefined;
  const errorOutput = part.state.kind === "error" ? part.state.message : undefined;
  const isError = part.state.kind === "error";

  return (
    <div
      data-testid="tool-widget"
      data-tool-name={part.tool_name}
      data-tool-state={part.state.kind}
      className={cn(
        "rounded-xl border border-amber-200/40 bg-white/50 backdrop-blur-sm",
        "dark:border-white/10 dark:bg-black/30",
        "shadow-sm",
      )}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        data-testid="tool-widget-toggle"
        className={cn(
          "flex w-full items-center gap-2 px-3 py-2 text-left text-xs",
          "text-amber-950 dark:text-amber-100",
          "hover:bg-amber-50/40 dark:hover:bg-amber-900/10",
          "transition-colors rounded-xl",
        )}
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown className="size-3.5 shrink-0 opacity-50" aria-hidden />
        ) : (
          <ChevronRight className="size-3.5 shrink-0 opacity-50" aria-hidden />
        )}
        <StateIcon state={part.state} />
        <span className="font-mono font-semibold text-amber-900 dark:text-amber-200">
          {part.tool_name}
        </span>
        <span className="truncate font-mono opacity-60">{summarizeArgs(part.input_json)}</span>
        <span
          className={cn(
            "ml-auto shrink-0 rounded-sm px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide",
            part.state.kind === "running" &&
              "bg-amber-200/40 text-amber-900 dark:bg-amber-700/30 dark:text-amber-200",
            part.state.kind === "completed" &&
              "bg-emerald-200/40 text-emerald-900 dark:bg-emerald-700/30 dark:text-emerald-200",
            part.state.kind === "error" &&
              "bg-red-200/40 text-red-900 dark:bg-red-700/30 dark:text-red-200",
            part.state.kind === "pending" && "bg-amber-100/40 text-amber-700/70 dark:bg-white/5",
          )}
        >
          {t(stateLabelKey)}
        </span>
        {elapsed && (
          <span className="shrink-0 font-mono text-[10px] opacity-60">{elapsed}</span>
        )}
      </button>
      {open && (
        <div
          data-testid="tool-widget-body"
          className="border-t border-amber-100/40 px-3 py-2 dark:border-white/5"
        >
          <Renderer
            toolName={part.tool_name}
            inputJson={part.input_json}
            output={output ?? errorOutput}
            isError={isError}
          />
        </div>
      )}
      {/* W3.2 — nested subagent timelines spawned by this tool call. Mounted
       *  outside the expandable args/result block so the operator can watch
       *  the child run unfold even when the parent tool body is collapsed. */}
      {part.subagentSessions && part.subagentSessions.length > 0 && (
        <div className="border-t border-amber-100/40 px-3 py-2 dark:border-white/5">
          {part.subagentSessions.map((session) => (
            <SubagentTree
              key={session.child_session_key}
              session={session}
            />
          ))}
        </div>
      )}
    </div>
  );
}
