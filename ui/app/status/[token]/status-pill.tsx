"use client";

/**
 * Read-only status pill for the public status card.
 *
 * Distinct from `components/ui/stream-pill.tsx` (which carries a Pause/Play
 * control we deliberately do NOT want on a read-only public surface). This
 * pill maps the coarse {@link StatusState} to a Spatial Glass tone + dot +
 * label, with a state-keyed breathing glow only while the agent is actively
 * working (amber breathe) — done/ok and error states are calm.
 */

import * as React from "react";
import { motion, useReducedMotion } from "framer-motion";
import { cn } from "@/lib/utils";
import { springs } from "@/lib/motion";
import type { StatusState } from "@/lib/status";

type Tone = "ok" | "active" | "error" | "muted";

interface PillConfig {
  label: string;
  tone: Tone;
  /** Breathe the pill glow while the agent is actively producing output. */
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

/* Faux-glass tinted container per tone. NO backdrop-filter (content tier). */
const containerTone: Record<Tone, string> = {
  ok: "bg-sg-ok-soft text-sg-ok border-sg-ok/30 shadow-sg-1",
  active: "bg-sg-accent-soft text-sg-accent border-sg-accent/30 shadow-sg-glow",
  error: "bg-sg-err-soft text-sg-err border-sg-err/30 shadow-sg-1",
  muted: "bg-sg-inset text-sg-ink-3 border-sg-border",
};

const dotTone: Record<Tone, string> = {
  ok: "bg-sg-ok",
  active: "bg-sg-accent",
  error: "bg-sg-err",
  muted: "bg-sg-ink-4",
};

export function StatusPill({
  state,
  className,
}: {
  state: StatusState;
  className?: string;
}) {
  const cfg = configFor(state);
  const reduced = useReducedMotion();
  return (
    <motion.div
      // Re-key on state so a transition (Working → Complete → …) pops the
      // pill with a springy overshoot — the eye is drawn to the change.
      key={String(state)}
      role="status"
      aria-live="polite"
      data-testid="status-pill"
      data-state={String(state)}
      initial={reduced ? false : { scale: 0.82, opacity: 0.6 }}
      animate={{ scale: 1, opacity: 1 }}
      transition={reduced ? { duration: 0 } : springs.bouncy}
      className={cn(
        "inline-flex items-center gap-2 rounded-full border",
        "py-[6px] pl-[11px] pr-3.5 font-mono text-[11.5px] tracking-tight",
        containerTone[cfg.tone],
        // State-keyed breathing glow: amber while working.
        cfg.breathe && "sg-breathe-accent",
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          "h-[7px] w-[7px] rounded-full",
          dotTone[cfg.tone],
          cfg.breathe ? "sg-breathe-accent" : "",
        )}
      />
      <span>{cfg.label}</span>
    </motion.div>
  );
}

export default StatusPill;
