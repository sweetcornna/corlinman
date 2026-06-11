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
      <div className="flex items-center gap-2 rounded-sg-md border border-sg-border bg-sg-inset px-2.5 py-1.5 font-mono text-[12.5px] text-sg-ink-3">
        <FileText className="size-3.5 shrink-0 text-sg-ink-4" aria-hidden />
        <span className="truncate text-sg-ink-2">{path || "(no path)"}</span>
        {range && (
          <span className="ml-auto shrink-0 text-[10px] text-sg-ink-4">{range}</span>
        )}
      </div>
      {output !== undefined && output !== "" && (
        <pre
          className={cn(
            "max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-sg-md border px-2 py-1.5 font-mono text-[11px]",
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
