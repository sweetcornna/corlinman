"use client";

import * as React from "react";
import { cn } from "@/lib/utils";
import { GlassPanel } from "./glass-panel";

/**
 * **Inline** detail drawer — used on Logs, Approvals, Nodes, etc where
 * inspecting a selected row needs a persistent side pane that fills the
 * right edge of the layout. Not a modal dialog (for that, use
 * `components/ui/drawer.tsx`, which is Radix-Dialog based).
 *
 * Designed to sit alongside a scroll-container in a CSS grid:
 *
 *     <div className="grid grid-cols-[1fr_380px] gap-3">
 *       <LogList />
 *       <DetailDrawer title="…"> … </DetailDrawer>
 *     </div>
 *
 * Features:
 *   - Sections: `<DetailDrawer.Section label="Payload">…</Section>` for
 *     consistent vertical rhythm.
 *   - Header meta: renders a severity pill + timestamp + relative-time
 *     in a canonical layout.
 *
 * Closing: delegated to the parent (this isn't a floating overlay).
 * Most pages use the "click the same row again to close" pattern.
 */

export interface DetailDrawerProps
  extends Omit<React.HTMLAttributes<HTMLElement>, "title"> {
  /** Header H1. Can include inline <code> etc. */
  title: React.ReactNode;
  /** Optional subtitle (usually the subsystem path). Shown in amber. */
  subsystem?: string;
  /** Optional header meta row above the title (timestamp, severity, etc). */
  meta?: React.ReactNode;
  /** Optional trace-id block with copy affordance. */
  trace?: { id: string; onCopy?: (id: string) => void; label?: string };
}

function DetailDrawerRoot({
  title,
  subsystem,
  meta,
  trace,
  className,
  children,
  ...rest
}: DetailDrawerProps) {
  return (
    <GlassPanel
      as="section"
      className={cn(
        "flex flex-col overflow-hidden",
        className,
      )}
      {...rest}
    >
      <div className="flex flex-col gap-2.5 border-b border-sg-border p-4">
        {meta ? <div className="flex flex-wrap items-center gap-2">{meta}</div> : null}
        {subsystem ? (
          <div className="font-mono text-[12px] text-sg-accent">{subsystem}</div>
        ) : null}
        <h2 className="text-[16px] font-medium leading-snug tracking-[-0.01em] text-sg-ink">
          {title}
        </h2>
        {trace ? <TraceRow trace={trace} /> : null}
      </div>
      <div className="flex flex-1 flex-col overflow-y-auto">{children}</div>
    </GlassPanel>
  );
}

function TraceRow({ trace }: { trace: NonNullable<DetailDrawerProps["trace"]> }) {
  const [copied, setCopied] = React.useState(false);
  return (
    <div className="flex items-center gap-2 font-mono text-[11px] text-sg-ink-3">
      <span>{trace.label ?? "trace_id"}:</span>
      <span className="font-medium text-sg-ink">{trace.id}</span>
      <button
        type="button"
        onClick={() => {
          try {
            void navigator.clipboard?.writeText(trace.id);
          } catch {
            // non-fatal
          }
          trace.onCopy?.(trace.id);
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        }}
        className={cn(
          "ml-auto rounded-sg-sm border px-2 py-[2px] text-[10px]",
          "bg-sg-inset border-sg-border text-sg-ink-3",
          "hover:bg-sg-inset-hover hover:text-sg-ink",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40",
        )}
      >
        {copied ? "copied ✓" : "copy"}
      </button>
    </div>
  );
}

export interface DetailDrawerSectionProps
  extends React.HTMLAttributes<HTMLDivElement> {
  label: string;
}

function Section({
  label,
  className,
  children,
  ...rest
}: DetailDrawerSectionProps) {
  return (
    <div
      className={cn(
        "border-b border-sg-border p-4 last:border-b-0",
        className,
      )}
      {...rest}
    >
      <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.1em] text-sg-ink-4">
        {label}
      </div>
      {children}
    </div>
  );
}

export const DetailDrawer = Object.assign(DetailDrawerRoot, { Section });
export default DetailDrawer;
