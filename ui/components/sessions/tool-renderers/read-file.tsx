/**
 * Read tool renderer — file path + optional offset/limit line range.
 */
"use client";

import * as React from "react";
import { FileText } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ToolRendererProps } from "./generic";

interface ReadArgs {
  file_path?: string;
  path?: string;
  offset?: number;
  limit?: number;
}

function parseArgs(raw: string): ReadArgs {
  if (!raw) return {};
  try {
    return JSON.parse(raw) as ReadArgs;
  } catch {
    return {};
  }
}

export function ReadFileToolRenderer({ inputJson, output, isError }: ToolRendererProps) {
  const args = parseArgs(inputJson);
  const path = args.file_path ?? args.path ?? "";
  const range =
    args.offset !== undefined || args.limit !== undefined
      ? `lines ${args.offset ?? 1}${args.limit ? `..${(args.offset ?? 0) + args.limit}` : "+"}`
      : null;

  return (
    <div className="space-y-2 text-xs">
      <div
        className={cn(
          "flex items-center gap-2 rounded-md border border-amber-100/50 bg-amber-50/30 px-2.5 py-1.5",
          "dark:border-white/5 dark:bg-black/30",
          "font-mono text-amber-950/80 dark:text-amber-100/70",
        )}
      >
        <FileText className="size-3.5 shrink-0 opacity-70" aria-hidden />
        <span className="truncate">{path || "(no path)"}</span>
        {range && (
          <span className="ml-auto shrink-0 text-[10px] opacity-60">{range}</span>
        )}
      </div>
      {output !== undefined && output !== "" && (
        <pre
          className={cn(
            "max-h-80 overflow-auto rounded-md border px-2 py-1.5 font-mono whitespace-pre-wrap break-words text-[11px]",
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
