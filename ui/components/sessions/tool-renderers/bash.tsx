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
      <div
        className={cn(
          "rounded-md border border-amber-100/50 bg-black/80 px-3 py-2",
          "font-mono text-amber-100",
          "dark:border-white/5",
        )}
      >
        <div className="flex items-start gap-2">
          <Terminal className="mt-0.5 size-3.5 shrink-0 text-amber-400" aria-hidden />
          <span className="select-none text-amber-400">$</span>
          <span className="whitespace-pre-wrap break-words">{command || "(empty)"}</span>
        </div>
        {args.description && (
          <div className="mt-1 pl-6 text-[10px] italic text-amber-200/60">
            {args.description}
          </div>
        )}
      </div>
      {output !== undefined && output !== "" && (
        <pre
          className={cn(
            "max-h-80 overflow-auto rounded-md border px-2 py-1.5 font-mono whitespace-pre-wrap break-words",
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
