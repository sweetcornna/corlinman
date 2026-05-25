/**
 * Write/Edit tool renderer — path + byte size summary.
 */
"use client";

import * as React from "react";
import { FilePlus2 } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ToolRendererProps } from "./generic";

interface WriteArgs {
  file_path?: string;
  path?: string;
  content?: string;
  new_string?: string;
}

function parseArgs(raw: string): WriteArgs {
  if (!raw) return {};
  try {
    return JSON.parse(raw) as WriteArgs;
  } catch {
    return {};
  }
}

function humanBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function WriteFileToolRenderer({ inputJson, output, isError }: ToolRendererProps) {
  const args = parseArgs(inputJson);
  const path = args.file_path ?? args.path ?? "";
  const body = args.content ?? args.new_string ?? "";
  const size = body ? new Blob([body]).size : 0;

  return (
    <div className="space-y-2 text-xs">
      <div
        className={cn(
          "flex items-center gap-2 rounded-md border border-amber-100/50 bg-amber-50/30 px-2.5 py-1.5",
          "dark:border-white/5 dark:bg-black/30",
          "font-mono text-amber-950/80 dark:text-amber-100/70",
        )}
      >
        <FilePlus2 className="size-3.5 shrink-0 opacity-70" aria-hidden />
        <span className="truncate">{path || "(no path)"}</span>
        {size > 0 && (
          <span className="ml-auto shrink-0 text-[10px] opacity-60">{humanBytes(size)}</span>
        )}
      </div>
      {output !== undefined && output !== "" && (
        <pre
          className={cn(
            "max-h-40 overflow-auto rounded-md border px-2 py-1.5 font-mono whitespace-pre-wrap break-words",
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
