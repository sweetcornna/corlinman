"use client";

/**
 * `<UpdateBubble>` — TopNav chip that lights up when a newer corlinman
 * release is available upstream. Wave 1.2 of the auto-update plan.
 *
 * Behaviour:
 *   - Polls `GET /admin/system/info` every 30s (react-query, matches the
 *     existing HealthDot pattern). 20s `staleTime` so consecutive renders
 *     don't refire the request.
 *   - Refetches whenever the tab regains visibility (`refetchOnWindowFocus`).
 *   - Renders nothing when: no update available, the latest tag is
 *     dismissed in localStorage, or the fetch errored (fail-silent).
 *   - When dismissed, the user clicks the chip's "X" → localStorage stash
 *     under `corlinman_update_dismissed_tag`. Backend will reappear the
 *     bubble automatically once `latest` advances past the stashed tag.
 *
 * Visual:
 *   - Amber dot with a 2s gentle pulse (respects `prefers-reduced-motion`).
 *   - Monospace tag chip (`vX.Y.Z`) — keyboard-focusable, clicking opens
 *     `/system` (the updates page; `/admin/*` is the API namespace).
 *   - Inline dismiss `×` button — does NOT trigger the chip's navigation.
 *
 * Accessibility:
 *   - The whole chip is a `next/link` with `aria-label` describing the
 *     update tag.
 *   - The dismiss button is a separate button with its own aria-label.
 *   - `prefers-reduced-motion` disables the pulse keyframes.
 */

import * as React from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { X } from "lucide-react";

import { cn } from "@/lib/utils";
import { fetchSystemInfo, type UpdateStatus } from "@/lib/api";

/** localStorage key — exported for tests + the future system page that
 * also writes to it from its "Dismiss until next release" button. */
export const DISMISS_KEY = "corlinman_update_dismissed_tag";

export interface UpdateBubbleProps {
  className?: string;
}

export function UpdateBubble({ className }: UpdateBubbleProps) {
  const { t } = useTranslation();

  const q = useQuery<UpdateStatus>({
    queryKey: ["admin", "system", "info"],
    queryFn: fetchSystemInfo,
    refetchInterval: 30_000,
    staleTime: 20_000,
    refetchOnWindowFocus: true,
    retry: false,
  });

  // Track the dismissed tag in component state so a click on the X
  // re-renders without waiting for the next poll. Initial value pulled
  // from localStorage (guarded for SSR).
  const [dismissedTag, setDismissedTag] = React.useState<string | null>(() => {
    if (typeof window === "undefined") return null;
    try {
      return window.localStorage.getItem(DISMISS_KEY);
    } catch {
      return null;
    }
  });

  // Fail-silent on any error.
  if (q.isError || !q.data) return null;

  const data = q.data;
  if (!data.available || !data.latest) return null;
  if (dismissedTag && dismissedTag === data.latest) return null;

  const latest = data.latest;

  const handleDismiss = (e: React.MouseEvent<HTMLButtonElement>) => {
    e.preventDefault();
    e.stopPropagation();
    try {
      window.localStorage.setItem(DISMISS_KEY, latest);
    } catch {
      /* ignore quota / privacy-mode errors — UI just won't persist */
    }
    setDismissedTag(latest);
  };

  return (
    <Link
      href="/system"
      aria-label={t("update.bubble.label", { version: latest })}
      title={t("update.bubble.tooltip")}
      data-testid="update-bubble"
      className={cn(
        "group inline-flex items-center gap-1.5 rounded-full border border-sg-accent/40 bg-sg-accent/10 px-2 py-0.5 text-xs text-sg-ink-2 transition-colors hover:bg-sg-accent/20 hover:text-sg-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40",
        className,
      )}
    >
      <span
        aria-hidden
        className={cn(
          "inline-block h-2 w-2 rounded-full bg-sg-accent",
          // Gentle pulse, disabled when the user prefers reduced motion.
          // Tailwind's `animate-pulse` is a 2s opacity loop — exactly the
          // "gentle 2s" the plan specifies; `motion-safe:` ensures we
          // respect `prefers-reduced-motion`.
          "motion-safe:animate-pulse",
        )}
      />
      <span className="font-mono tabular-nums">{latest}</span>
      <button
        type="button"
        onClick={handleDismiss}
        aria-label={t("update.bubble.dismiss")}
        data-testid="update-bubble-dismiss"
        className="-mr-0.5 ml-0.5 inline-flex h-4 w-4 items-center justify-center rounded-full text-sg-ink-3 transition-colors hover:bg-sg-accent/30 hover:text-sg-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40"
      >
        <X className="h-3 w-3" aria-hidden />
      </button>
    </Link>
  );
}
