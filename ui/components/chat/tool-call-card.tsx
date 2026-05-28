"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Loader2,
  Wrench,
  XCircle,
} from "lucide-react";

import { cn } from "@/lib/utils";
import type { ToolCallState } from "@/lib/chat/types";

interface ToolCallCardProps {
  tool: ToolCallState;
  defaultExpanded?: boolean;
}

function formatDuration(ms?: number): string {
  if (!ms) return "";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

export function ToolCallCard({ tool, defaultExpanded = false }: ToolCallCardProps) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = React.useState(defaultExpanded);

  const statusIcon =
    tool.status === "running" ? (
      <Loader2 className="h-3.5 w-3.5 animate-spin text-tp-amber" />
    ) : tool.status === "ok" ? (
      <CheckCircle2 className="h-3.5 w-3.5 text-tp-ok" />
    ) : tool.status === "error" ? (
      <XCircle className="h-3.5 w-3.5 text-tp-err" />
    ) : (
      <AlertCircle className="h-3.5 w-3.5 text-tp-ink-3" />
    );

  const argsPretty = React.useMemo(() => {
    try {
      return JSON.stringify(JSON.parse(tool.argsJson || "{}"), null, 2);
    } catch {
      return tool.argsJson;
    }
  }, [tool.argsJson]);

  return (
    <div
      className={cn(
        "my-2 overflow-hidden rounded-md border border-tp-glass-edge bg-tp-glass-inner/60",
        tool.status === "error" && "border-tp-err/40",
      )}
      data-testid="tool-call-card"
      data-tool-name={tool.toolName}
      data-status={tool.status}
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
        <Wrench className="h-3.5 w-3.5 text-tp-ink-3" aria-hidden="true" />
        <span className="font-mono text-tp-ink">{tool.toolName}</span>
        {tool.pluginName ? (
          <span className="text-tp-ink-3">· {tool.pluginName}</span>
        ) : null}
        <span className="ml-auto flex items-center gap-1 text-[11px] text-tp-ink-3">
          {statusIcon}
          {tool.durationMs ? (
            <time className="font-mono">{formatDuration(tool.durationMs)}</time>
          ) : null}
        </span>
      </button>
      {expanded ? (
        <div className="border-t border-tp-glass-edge px-2.5 py-2 text-[11px] text-tp-ink">
          <div className="mb-1 font-mono text-tp-ink-3">{t("chat.toolArgsLabel")}</div>
          <pre className="max-h-[280px] overflow-auto rounded bg-tp-glass-inner/80 p-2 font-mono text-[11px] leading-snug text-tp-ink">
            {argsPretty || t("chat.toolEmpty")}
          </pre>
          {tool.resultPreview ? (
            <>
              <div className="mt-2 mb-1 font-mono text-tp-ink-3">{t("chat.toolResultLabel")}</div>
              <pre className="max-h-[280px] overflow-auto rounded bg-tp-glass-inner/80 p-2 font-mono text-[11px] leading-snug text-tp-ink">
                {tool.resultPreview}
              </pre>
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
