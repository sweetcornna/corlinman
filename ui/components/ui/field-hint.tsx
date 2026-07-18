import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * One-line muted hint under a form control — the ONLY sanctioned shape
 * for per-field help. Long-form contracts (regex, config keys, API
 * details) go in `detail`, surfaced through the native title tooltip
 * instead of stacked prose; the visible line stays a single plain
 * sentence. Born from the 2026-07 copy audit: three layers of prose per
 * field is how the admin UI got cluttered.
 */
export function FieldHint({
  children,
  detail,
  className,
  id,
}: {
  children: React.ReactNode;
  /** Optional long-form detail shown as a hover tooltip (native title). */
  detail?: string;
  className?: string;
  id?: string;
}) {
  return (
    <p
      id={id}
      className={cn("text-xs text-muted-foreground", className)}
      title={detail}
    >
      {children}
      {detail ? (
        <span aria-hidden className="ml-1 cursor-help select-none opacity-60">
          ⓘ
        </span>
      ) : null}
    </p>
  );
}

export default FieldHint;
