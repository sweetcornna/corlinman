"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Loader2,
  Wrench,
  XCircle,
} from "@/components/icons";

import { cn } from "@/lib/utils";
import { springs } from "@/lib/motion";
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
  const reducedMotion = useReducedMotion();
  const [expanded, setExpanded] = React.useState(defaultExpanded);

  const statusIcon =
    tool.status === "running" ? (
      <Loader2 className="h-3.5 w-3.5 animate-spin text-sg-accent" />
    ) : tool.status === "ok" ? (
      <CheckCircle2 className="h-3.5 w-3.5 text-sg-ok" />
    ) : tool.status === "error" ? (
      <XCircle className="h-3.5 w-3.5 text-sg-err" />
    ) : tool.status === "settled" ? (
      <CheckCircle2 className="h-3.5 w-3.5 text-sg-ink-4" />
    ) : (
      <AlertCircle className="h-3.5 w-3.5 text-sg-ink-4" />
    );

  const argsPretty = React.useMemo(() => {
    try {
      return JSON.stringify(JSON.parse(tool.argsJson || "{}"), null, 2);
    } catch {
      return tool.argsJson;
    }
  }, [tool.argsJson]);

  return (
    <motion.div
      layout={reducedMotion ? false : "position"}
      transition={springs.soft}
      className={cn(
        "my-2 overflow-hidden rounded-sg-md sg-inset",
        tool.status === "error" && "border-sg-err/40",
      )}
      data-testid="tool-call-card"
      data-tool-name={tool.toolName}
      data-status={tool.status}
    >
      <button
        type="button"
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[12.5px] text-sg-ink hover:bg-sg-inset-hover"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        {expanded ? (
          <ChevronDown className="h-3.5 w-3.5 text-sg-ink-4" aria-hidden="true" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 text-sg-ink-4" aria-hidden="true" />
        )}
        <Wrench className="h-3.5 w-3.5 text-sg-ink-4" aria-hidden="true" />
        <span className="font-mono text-[12.5px] text-sg-ink">{tool.toolName}</span>
        {tool.pluginName ? (
          <span className="text-sg-ink-4">· {tool.pluginName}</span>
        ) : null}
        <span className="ml-auto flex items-center gap-1 text-[11px] text-sg-ink-5">
          {statusIcon}
          {tool.durationMs ? (
            <time className="font-mono">{formatDuration(tool.durationMs)}</time>
          ) : null}
        </span>
      </button>
      <AnimatePresence initial={false}>
        {expanded ? (
          <motion.div
            key="tool-body"
            initial={reducedMotion ? false : { height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={reducedMotion ? { opacity: 0 } : { height: 0, opacity: 0 }}
            transition={reducedMotion ? { duration: 0 } : springs.soft}
            className="overflow-hidden"
          >
            <div className="border-t border-sg-border px-2.5 py-2 text-[11px] text-sg-ink">
              <div className="mb-1 font-mono text-sg-ink-4">{t("chat.toolArgsLabel")}</div>
              <pre className="max-h-[280px] overflow-auto rounded-sg-sm bg-sg-inset p-2 font-mono text-[12px] leading-snug text-sg-ink">
                {argsPretty || t("chat.toolEmpty")}
              </pre>
              {tool.resultPreview ? (
                <>
                  <div className="mt-2 mb-1 font-mono text-sg-ink-4">{t("chat.toolResultLabel")}</div>
                  <pre className="max-h-[280px] overflow-auto rounded-sg-sm bg-sg-inset p-2 font-mono text-[12px] leading-snug text-sg-ink">
                    {tool.resultPreview}
                  </pre>
                </>
              ) : null}
            </div>
          </motion.div>
        ) : null}
      </AnimatePresence>
    </motion.div>
  );
}
