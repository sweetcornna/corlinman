"use client";

/**
 * `<LiveAgentsPanel>` — Codex-Desktop-style live cards for multi-agent runs.
 *
 * Shared by the global `/admin/subagents` page and the chat-side live panel.
 * Renders one card per sub-agent with a status badge (running→spinner,
 * done→check, failed→✕), a live current-activity line ("运行工具 web_search"),
 * an elapsed ticker, a tool-call count, an inline/background source tag, and a
 * kill action for in-flight rows. Supervisor→worker nesting is reconstructed
 * from `parent_session_key` (which equals the parent's `request_id` for inline
 * children) and rendered as indented sub-cards.
 *
 * Pure presentation: the caller owns the live `SubagentStatusResponse[]`
 * (fed by the `/admin/subagents/events/live` SSE) plus the select/kill
 * callbacks. Status-icon + elapsed logic is reused from `<SubagentRow>`.
 */

import * as React from "react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import type { SubagentStatusResponse } from "@/lib/api";
import {
  IN_FLIGHT_STATES,
  formatElapsed,
  statePresentation,
  useElapsed,
} from "@/components/subagents/subagent-row";

export interface LiveAgentsPanelProps {
  rows: SubagentStatusResponse[];
  onSelect: (requestId: string) => void;
  onKill: (requestId: string) => void;
  /** Tighter padding for the narrow chat-side variant. */
  dense?: boolean;
  /** When true, clicking a card toggles an inline detail panel (prompt,
   * activity, tools, elapsed, finish reason, summary) instead of calling
   * `onSelect`. Used by the chat rail so an inline child's current status is
   * visible without navigating away. */
  expandable?: boolean;
  className?: string;
}

interface TreeNode {
  row: SubagentStatusResponse;
  children: TreeNode[];
}

function sortRows(rows: SubagentStatusResponse[]): SubagentStatusResponse[] {
  return [...rows].sort((a, b) => {
    const aActive = IN_FLIGHT_STATES.has(a.state);
    const bActive = IN_FLIGHT_STATES.has(b.state);
    if (aActive !== bActive) return aActive ? -1 : 1;
    const aKey = a.started_at ?? a.finished_at ?? 0;
    const bKey = b.started_at ?? b.finished_at ?? 0;
    return bKey - aKey;
  });
}

/** Reconstruct the supervisor→worker forest. A row is a child of another when
 * its `parent_session_key` matches that row's `request_id` (inline children
 * are keyed by `child_session_key`); otherwise it is a root (its parent is the
 * main turn/session, which has no sub-agent row). */
function buildForest(rows: SubagentStatusResponse[]): TreeNode[] {
  const byId = new Map(rows.map((r) => [r.request_id, r]));
  const childrenOf = new Map<string, SubagentStatusResponse[]>();
  const roots: SubagentStatusResponse[] = [];
  for (const r of rows) {
    const parent = r.parent_session_key;
    if (parent && parent !== r.request_id && byId.has(parent)) {
      const bucket = childrenOf.get(parent);
      if (bucket) bucket.push(r);
      else childrenOf.set(parent, [r]);
    } else {
      roots.push(r);
    }
  }
  const build = (row: SubagentStatusResponse): TreeNode => ({
    row,
    children: sortRows(childrenOf.get(row.request_id) ?? []).map(build),
  });
  return sortRows(roots).map(build);
}

export function LiveAgentsPanel({
  rows,
  onSelect,
  onKill,
  dense = false,
  expandable = false,
  className,
}: LiveAgentsPanelProps): React.JSX.Element {
  const forest = React.useMemo(() => buildForest(rows), [rows]);
  return (
    <div
      className={cn("flex flex-col gap-2", className)}
      data-testid="live-agents-panel"
    >
      {forest.map((node) => (
        <AgentCardTree
          key={node.row.request_id}
          node={node}
          depth={0}
          onSelect={onSelect}
          onKill={onKill}
          dense={dense}
          expandable={expandable}
        />
      ))}
    </div>
  );
}

function AgentCardTree({
  node,
  depth,
  onSelect,
  onKill,
  dense,
  expandable,
}: {
  node: TreeNode;
  depth: number;
  onSelect: (requestId: string) => void;
  onKill: (requestId: string) => void;
  dense: boolean;
  expandable: boolean;
}): React.JSX.Element {
  return (
    <>
      <AgentCard
        data={node.row}
        depth={depth}
        onSelect={onSelect}
        onKill={onKill}
        dense={dense}
        expandable={expandable}
      />
      {node.children.length > 0 ? (
        <div
          className="flex flex-col gap-2 border-l border-sg-border pl-2"
          style={{ marginLeft: dense ? 6 : 10 }}
        >
          {node.children.map((child) => (
            <AgentCardTree
              key={child.row.request_id}
              node={child}
              depth={depth + 1}
              onSelect={onSelect}
              onKill={onKill}
              dense={dense}
              expandable={expandable}
            />
          ))}
        </div>
      ) : null}
    </>
  );
}

function truncate(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, max - 1)}…`;
}

function AgentCard({
  data,
  onSelect,
  onKill,
  dense,
  expandable,
}: {
  data: SubagentStatusResponse;
  depth: number;
  onSelect: (requestId: string) => void;
  onKill: (requestId: string) => void;
  dense: boolean;
  expandable: boolean;
}): React.JSX.Element {
  const { t } = useTranslation();
  const [expanded, setExpanded] = React.useState(false);
  const elapsed = useElapsed(data);
  const { Icon, className: stateClass } = statePresentation(data.state);
  const inFlight = IN_FLIGHT_STATES.has(data.state);
  const activity = (data.activity ?? "").trim();
  const task = truncate(data.description ?? "", 100);
  // Inline subagents are awaited inside the parent turn — the background
  // ``/admin/subagents/{id}/kill`` store path can't reach them, so only
  // background rows get the kill affordance.
  const canKill = inFlight && data.source !== "inline";

  function handleKill(e: React.MouseEvent<HTMLButtonElement>) {
    e.stopPropagation();
    const message = t("subagents.action.killConfirm", {
      type: data.subagent_type,
    });
    if (typeof window !== "undefined" && !window.confirm(message)) return;
    onKill(data.request_id);
  }

  const activate = () => {
    if (expandable) setExpanded((v) => !v);
    else onSelect(data.request_id);
  };

  return (
    <div
      role="button"
      tabIndex={0}
      aria-expanded={expandable ? expanded : undefined}
      data-testid="live-agent-card"
      data-state={data.state}
      data-request-id={data.request_id}
      data-source={data.source ?? "background"}
      onClick={activate}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          activate();
        }
      }}
      className={cn(
        "group cursor-pointer rounded-sg-md border border-sg-border bg-sg-card",
        "shadow-sg-1 transition-colors hover:border-sg-accent/35 hover:bg-sg-inset",
        "focus:outline-none focus-visible:border-sg-accent/50",
        dense ? "px-2.5 py-2" : "px-3 py-2.5",
      )}
    >
      <div className="flex items-center gap-2">
        <span
          className={cn(
            "inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full",
            stateClass,
          )}
          aria-hidden
        >
          <Icon className={cn("h-3 w-3", data.state === "running" && "animate-spin")} />
        </span>
        <span className="min-w-0 flex-1 truncate font-mono text-[12px] font-medium text-sg-ink">
          {data.subagent_type || "subagent"}
        </span>
        {data.source === "inline" ? (
          <span className="shrink-0 rounded-sg-sm border border-sg-border px-1 py-0 font-mono text-[9px] uppercase tracking-wide text-sg-ink-3">
            inline
          </span>
        ) : null}
        <span className="shrink-0 font-mono text-[10px] text-sg-ink-3">
          {formatElapsed(elapsed)}
        </span>
        {canKill ? (
          <Button
            type="button"
            size="sm"
            variant="destructive"
            data-testid="live-agent-kill"
            onClick={handleKill}
            className="h-6 shrink-0 px-2 text-[10px] opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100"
          >
            {t("subagents.action.kill")}
          </Button>
        ) : null}
      </div>
      {/* Codex-style current-activity line (in-flight) or the task/summary. */}
      {inFlight && activity ? (
        <div className="mt-1 flex items-center gap-1 truncate pl-7 text-[11px] text-sg-warn">
          <span className="truncate">{activity}</span>
        </div>
      ) : task ? (
        <div className="mt-1 truncate pl-7 text-[11px] text-sg-ink-3">{task}</div>
      ) : null}
      <div className="mt-1 flex items-center gap-3 pl-7 text-[10px] text-sg-ink-4">
        <span data-testid="live-agent-state">{t(`subagents.state.${data.state}`)}</span>
        <span>{t("subagents.column.tools")}: {data.tool_calls_made}</span>
        {data.finish_reason && !inFlight ? (
          <span className="truncate font-mono">{data.finish_reason}</span>
        ) : null}
      </div>
      {expandable && expanded ? (
        <div
          className="mt-2 space-y-1.5 border-t border-sg-border pl-7 pt-2 text-[11px]"
          data-testid="live-agent-detail"
        >
          {data.description ? (
            <div>
              <span className="text-sg-ink-4">{t("subagents.column.description")}: </span>
              <span className="text-sg-ink-2">{data.description}</span>
            </div>
          ) : null}
          {inFlight && activity ? (
            <div className="text-sg-warn">{activity}</div>
          ) : null}
          {data.summary ? (
            <div className="whitespace-pre-wrap text-sg-ink-2">{data.summary}</div>
          ) : null}
          {data.error ? (
            <div className="whitespace-pre-wrap text-sg-err">{data.error}</div>
          ) : null}
          <div className="flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-[10px] text-sg-ink-4">
            <span>{t("subagents.state." + data.state)}</span>
            <span>{t("subagents.column.tools")}: {data.tool_calls_made}</span>
            <span>{formatElapsed(elapsed)}</span>
            {data.finish_reason ? <span>{data.finish_reason}</span> : null}
            {data.child_session_key ? (
              <span className="truncate" title={data.child_session_key}>
                {data.child_session_key.slice(-18)}
              </span>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}

export default LiveAgentsPanel;
