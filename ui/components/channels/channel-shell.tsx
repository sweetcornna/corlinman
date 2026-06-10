"use client";

import * as React from "react";
import Link from "next/link";
import { motion } from "framer-motion";
import {
  AtSign,
  Bot,
  Hash,
  MessageCircle,
  MessageSquare,
  MessageSquareText,
  Radio,
  Send,
  type LucideIcon,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { LiveDot } from "@/components/ui/live-dot";
import { useMotion } from "@/components/ui/motion-safe";

/**
 * Maps a stable channel id to the same lucide glyph the sidebar uses, so the
 * page-header identity tile reads as the same product surface as the nav.
 * Falls back to a generic broadcast icon for ids we don't know.
 */
const CHANNEL_ICON: Record<string, LucideIcon> = {
  qq: MessageCircle,
  telegram: Send,
  discord: Hash,
  slack: AtSign,
  feishu: MessageSquareText,
  wechat_official: MessageSquare,
  qq_official: Bot,
};

/**
 * Single tab entry for the sub-nav beneath the title. `id` drives the
 * `data-state` + active-underline animation; `href` is optional so tabs can
 * be pure state toggles (caller owns activation via `activeTabId`).
 */
export interface ChannelShellTab {
  id: string;
  label: string;
  href?: string;
}

export interface ChannelShellProps {
  /** Stable channel identifier (e.g. "qq", "telegram"). */
  channelId: "qq" | "telegram" | (string & {});
  /** Bold title shown top-left. */
  title: string;
  /** Optional subtitle beneath the title. */
  subtitle?: string;
  /** Connection state drives the LiveDot variant (ok / err). */
  connected: boolean;
  /** Overrides the default "Live" / "Offline" label next to the dot. */
  connectionLabel?: string;
  /** Optional header actions rendered top-right (e.g. Reconnect button). */
  actions?: React.ReactNode;
  /** Optional tab bar under the title. */
  tabs?: ChannelShellTab[];
  /** The id of the currently active tab — matched against `tabs[].id`. */
  activeTabId?: string;
  /** Called when a non-linked tab is clicked. */
  onTabChange?: (tabId: string) => void;
  /** Page body. */
  children: React.ReactNode;
}

/**
 * Shared chrome for channel admin pages.
 *
 * Top bar: title + subtitle + LiveDot connection indicator, with optional
 * `actions` slot on the right.
 *
 * Tab bar: optional; the active tab grows a shared-`layoutId` underline that
 * animates between tabs via framer-motion. Collapses to an instant swap
 * under `prefers-reduced-motion`.
 */
export function ChannelShell({
  channelId,
  title,
  subtitle,
  connected,
  connectionLabel,
  actions,
  tabs,
  activeTabId,
  onTabChange,
  children,
}: ChannelShellProps) {
  const { reduced } = useMotion();

  const label = connectionLabel ?? (connected ? "Live" : "Offline");
  const variant = connected ? "ok" : "err";
  const Icon = CHANNEL_ICON[channelId] ?? Radio;

  return (
    <div
      className="flex flex-col gap-4"
      data-channel-id={channelId}
      data-testid={`channel-shell-${channelId}`}
    >
      <header className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 items-start gap-3.5">
          {/* Channel identity tile — accent-soft well with the nav glyph. */}
          <span
            aria-hidden
            className="mt-0.5 grid h-11 w-11 shrink-0 place-items-center rounded-sg-md border border-sg-border bg-sg-accent-soft text-sg-accent shadow-sg-1"
          >
            <Icon className="h-5 w-5" />
          </span>
          <div className="min-w-0 space-y-1">
            <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
              <h1 className="text-2xl font-semibold tracking-tight text-sg-ink">
                {title}
              </h1>
              {/* Live status pill: glow dot + label, breathing ring when on. */}
              <span
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5",
                  "text-[11px] font-medium uppercase tracking-wider",
                  connected
                    ? "border-sg-ok/25 bg-sg-ok-soft text-sg-ok"
                    : "border-sg-border bg-sg-inset text-sg-ink-4",
                )}
              >
                <span
                  aria-hidden
                  className={cn(
                    "h-1.5 w-1.5 rounded-full",
                    connected ? "bg-sg-ok tp-breathe" : "bg-sg-ink-5",
                  )}
                />
                <LiveDot
                  variant={variant}
                  pulse={false}
                  label={label}
                  className="sr-only"
                  data-testid="channel-shell-live-dot"
                />
                {label}
              </span>
            </div>
            {subtitle ? (
              <p className="text-sm text-sg-ink-3">{subtitle}</p>
            ) : null}
          </div>
        </div>
        {actions ? (
          <div className="flex shrink-0 items-center gap-2">{actions}</div>
        ) : null}
      </header>

      {tabs && tabs.length > 0 ? (
        <div
          role="tablist"
          aria-label={`${title} sections`}
          className="flex items-center gap-1 border-b border-sg-border"
        >
          {tabs.map((tab) => {
            const active = tab.id === activeTabId;
            const className = cn(
              "relative inline-flex h-9 items-center px-3 text-sm font-medium transition-colors",
              active
                ? "text-sg-ink"
                : "text-sg-ink-4 hover:text-sg-ink",
            );
            const underline = active ? (
              reduced ? (
                <span
                  aria-hidden
                  data-testid="channel-shell-tab-underline"
                  className="absolute inset-x-0 bottom-0 h-[2px] rounded-full bg-sg-accent"
                />
              ) : (
                <motion.span
                  aria-hidden
                  layoutId="channel-tab-underline"
                  data-testid="channel-shell-tab-underline"
                  className="absolute inset-x-0 bottom-0 h-[2px] rounded-full bg-sg-accent"
                  transition={{
                    type: "spring",
                    stiffness: 500,
                    damping: 40,
                    mass: 0.6,
                  }}
                />
              )
            ) : null;

            if (tab.href) {
              return (
                <Link
                  key={tab.id}
                  href={tab.href as never}
                  role="tab"
                  aria-selected={active}
                  data-state={active ? "active" : "inactive"}
                  className={className}
                >
                  {tab.label}
                  {underline}
                </Link>
              );
            }
            return (
              <button
                key={tab.id}
                type="button"
                role="tab"
                aria-selected={active}
                data-state={active ? "active" : "inactive"}
                onClick={() => onTabChange?.(tab.id)}
                className={className}
              >
                {tab.label}
                {underline}
              </button>
            );
          })}
        </div>
      ) : null}

      <div className="flex flex-col gap-4">{children}</div>
    </div>
  );
}

export default ChannelShell;
