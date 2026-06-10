/**
 * Bash / shell command renderer — `$ {command}` header + stdout monospace.
 */
"use client";

import * as React from "react";
import { Terminal } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ToolRendererProps } from "./generic";

interface BashArgs {
  command?: string;
  cmd?: string;
  description?: string;
}

function parseArgs(raw: string): BashArgs {
  if (!raw) return {};
  try {
    return JSON.parse(raw) as BashArgs;
  } catch {
    return {};
  }
}

export function BashToolRenderer({ inputJson, output, isError }: ToolRendererProps) {
  const args = parseArgs(inputJson);
  const command = args.command ?? args.cmd ?? "";

  return (
    <div className="space-y-2 text-xs">
      <div className="rounded-sg-md border border-sg-border bg-sg-inset-strong px-3 py-2 font-mono text-[12.5px] text-sg-ink-2">
        <div className="flex items-start gap-2">
          <Terminal className="mt-0.5 size-3.5 shrink-0 text-sg-accent" aria-hidden />
          <span className="select-none text-sg-accent">$</span>
          <span className="whitespace-pre-wrap break-words">{command || "(empty)"}</span>
        </div>
        {args.description && (
          <div className="mt-1 pl-6 text-[10px] italic text-sg-ink-4">
            {args.description}
          </div>
        )}
      </div>
      {output !== undefined && output !== "" && (
        <pre
          className={cn(
            "max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-sg-md border px-2 py-1.5 font-mono text-[12.5px]",
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
