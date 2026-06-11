"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { GlassPanel } from "@/components/ui/glass-panel";
import { CountdownRing } from "@/components/ui/countdown-ring";
import { cn } from "@/lib/utils";
import { Checkbox } from "./Checkbox";
import type { Approval } from "./types";

/**
 * ApprovalCard — the row in the pending/decided list, rendered as a soft
 * glass panel. Contains:
 *
 *   - Leading checkbox (pending only)
 *   - Agent avatar (small amber→ember gradient dot)
 *   - plugin.tool (font-mono, the tool name is the emphasised half)
 *   - Single-line args preview (truncated)
 *   - Held-for pill — tone ramps info → warn (amber) → err (red) with time
 *   - Primary "Approve" button (amber) + outlined "Deny" button
 *   - Countdown ring (pending only)
 *
 * Interaction:
 *   - Clicking the card body (anywhere outside action buttons / checkbox)
 *     calls `onSelect(approval)` so the drawer populates.
 *   - Pressing A approves, D opens deny — handled at the page level via
 *     `selected` state + global keydown. The card shows the shortcut badge
 *     ("A") only when `isSelectedForShortcuts` to avoid visual noise.
 *
 * Accessibility:
 *   - Whole row is keyboard focusable (tabIndex=0) with role="article" so
 *     screen readers announce it as a distinct unit.
 */

const URGENT_THRESHOLD_MS = 60_000; // 1 minute — "urgent"
const WARN_THRESHOLD_MS = 3_000; // 3 seconds — "held for a moment"
const APPROVAL_TTL_MS = 5 * 60 * 1000;

export interface ApprovalCardProps {
  approval: Approval;
  now: number;
  isPending: boolean;
  isSelected: boolean;
  isActive: boolean;
  isHighlighted?: boolean;
  isFading?: boolean;
  onToggleSelect: (id: string) => void;
  onActivate: (id: string) => void;
  onApprove: (id: string) => void;
  onDeny: (id: string) => void;
  disabled?: boolean;
  /** When true, render the ⌨ A / D shortcut hints on the action buttons. */
  showShortcuts: boolean;
}

export function ApprovalCard({
  approval,
  now,
  isPending,
  isSelected,
  isActive,
  isHighlighted = false,
  isFading = false,
  onToggleSelect,
  onActivate,
  onApprove,
  onDeny,
  disabled = false,
  showShortcuts,
}: ApprovalCardProps) {
  const { t } = useTranslation();
  const heldMs = Math.max(0, now - new Date(approval.requested_at).getTime());
  const remainingMs = isPending
    ? Math.max(0, APPROVAL_TTL_MS - heldMs)
    : 0;

  const argsPreview = React.useMemo(
    () => truncateArgs(approval.args_json),
    [approval.args_json],
  );
  const heldTone = heldToneFor(heldMs);

  return (
    <GlassPanel
      as="article"
      variant="soft"
      rounded="rounded-2xl"
      role="article"
      aria-label={`${approval.plugin}.${approval.tool}`}
      tabIndex={0}
      data-active={isActive || undefined}
      onClick={() => onActivate(approval.id)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onActivate(approval.id);
        }
      }}
      className={cn(
        "group cursor-pointer p-4 transition-colors",
        "hover:bg-sg-inset-hover",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40",
        isActive && "ring-2 ring-sg-accent/55",
        isHighlighted && "ring-2 ring-sg-ok/55",
        isFading && "opacity-40",
      )}
    >
      <div className="flex items-start gap-3">
        {isPending ? (
          <span
            onClick={(e) => e.stopPropagation()}
            className="mt-1 flex shrink-0 items-center"
          >
            <Checkbox
              aria-label={t("approvals.selectOneAria", {
                plugin: approval.plugin,
                tool: approval.tool,
              })}
              checked={isSelected}
              onChange={() => onToggleSelect(approval.id)}
              disabled={disabled}
            />
          </span>
        ) : null}
        <AgentAvatar />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <span className="font-mono text-[13px] font-medium text-sg-ink">
              <span className="text-sg-accent">{approval.plugin}</span>
              <span className="text-sg-ink-4">.</span>
              {approval.tool}
            </span>
            <span className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-sg-ink-4">
              {approval.session_key || t("approvals.tp.cardNoSession")}
            </span>
            {isPending ? (
              <HeldForPill tone={heldTone} heldMs={heldMs} />
            ) : (
              <DecisionTag decision={approval.decision} />
            )}
          </div>
          <div className="mt-1.5 flex items-center gap-2 font-mono text-[11.5px] text-sg-ink-3">
            <span className="text-sg-ink-4">{t("approvals.tp.cardArgsLabel")}</span>
            <span className="truncate text-sg-ink-2">{argsPreview}</span>
          </div>
        </div>
        {isPending ? (
          <div
            className="flex shrink-0 items-center gap-2"
            onClick={(e) => e.stopPropagation()}
          >
            <ApproveButton
              onClick={() => onApprove(approval.id)}
              disabled={disabled}
              showShortcut={showShortcuts}
              shortcutKey={t("approvals.tp.shortcutApprove")}
            />
            <DenyButton
              onClick={() => onDeny(approval.id)}
              disabled={disabled}
              showShortcut={showShortcuts}
              shortcutKey={t("approvals.tp.shortcutDeny")}
            />
            <CountdownRing
              remainingMs={remainingMs}
              totalMs={APPROVAL_TTL_MS}
              size={22}
              label={`${approval.plugin}.${approval.tool} expires in`}
              className="shrink-0 opacity-75"
            />
          </div>
        ) : null}
      </div>
    </GlassPanel>
  );
}

// ─── Pieces ──────────────────────────────────────────────────────────────

function AgentAvatar() {
  return (
    <span
      aria-hidden
      className={cn(
        "mt-1 h-5 w-5 shrink-0 rounded-full",
        "bg-[linear-gradient(135deg,var(--sg-accent),var(--sg-accent-2))]",
        "shadow-[0_0_12px_-2px_color-mix(in_oklch,var(--sg-accent)_55%,transparent)]",
      )}
    />
  );
}

function ApproveButton({
  onClick,
  disabled,
  showShortcut,
  shortcutKey,
}: {
  onClick: () => void;
  disabled: boolean;
  showShortcut: boolean;
  shortcutKey: string;
}) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={t("approvals.approve")}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-[12px] font-medium",
        "bg-sg-accent text-primary-foreground shadow-sg-primary",
        "transition-transform duration-150 hover:-translate-y-[1px] active:translate-y-0",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/55",
        "disabled:pointer-events-none disabled:opacity-50",
      )}
    >
      {t("approvals.approve")}
      {showShortcut ? <KbdBadge tone="light">{shortcutKey}</KbdBadge> : null}
    </button>
  );
}

function DenyButton({
  onClick,
  disabled,
  showShortcut,
  shortcutKey,
}: {
  onClick: () => void;
  disabled: boolean;
  showShortcut: boolean;
  shortcutKey: string;
}) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={t("approvals.deny")}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-[12px] font-medium",
        "border-sg-err/40 bg-transparent text-sg-err",
        "hover:bg-sg-err-soft",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-err/50",
        "disabled:pointer-events-none disabled:opacity-50",
      )}
    >
      {t("approvals.deny")}
      {showShortcut ? <KbdBadge tone="dark">{shortcutKey}</KbdBadge> : null}
    </button>
  );
}

function KbdBadge({
  children,
  tone,
}: {
  children: React.ReactNode;
  tone: "light" | "dark";
}) {
  return (
    <kbd
      aria-hidden
      className={cn(
        "inline-flex h-[15px] min-w-[15px] items-center justify-center rounded px-1",
        "font-mono text-[9.5px] font-medium",
        tone === "light"
          ? "bg-black/10 text-black/65"
          : "bg-sg-err/10 text-sg-err",
      )}
    >
      {children}
    </kbd>
  );
}

function HeldForPill({
  tone,
  heldMs,
}: {
  tone: "info" | "warn" | "err";
  heldMs: number;
}) {
  const { t } = useTranslation();
  const toneClass: Record<typeof tone, string> = {
    info: "border-sg-border bg-sg-inset text-sg-ink-3",
    warn: "border-sg-warn/30 bg-sg-warn-soft text-sg-warn",
    err: "border-sg-err/40 bg-sg-err-soft text-sg-err",
  };
  const secs = Math.floor(heldMs / 1000);
  const label =
    heldMs < 1_000
      ? t("approvals.tp.heldForNow")
      : heldMs >= 60_000
        ? t("approvals.tp.heldForMin", { m: Math.floor(heldMs / 60_000) })
        : t("approvals.tp.heldForSec", { s: secs });
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-[2px]",
        "font-mono text-[10px] tabular-nums",
        toneClass[tone],
      )}
    >
      {label}
    </span>
  );
}

function DecisionTag({ decision }: { decision: string | null }) {
  const { t } = useTranslation();
  if (!decision) {
    return (
      <span
        className={cn(
          "rounded-full border border-sg-border bg-sg-inset px-2 py-[2px]",
          "font-mono text-[10px] text-sg-ink-3",
        )}
      >
        {t("approvals.statusPending")}
      </span>
    );
  }
  if (decision === "approved") {
    return (
      <span
        className={cn(
          "rounded-full border border-sg-ok/35 bg-sg-ok-soft px-2 py-[2px]",
          "font-mono text-[10px] text-sg-ok",
        )}
      >
        {t("approvals.statusApproved")}
      </span>
    );
  }
  if (decision === "denied") {
    return (
      <span
        className={cn(
          "rounded-full border border-sg-err/40 bg-sg-err-soft px-2 py-[2px]",
          "font-mono text-[10px] text-sg-err",
        )}
      >
        {t("approvals.statusDenied")}
      </span>
    );
  }
  return (
    <span
      className={cn(
        "rounded-full border border-sg-border bg-sg-inset px-2 py-[2px]",
        "font-mono text-[10px] text-sg-ink-3",
      )}
    >
      {decision}
    </span>
  );
}

// ─── helpers ─────────────────────────────────────────────────────────────

function heldToneFor(heldMs: number): "info" | "warn" | "err" {
  if (heldMs >= URGENT_THRESHOLD_MS) return "err";
  if (heldMs >= WARN_THRESHOLD_MS) return "warn";
  return "info";
}

const ARGS_PREVIEW_LIMIT = 80;

function truncateArgs(raw: string): string {
  try {
    const serialized = JSON.stringify(JSON.parse(raw));
    return serialized.length > ARGS_PREVIEW_LIMIT
      ? serialized.slice(0, ARGS_PREVIEW_LIMIT) + "…"
      : serialized;
  } catch {
    return raw.length > ARGS_PREVIEW_LIMIT
      ? raw.slice(0, ARGS_PREVIEW_LIMIT) + "…"
      : raw;
  }
}
