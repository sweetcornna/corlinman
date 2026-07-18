"use client";

/**
 * `<VersionBadge>` — TopNav version chip + dropdown panel, modeled on
 * sub2api's `VersionBadge.vue`. Evolves the old `<UpdateBubble>` (which
 * only appeared when an update existed) into an always-visible
 * `v{current}` chip:
 *
 *   - Chip: monospace current version; when a newer release exists it
 *     turns amber with a pulsing dot (respects `prefers-reduced-motion`
 *     and the localStorage dismiss stash — dismissing hides the dot, the
 *     chip itself stays).
 *   - Panel (click): ONE priority-ordered state —
 *       1. update available → tag + notes excerpt + **Update now**
 *          (one-click POST; on 202 routes to `/system?upgrade=<id>` where
 *          `<UpgradeProgress>` takes over; 409 routes to the in-flight
 *          upgrade; 503 routes to the manual commands) + details link.
 *       2. up to date → green check + link to `/system` (audit, rollback).
 *     Plus an always-present "check now" refresh action (force poll,
 *     server-throttled to 1/min → 429 surfaces as a toast).
 *
 * Dropdown mechanics (outside-click + Escape) follow the existing
 * `<ProfileSwitcher>` pattern — no new Radix dependency.
 */

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import {
  ArrowUpCircle,
  CheckCircle2,
  ExternalLink,
  Loader2,
  RefreshCcw,
} from "@/components/icons";
import { toast } from "sonner";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  checkForUpdates,
  CorlinmanApiError,
  fetchSystemInfo,
  startSystemUpgrade,
  type UpdateStatus,
} from "@/lib/api";

/** localStorage key — dismissing hides the amber dot until `latest`
 * advances past the stashed tag. Shared with the system page. */
export const DISMISS_KEY = "corlinman_update_dismissed_tag";

export interface VersionBadgeProps {
  className?: string;
}

export function VersionBadge({ className }: VersionBadgeProps) {
  const { t } = useTranslation();
  const router = useRouter();
  const qc = useQueryClient();

  const q = useQuery<UpdateStatus>({
    queryKey: ["admin", "system", "info"],
    queryFn: fetchSystemInfo,
    refetchInterval: 30_000,
    staleTime: 20_000,
    refetchOnWindowFocus: true,
    retry: false,
  });

  const [open, setOpen] = React.useState(false);
  const [checking, setChecking] = React.useState(false);
  const [starting, setStarting] = React.useState(false);
  const rootRef = React.useRef<HTMLDivElement | null>(null);

  const [dismissedTag, setDismissedTag] = React.useState<string | null>(() => {
    if (typeof window === "undefined") return null;
    try {
      return window.localStorage.getItem(DISMISS_KEY);
    } catch {
      return null;
    }
  });

  // Outside-click + Escape close (ProfileSwitcher pattern).
  React.useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Fail-silent on any error — the chip simply doesn't render (matches
  // the old bubble's behaviour; the health dot covers reachability).
  if (q.isError || !q.data) return null;
  const info = q.data;

  const hasUpdate = Boolean(info.available && info.latest);
  const dotVisible =
    hasUpdate && !(dismissedTag && dismissedTag === info.latest);

  const handleRefresh = async () => {
    if (checking) return;
    setChecking(true);
    try {
      const fresh = await checkForUpdates();
      qc.setQueryData(["admin", "system", "info"], fresh);
    } catch (err) {
      const msg =
        err instanceof CorlinmanApiError && err.status === 429
          ? t("system.badge.throttled")
          : err instanceof Error
            ? err.message
            : String(err);
      toast.error(msg);
    } finally {
      setChecking(false);
    }
  };

  const handleUpdateNow = async () => {
    if (!info.latest || starting) return;
    setStarting(true);
    try {
      const res = await startSystemUpgrade(info.latest);
      setOpen(false);
      router.push(`/system?upgrade=${encodeURIComponent(res.request_id)}`);
    } catch (err) {
      if (err instanceof CorlinmanApiError && err.status === 409) {
        // Another upgrade is already in flight — follow it instead.
        let inflightId: string | null = null;
        try {
          const parsed = JSON.parse(err.message) as {
            request_id?: string;
          };
          inflightId = parsed.request_id ?? null;
        } catch {
          /* plain-text body */
        }
        setOpen(false);
        router.push(
          inflightId
            ? `/system?upgrade=${encodeURIComponent(inflightId)}`
            : "/system",
        );
      } else if (err instanceof CorlinmanApiError && err.status === 503) {
        // One-click not wired on this deploy → the manual commands on
        // /system are the path.
        setOpen(false);
        toast.info(t("system.badge.unavailable"));
        router.push("/system");
      } else {
        toast.error(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setStarting(false);
    }
  };

  const handleDismissDot = () => {
    if (!info.latest) return;
    try {
      window.localStorage.setItem(DISMISS_KEY, info.latest);
    } catch {
      /* quota / privacy-mode — dot just reappears next session */
    }
    setDismissedTag(info.latest);
  };

  return (
    <div ref={rootRef} className={cn("relative", className)}>
      <button
        type="button"
        data-testid="version-badge"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={
          hasUpdate && info.latest
            ? t("update.bubble.label", { version: info.latest })
            : t("system.badge.currentLabel", { version: info.current })
        }
        onClick={() => setOpen((v) => !v)}
        className={cn(
          "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40",
          dotVisible
            ? "border-sg-accent/40 bg-sg-accent/10 text-sg-ink-2 hover:bg-sg-accent/20 hover:text-sg-ink"
            : "border-sg-border bg-sg-card text-sg-ink-3 hover:text-sg-ink",
        )}
      >
        {dotVisible ? (
          <span
            aria-hidden
            data-testid="version-badge-dot"
            className="inline-block h-2 w-2 rounded-full bg-sg-accent motion-safe:animate-pulse"
          />
        ) : null}
        <span className="font-mono tabular-nums">v{info.current}</span>
      </button>

      {open ? (
        <div
          role="menu"
          data-testid="version-badge-panel"
          className="absolute right-0 top-full z-50 mt-2 w-72 rounded-lg border border-sg-border bg-sg-card p-3 shadow-lg"
        >
          <div className="flex items-center justify-between gap-2">
            <span className="font-mono text-sm font-semibold text-sg-ink">
              v{info.current}
            </span>
            <button
              type="button"
              data-testid="version-badge-refresh"
              aria-label={t("system.badge.checkNow")}
              onClick={handleRefresh}
              className="inline-flex h-6 w-6 items-center justify-center rounded text-sg-ink-3 transition-colors hover:bg-sg-inset hover:text-sg-ink"
            >
              {checking ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
              ) : (
                <RefreshCcw className="h-3.5 w-3.5" aria-hidden />
              )}
            </button>
          </div>

          {hasUpdate && info.latest ? (
            <div
              data-testid="version-badge-update"
              className="mt-3 space-y-2 rounded-md border border-sg-accent/40 bg-sg-accent/5 p-3"
            >
              <p className="flex items-center gap-1.5 text-xs font-medium text-sg-ink">
                <ArrowUpCircle
                  className="h-3.5 w-3.5 text-sg-accent"
                  aria-hidden
                />
                {t("system.badge.updateAvailable", { tag: info.latest })}
              </p>
              {info.release_notes_md ? (
                <p className="line-clamp-2 text-[11px] text-sg-ink-3">
                  {info.release_notes_md.split("\n").slice(0, 2).join(" ")}
                </p>
              ) : null}
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  size="sm"
                  data-testid="version-badge-update-now"
                  onClick={handleUpdateNow}
                  disabled={starting}
                  className="gap-1.5"
                >
                  {starting ? (
                    <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
                  ) : null}
                  {starting
                    ? t("system.badge.starting")
                    : t("system.badge.updateNow")}
                </Button>
                <Link
                  href="/system"
                  onClick={() => setOpen(false)}
                  className="inline-flex items-center gap-1 text-[11px] text-sg-ink-3 underline-offset-2 hover:text-sg-ink hover:underline"
                >
                  {t("system.badge.details")}
                  <ExternalLink className="h-3 w-3" aria-hidden />
                </Link>
              </div>
              {dotVisible ? (
                <button
                  type="button"
                  data-testid="version-badge-dismiss"
                  onClick={handleDismissDot}
                  className="text-[11px] text-sg-ink-3 underline-offset-2 hover:text-sg-ink hover:underline"
                >
                  {t("update.bubble.dismiss")}
                </button>
              ) : null}
            </div>
          ) : (
            <div
              data-testid="version-badge-uptodate"
              className="mt-3 space-y-2"
            >
              <p className="flex items-center gap-1.5 text-xs text-sg-ink-2">
                <CheckCircle2
                  className="h-3.5 w-3.5 text-emerald-500"
                  aria-hidden
                />
                {t("system.version.upToDate")}
              </p>
              <Link
                href="/system"
                onClick={() => setOpen(false)}
                className="inline-flex items-center gap-1 text-[11px] text-sg-ink-3 underline-offset-2 hover:text-sg-ink hover:underline"
              >
                {t("system.badge.manage")}
                <ExternalLink className="h-3 w-3" aria-hidden />
              </Link>
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
