/**
 * WebFetch renderer — URL + status (from output if available).
 */
"use client";

import * as React from "react";
import { Globe } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ToolRendererProps } from "./generic";

interface WebArgs {
  url?: string;
  prompt?: string;
}

function parseArgs(raw: string): WebArgs {
  if (!raw) return {};
  try {
    return JSON.parse(raw) as WebArgs;
  } catch {
    return {};
  }
}

const STATUS_LINE = /^\s*(HTTP\/[\d.]+\s+)?(\d{3})\b/;

export function WebFetchToolRenderer({ inputJson, output, isError }: ToolRendererProps) {
  const args = parseArgs(inputJson);
  const url = args.url ?? "";
  const statusMatch = output ? output.match(STATUS_LINE) : null;
  const status = statusMatch ? statusMatch[2] : null;

  return (
    <div className="space-y-2 text-xs">
      <div
        className={cn(
          "flex items-center gap-2 rounded-md border border-amber-100/50 bg-amber-50/30 px-2.5 py-1.5",
          "dark:border-white/5 dark:bg-black/30",
          "font-mono text-amber-950/80 dark:text-amber-100/70",
        )}
      >
        <Globe className="size-3.5 shrink-0 opacity-70" aria-hidden />
        <span className="truncate">{url || "(no url)"}</span>
        {status && (
          <span
            className={cn(
              "ml-auto shrink-0 rounded-sm px-1.5 py-0.5 text-[10px]",
              status.startsWith("2")
                ? "bg-emerald-200/40 text-emerald-900 dark:bg-emerald-700/30 dark:text-emerald-200"
                : "bg-red-200/40 text-red-900 dark:bg-red-700/30 dark:text-red-200",
            )}
          >
            {status}
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
