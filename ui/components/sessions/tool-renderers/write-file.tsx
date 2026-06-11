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
      <div className="flex items-center gap-2 rounded-sg-md border border-sg-border bg-sg-inset px-2.5 py-1.5 font-mono text-[12.5px] text-sg-ink-3">
        <FilePlus2 className="size-3.5 shrink-0 text-sg-ink-4" aria-hidden />
        <span className="truncate text-sg-ink-2">{path || "(no path)"}</span>
        {size > 0 && (
          <span className="ml-auto shrink-0 text-[10px] text-sg-ink-4">{humanBytes(size)}</span>
        )}
      </div>
      {output !== undefined && output !== "" && (
        <pre
          className={cn(
            "max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-sg-md border px-2 py-1.5 font-mono text-[12.5px]",
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
