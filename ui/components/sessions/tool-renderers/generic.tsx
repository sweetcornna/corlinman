/**
 * Fallback tool renderer — JSON args + result `<pre>` blocks.
 *
 * Used whenever a tool name doesn't match a specialized renderer in
 * `./index.ts`.
 */
"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

export interface ToolRendererProps {
  toolName: string;
  inputJson: string;
  output?: string;
  isError?: boolean;
}

function tryPrettyJson(raw: string): string {
  if (!raw) return "";
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

export function GenericToolRenderer({ inputJson, output, isError }: ToolRendererProps) {
  const pretty = tryPrettyJson(inputJson);
  return (
    <div className="space-y-2 text-xs">
      {pretty && (
        <pre
          className={cn(
            "max-h-64 overflow-auto rounded-md border border-amber-100/50 bg-amber-50/30",
            "dark:border-white/5 dark:bg-black/30",
            "px-2 py-1.5 font-mono whitespace-pre-wrap break-words",
            "text-amber-950/80 dark:text-amber-100/70",
          )}
        >
          {pretty}
        </pre>
      )}
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
