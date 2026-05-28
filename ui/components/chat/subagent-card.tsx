"use client";

/**
 * Sub-agent spawn card. Renders the child agent's prompt + a status pill +
 * a collapsible event-log preview (shown as a count, expand to see).
 */

import * as React from "react";
import { ChevronDown, ChevronRight, GitFork, Loader2 } from "lucide-react";

import { cn } from "@/lib/utils";
import type { SubagentCardState } from "@/lib/chat/types";

interface SubagentCardProps {
  subagent: SubagentCardState;
}

const STATUS_LABEL: Record<SubagentCardState["status"], string> = {
  spawned: "Spawned",
  running: "Running",
  completed: "Completed",
  errored: "Errored",
};

export function SubagentCard({ subagent }: SubagentCardProps) {
  const [expanded, setExpanded] = React.useState(false);
  const eventCount = subagent.events?.length ?? 0;

  return (
    <div
      className={cn(
        "my-2 overflow-hidden rounded-md border border-tp-glass-edge bg-tp-glass-inner/60",
      )}
      data-testid="subagent-card"
      data-status={subagent.status}
    >
      <button
        type="button"
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[12px] text-tp-ink hover:bg-tp-glass-inner/80"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        {expanded ? (
          <ChevronDown className="h-3.5 w-3.5 text-tp-ink-3" aria-hidden="true" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 text-tp-ink-3" aria-hidden="true" />
        )}
        <GitFork className="h-3.5 w-3.5 text-tp-ink-3" aria-hidden="true" />
        <span className="font-mono text-tp-ink">
          {subagent.childAgentId ?? subagent.childSessionKey.slice(0, 12)}
        </span>
        <span className="text-tp-ink-3">· depth {subagent.depth}</span>
        <span className="ml-auto flex items-center gap-1 text-[11px] text-tp-ink-3">
          {subagent.status === "running" || subagent.status === "spawned" ? (
            <Loader2 className="h-3 w-3 animate-spin text-tp-amber" aria-hidden="true" />
          ) : null}
          <span>{STATUS_LABEL[subagent.status]}</span>
          {subagent.toolCallsMade ? (
            <span className="font-mono">· {subagent.toolCallsMade} tools</span>
          ) : null}
        </span>
      </button>
      {expanded ? (
        <div className="border-t border-tp-glass-edge px-2.5 py-2 text-[11px] text-tp-ink-2">
          {subagent.promptPreview ? (
            <>
              <div className="mb-1 font-mono text-tp-ink-3">prompt</div>
              <div className="mb-2 whitespace-pre-wrap text-tp-ink">
                {subagent.promptPreview}
              </div>
            </>
          ) : null}
          {subagent.summary ? (
            <>
              <div className="mb-1 font-mono text-tp-ink-3">summary</div>
              <div className="mb-2 whitespace-pre-wrap text-tp-ink">
                {subagent.summary}
              </div>
            </>
          ) : null}
          {eventCount > 0 ? (
            <div className="font-mono text-tp-ink-3">
              {eventCount} event{eventCount === 1 ? "" : "s"} recorded
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
