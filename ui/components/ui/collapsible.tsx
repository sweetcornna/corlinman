"use client";

import * as React from "react";

import { ChevronDown } from "@/components/icons";
import { cn } from "@/lib/utils";

/**
 * Collapsible — Eclipse Minimal v2 disclosure for multi-sentence detail
 * that would otherwise clutter a lede or modal description. Built on the
 * native `<details>/<summary>` pair (same pattern the login page's
 * forgot-password panel established), so it works without JS state and
 * stays fully keyboard-accessible for free.
 *
 * The visible copy above stays one sentence; the folded body carries the
 * long contract (callback mechanics, security notes, format grammars).
 */
export interface CollapsibleProps {
  /** The always-visible one-line summary (the fold's label). */
  summary: React.ReactNode;
  /** Long-form body revealed on expand. */
  children: React.ReactNode;
  defaultOpen?: boolean;
  className?: string;
  contentClassName?: string;
  "data-testid"?: string;
}

export function Collapsible({
  summary,
  children,
  defaultOpen = false,
  className,
  contentClassName,
  "data-testid": testId,
}: CollapsibleProps) {
  return (
    <details
      open={defaultOpen}
      data-testid={testId}
      className={cn(
        "group rounded-sg-md border border-sg-border bg-sg-inset",
        className,
      )}
    >
      <summary
        className={cn(
          "flex cursor-pointer select-none list-none items-center gap-1.5",
          "px-3 py-2 text-xs text-sg-ink-3 transition-colors hover:text-sg-ink",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          "[&::-webkit-details-marker]:hidden",
        )}
      >
        <ChevronDown
          className="h-3.5 w-3.5 shrink-0 transition-transform duration-150 group-open:rotate-180"
          aria-hidden
        />
        {summary}
      </summary>
      <div
        className={cn(
          "px-3 pb-2.5 text-xs leading-relaxed text-sg-ink-2",
          contentClassName,
        )}
      >
        {children}
      </div>
    </details>
  );
}

export default Collapsible;
