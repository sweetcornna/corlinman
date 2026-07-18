"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, ChevronRight, GitFork, Loader2 } from "@/components/icons";

import { cn } from "@/lib/utils";
import type { SubagentCardState } from "@/lib/chat/types";

interface SubagentCardProps {
  subagent: SubagentCardState;
}

export function SubagentCard({ subagent }: SubagentCardProps) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = React.useState(false);
  const eventCount = subagent.events?.length ?? 0;

  const statusLabel: Record<SubagentCardState["status"], string> = {
    spawned: t("chat.subagentStatusSpawned"),
    running: t("chat.subagentStatusRunning"),
    completed: t("chat.subagentStatusCompleted"),
    errored: t("chat.subagentStatusErrored"),
  };

  const statusDot =
    subagent.status === "completed"
      ? "bg-sg-ok"
      : subagent.status === "errored"
        ? "bg-sg-err"
        : "bg-sg-accent";

  return (
    <div
      className={cn("my-2 overflow-hidden rounded-sg-md sg-inset")}
      data-testid="subagent-card"
      data-status={subagent.status}
    >
      <button
        type="button"
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[12px] text-sg-ink hover:bg-sg-inset-hover"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        {expanded ? (
          <ChevronDown className="h-3.5 w-3.5 text-sg-ink-4" aria-hidden="true" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 text-sg-ink-4" aria-hidden="true" />
        )}
        <GitFork className="h-3.5 w-3.5 text-sg-ink-4" aria-hidden="true" />
        <span className="font-mono text-sg-ink">
          {subagent.childAgentId ?? subagent.childSessionKey.slice(0, 12)}
        </span>
        <span className="rounded-full bg-sg-accent-2-soft px-1.5 py-0.5 font-mono text-[10px] text-sg-accent-2">
          {t("chat.subagentDepth", { n: subagent.depth })}
        </span>
        <span className="ml-auto flex items-center gap-1 text-[11px] text-sg-ink-4">
          {subagent.status === "running" || subagent.status === "spawned" ? (
            <Loader2 className="h-3 w-3 animate-spin text-sg-accent" aria-hidden="true" />
          ) : (
            <span
              className={cn("h-1.5 w-1.5 rounded-full", statusDot)}
              aria-hidden="true"
            />
          )}
          <span>{statusLabel[subagent.status]}</span>
          {subagent.toolCallsMade ? (
            <span className="font-mono">· {t("chat.subagentToolsSuffix", { n: subagent.toolCallsMade })}</span>
          ) : null}
        </span>
      </button>
      {expanded ? (
        <div className="border-t border-sg-border px-2.5 py-2 text-[11px] text-sg-ink-3">
          {subagent.promptPreview ? (
            <>
              <div className="mb-1 font-mono text-sg-ink-4">{t("chat.subagentPromptLabel")}</div>
              <div className="mb-2 whitespace-pre-wrap text-sg-ink">{subagent.promptPreview}</div>
            </>
          ) : null}
          {subagent.summary ? (
            <>
              <div className="mb-1 font-mono text-sg-ink-4">{t("chat.subagentSummaryLabel")}</div>
              <div className="mb-2 whitespace-pre-wrap text-sg-ink">{subagent.summary}</div>
            </>
          ) : null}
          {eventCount > 0 ? (
            <div className="font-mono text-sg-ink-4">
              {t("chat.subagentEventsRecorded", { count: eventCount, n: eventCount })}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
