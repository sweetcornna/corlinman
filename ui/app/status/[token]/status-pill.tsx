"use client";

/**
 * Read-only status pill for the public status card.
 *
 * Distinct from `components/ui/stream-pill.tsx` (which carries a Pause/Play
 * control we deliberately do NOT want on a read-only public surface). This
 * pill maps the coarse {@link StatusState} to a tone + dot + label, with a
 * breathing dot only while the agent is actively working.
 */

import * as React from "react";
import { cn } from "@/lib/utils";
import type { StatusState } from "@/lib/status";

type Tone = "ok" | "active" | "error" | "muted";

interface PillConfig {
  label: string;
  tone: Tone;
  /** Breathe the dot while the agent is actively producing output. */
  breathe: boolean;
}

function configFor(state: StatusState): PillConfig {
  switch (state) {
    case "running":
    case "streaming":
      return { label: "Working", tone: "active", breathe: true };
    case "cancelling":
      return { label: "Cancelling", tone: "active", breathe: true };
    case "complete":
      return { label: "Complete", tone: "ok", breathe: false };
    case "errored":
      return { label: "Errored", tone: "error", breathe: false };
    case "idle":
      return { label: "Idle", tone: "muted", breathe: false };
    default:
      // Unknown / future state — neutral, never crash.
      return { label: String(state || "Unknown"), tone: "muted", breathe: false };
  }
}

const containerTone: Record<Tone, string> = {
  ok: "bg-tp-ok-soft text-tp-ok border-tp-ok/25",
  active: "bg-tp-warn-soft text-tp-warn border-tp-warn/25",
  error: "border-destructive/30 bg-destructive/10 text-destructive",
  muted: "bg-tp-glass-inner text-tp-ink-3 border-tp-glass-edge",
};

const dotTone: Record<Tone, string> = {
  ok: "bg-tp-ok",
  active: "bg-tp-warn",
  error: "bg-destructive",
  muted: "bg-tp-ink-4",
};

export function StatusPill({
  state,
  className,
}: {
  state: StatusState;
  className?: string;
}) {
  const cfg = configFor(state);
  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="status-pill"
      data-state={String(state)}
      className={cn(
        "inline-flex items-center gap-2 rounded-full border",
        "py-[5px] pl-[10px] pr-3 font-mono text-[11.5px]",
        containerTone[cfg.tone],
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          "h-[7px] w-[7px] rounded-full",
          dotTone[cfg.tone],
          cfg.breathe ? "tp-breathe" : "",
        )}
      />
      <span>{cfg.label}</span>
    </div>
  );
}

export default StatusPill;
