/**
 * Grep renderer — pattern + match count.
 */
"use client";

import * as React from "react";
import { Search } from "lucide-react";
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
      <div
        className={cn(
          "flex items-center gap-2 rounded-md border border-amber-100/50 bg-amber-50/30 px-2.5 py-1.5",
          "dark:border-white/5 dark:bg-black/30",
          "font-mono text-amber-950/80 dark:text-amber-100/70",
        )}
      >
        <Search className="size-3.5 shrink-0 opacity-70" aria-hidden />
        <span className="truncate">{args.pattern ?? "(no pattern)"}</span>
        {(args.path || args.glob) && (
          <span className="shrink-0 text-[10px] opacity-60">
            {args.glob ? `glob:${args.glob}` : ""} {args.path ?? ""}
          </span>
        )}
        {count !== null && (
          <span className="ml-auto shrink-0 rounded-sm bg-amber-200/40 px-1.5 py-0.5 text-[10px] text-amber-900 dark:bg-amber-700/30 dark:text-amber-200">
            {t("sessions.tools.matchCount", { n: count })}
          </span>
        )}
      </div>
      {output !== undefined && output !== "" && (
        <pre
          className={cn(
            "max-h-60 overflow-auto rounded-md border px-2 py-1.5 font-mono whitespace-pre-wrap break-words",
            isError
              ? "border-red-200/60 bg-red-50/40 text-red-900 dark:border-red-400/20 dark:bg-red-950/30 dark:text-red-200"
              : "border-amber-100/50 bg-amber-50/30 text-amber-950/80 dark:border-white/5 dark:bg-black/30 dark:text-amber-100/70",
          )}
        >
          {output}
        </pre>
      )}
    </div>
  );
}
