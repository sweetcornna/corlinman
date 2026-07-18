/**
 * WebFetch renderer — URL + status (from output if available).
 */
"use client";

import * as React from "react";
import { Globe } from "@/components/icons";
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
      <div className="flex items-center gap-2 rounded-sg-md border border-sg-border bg-sg-inset px-2.5 py-1.5 font-mono text-[12.5px] text-sg-ink-3">
        <Globe className="size-3.5 shrink-0 text-sg-ink-4" aria-hidden />
        <span className="truncate text-sg-ink-2">{url || "(no url)"}</span>
        {status && (
          <span
            className={cn(
              "ml-auto shrink-0 rounded-sg-sm border px-1.5 py-0.5 text-[10px]",
              status.startsWith("2")
                ? "border-sg-ok/30 bg-sg-ok-soft text-sg-ok"
                : "border-sg-err/30 bg-sg-err-soft text-sg-err",
            )}
          >
            {status}
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
