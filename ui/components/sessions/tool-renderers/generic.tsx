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
        <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-sg-md border border-sg-border bg-sg-inset px-2 py-1.5 font-mono text-[12.5px] text-sg-ink-3">
          {pretty}
        </pre>
      )}
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
