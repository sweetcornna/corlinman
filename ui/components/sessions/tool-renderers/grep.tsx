/**
 * Grep renderer — pattern + match count.
 */
"use client";

import * as React from "react";
import { Search } from "@/components/icons";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import type { ToolRendererProps } from "./generic";

interface GrepArgs {
  pattern?: string;
  path?: string;
  glob?: string;
}

function parseArgs(raw: string): GrepArgs {
  if (!raw) return {};
  try {
    return JSON.parse(raw) as GrepArgs;
  } catch {
    return {};
  }
}

function matchCount(out: string): number | null {
  if (!out) return null;
  const lines = out.split(/\r?\n/).filter((l) => l.trim().length > 0);
  return lines.length;
}

export function GrepToolRenderer({ inputJson, output, isError }: ToolRendererProps) {
  const { t } = useTranslation();
  const args = parseArgs(inputJson);
  const count = output ? matchCount(output) : null;

  return (
    <div className="space-y-2 text-xs">
      <div className="flex items-center gap-2 rounded-sg-md border border-sg-border bg-sg-inset px-2.5 py-1.5 font-mono text-[12.5px] text-sg-ink-3">
        <Search className="size-3.5 shrink-0 text-sg-ink-4" aria-hidden />
        <span className="truncate text-sg-ink-2">{args.pattern ?? "(no pattern)"}</span>
        {(args.path || args.glob) && (
          <span className="shrink-0 text-[10px] text-sg-ink-4">
            {args.glob ? `glob:${args.glob}` : ""} {args.path ?? ""}
          </span>
        )}
        {count !== null && (
          <span className="ml-auto shrink-0 rounded-sg-sm border border-sg-accent/30 bg-sg-accent-soft px-1.5 py-0.5 text-[10px] text-sg-accent">
            {t("sessions.tools.matchCount", { n: count })}
          </span>
        )}
      </div>
      {output !== undefined && output !== "" && (
        <pre
          className={cn(
            "max-h-60 overflow-auto whitespace-pre-wrap break-words rounded-sg-md border px-2 py-1.5 font-mono text-[12.5px]",
            isError
              ? "border-sg-err/30 bg-sg-err-soft text-sg-err"
              : "border-sg-border bg-sg-inset text-sg-ink-3",
          )}
        >
          {output}
        </pre>
      )}
    </div>
  );
}
