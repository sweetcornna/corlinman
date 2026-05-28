"use client";

/**
 * Collapsible "thinking" block for Claude extended-thinking / o1 reasoning
 * traces. Default-collapsed; subtle styling so it doesn't dominate the bubble.
 */

import * as React from "react";
import { Brain, ChevronDown, ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";

interface ReasoningBlockProps {
  text: string;
  streaming?: boolean;
}

export function ReasoningBlock({ text, streaming }: ReasoningBlockProps) {
  const [expanded, setExpanded] = React.useState(false);
  return (
    <div
      className={cn(
        "my-2 overflow-hidden rounded-md border border-dashed border-tp-glass-edge bg-tp-glass-inner/30",
      )}
      data-testid="reasoning-block"
    >
      <button
        type="button"
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[11px] text-tp-ink-3 hover:bg-tp-glass-inner/60"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        {expanded ? (
          <ChevronDown className="h-3 w-3" aria-hidden="true" />
        ) : (
          <ChevronRight className="h-3 w-3" aria-hidden="true" />
        )}
        <Brain className="h-3 w-3" aria-hidden="true" />
        <span className="italic">
          {streaming ? "Thinking…" : "Thought process"}
        </span>
        <span className="ml-auto font-mono">{text.length} chars</span>
      </button>
      {expanded ? (
        <div className="border-t border-tp-glass-edge px-2.5 py-2 font-mono text-[11px] leading-relaxed whitespace-pre-wrap text-tp-ink-2 italic">
          {text}
        </div>
      ) : null}
    </div>
  );
}
