"use client";

/**
 * `/admin/system` — version + update + upgrade-commands surface (W2.1).
 *
 * Page composition (top → bottom):
 *
 *   1. Header           — title + "Check now" CTA (force-poll the backend).
 *   2. Version card     — current + latest mono pills + last-checked relative.
 *   3. Update banner    — only when `info.available === true`; renders the
 *                         release notes via `<ReleaseNotes>` + a "Dismiss
 *                         until next release" button + GitHub deep-link.
 *   4. Upgrade card     — three tabs (Native / Docker / Docker+QQ) each
 *                         exposing one `<CopyUpgradeCommand>` block.
 *
 * Data flow:
 *   - `fetchSystemInfo()`        — react-query, 60s staleTime; no polling.
 *   - `fetchUpgradeCommands()`   — separate query so the tabs render even
 *                                  if `/info` errors (the commands are
 *                                  deterministic; they don't need an
 *                                  upstream poll to be useful).
 *   - "Check now"                — mutation around `checkForUpdates()`;
 *                                  on success → invalidate the info query
 *                                  → on 429 → warning toast.
 *   - "Dismiss until next release" — writes `DISMISS_KEY → info.latest`
 *                                    in localStorage (same key the TopNav
 *                                    `<UpdateBubble>` reads from).
 */

import * as React from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ExternalLink, RefreshCcw, Server } from "lucide-react";
import { toast } from "sonner";

import { cn } from "@/lib/utils";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  CorlinmanApiError,
  checkForUpdates,
  fetchSystemInfo,
  fetchUpgradeCommands,
  type UpdateStatus,
  type UpgradeCommands,
} from "@/lib/api";
import { DISMISS_KEY } from "@/components/system/update-bubble";
import { ReleaseNotes } from "@/components/system/release-notes";
import { CopyUpgradeCommand } from "@/components/system/copy-upgrade-command";

const SYSTEM_INFO_QUERY_KEY = ["admin", "system", "info"] as const;
const UPGRADE_COMMANDS_QUERY_KEY = [
  "admin",
  "system",
  "upgrade-commands",
] as const;

type UpgradeTabKey = "native" | "docker" | "docker_with_qq";

/** Maps a tab key → the matching `UpgradeCommands` field + i18n label key. */
const UPGRADE_TABS: ReadonlyArray<{
  key: UpgradeTabKey;
  labelKey: string;
}> = [
  { key: "native", labelKey: "system.upgrade.tabNative" },
  { key: "docker", labelKey: "system.upgrade.tabDocker" },
  { key: "docker_with_qq", labelKey: "system.upgrade.tabDockerQq" },
];

/**
 * Compact "5 minutes ago / 2 hours ago / 3 days ago" formatter that reads
 * from the `common.*` time-ago keys already used by the scheduler.
 */
function relativeFromEpoch(
  ms: number | null,
  t: (key: string, opts?: Record<string, unknown>) => string,
  now: number,
): string {
  if (!ms) return t("common.unknown");
  const delta = Math.max(0, Math.round((now - ms) / 1000));
  if (delta < 60) return t("common.secondsAgo", { n: delta });
  if (delta < 3600) return t("common.minutesAgo", { n: Math.round(delta / 60) });
  if (delta < 86_400) {
    return t("common.hoursAgo", { n: Math.round(delta / 3600) });
  }
  return t("common.daysAgo", { n: Math.round(delta / 86_400) });
}

export default function SystemPage() {
  const { t } = useTranslation();
  const qc = useQueryClient();

  // 60s "now" tick so the lastChecked relative time stays fresh without
  // an aggressive refetch. The query itself has 60s staleTime — we don't
  // poll on this page.
  const [now, setNow] = React.useState<number>(() => Date.now());
  React.useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(id);
  }, []);

  const infoQuery = useQuery<UpdateStatus>({
    queryKey: SYSTEM_INFO_QUERY_KEY,
    queryFn: fetchSystemInfo,
    staleTime: 60_000,
    retry: false,
  });

  const commandsQuery = useQuery<UpgradeCommands>({
    queryKey: UPGRADE_COMMANDS_QUERY_KEY,
    queryFn: fetchUpgradeCommands,
    staleTime: 60_000,
    retry: false,
  });

  const checkMutation = useMutation({
    mutationFn: () => checkForUpdates(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: SYSTEM_INFO_QUERY_KEY });
      qc.invalidateQueries({ queryKey: UPGRADE_COMMANDS_QUERY_KEY });
    },
    onError: (err) => {
      // 429 → server rate-limited (1/min). Anything else → generic warning.
      if (err instanceof CorlinmanApiError && err.status === 429) {
        toast.warning(t("system.version.checking"));
        return;
      }
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(msg);
    },
  });

  const info = infoQuery.data;
  const commands = commandsQuery.data;

  const lastChecked = info
    ? relativeFromEpoch(info.last_checked_at, t, now)
    : null;
  const published = info?.published_at
    ? relativeFromEpoch(info.published_at, t, now)
    : null;

  const handleDismiss = React.useCallback(() => {
    if (!info?.latest) return;
    try {
      window.localStorage.setItem(DISMISS_KEY, info.latest);
    } catch {
      /* ignore quota / privacy-mode errors */
    }
    toast.success(t("system.update.dismiss"));
  }, [info?.latest, t]);

  return (
    <div className="space-y-6" data-testid="system-page">
      {/* Header */}
      <header className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight text-tp-ink">
            {t("system.title")}
          </h1>
          <p className="max-w-2xl text-sm text-tp-ink-3">
            {t("system.subtitle")}
          </p>
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => checkMutation.mutate()}
          disabled={checkMutation.isPending}
          aria-label={t("system.update.refresh")}
          data-testid="system-check-now"
          className="gap-1.5"
        >
          <RefreshCcw
            className={cn(
              "h-3.5 w-3.5",
              checkMutation.isPending && "motion-safe:animate-spin",
            )}
            aria-hidden
          />
          <span>
            {checkMutation.isPending
              ? t("system.version.checking")
              : t("system.update.refresh")}
          </span>
        </Button>
      </header>

      {/* Version card */}
      <Card data-testid="system-version-card">
        <CardHeader className="space-y-1.5 p-5 pb-3">
          <CardTitle className="text-sm font-medium text-tp-ink">
            {t("system.version.current")}
          </CardTitle>
          <CardDescription className="text-xs text-tp-ink-3">
            {lastChecked
              ? `${t("system.version.lastChecked")} · ${lastChecked}`
              : t("system.version.lastChecked")}
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4 p-5 pt-0 sm:grid-cols-2">
          <VersionPill
            label={t("system.version.current")}
            value={info?.current ?? "—"}
            testId="system-version-current"
          />
          <VersionPill
            label={t("system.version.latest")}
            value={info?.latest ?? "—"}
            testId="system-version-latest"
            highlighted={Boolean(info?.available)}
          />
        </CardContent>
      </Card>

      {/* Update banner OR "up to date" */}
      {info ? (
        info.available && info.latest ? (
          <Card
            data-testid="system-update-banner"
            className="border-tp-amber/40 bg-tp-amber-soft"
          >
            <CardHeader className="space-y-1.5 p-5 pb-3">
              <CardTitle className="flex flex-wrap items-baseline gap-x-2 text-base font-semibold text-tp-ink">
                <span>{t("system.update.available")}</span>
                <span className="font-mono text-sm text-tp-amber">
                  {info.latest}
                </span>
              </CardTitle>
              {published ? (
                <CardDescription className="text-xs text-tp-ink-3">
                  {t("system.update.published", { relative: published })}
                </CardDescription>
              ) : null}
            </CardHeader>
            <CardContent className="space-y-4 p-5 pt-0">
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={handleDismiss}
                  aria-label={t("system.update.dismiss")}
                  data-testid="system-dismiss-update"
                >
                  {t("system.update.dismiss")}
                </Button>
                {info.release_url ? (
                  <Button
                    asChild
                    variant="outline"
                    size="sm"
                    className="gap-1.5"
                  >
                    <a
                      href={info.release_url}
                      target="_blank"
                      rel="noreferrer noopener"
                      data-testid="system-changelog-link"
                    >
                      <ExternalLink className="h-3.5 w-3.5" aria-hidden />
                      <span>{t("system.update.fullChangelog")}</span>
                    </a>
                  </Button>
                ) : null}
              </div>
              {info.release_notes_md ? (
                <div className="space-y-2">
                  <h2 className="text-xs font-semibold uppercase tracking-wide text-tp-ink-3">
                    {t("system.update.releaseNotes")}
                  </h2>
                  <div className="rounded-md border border-tp-glass-edge bg-tp-glass-inner p-4">
                    <ReleaseNotes markdown={info.release_notes_md} />
                  </div>
                </div>
              ) : null}
            </CardContent>
          </Card>
        ) : (
          <Card
            data-testid="system-up-to-date"
            className="border-tp-glass-edge"
          >
            <CardContent className="flex items-center gap-2 p-5 text-sm text-tp-ink-2">
              <Server className="h-4 w-4 text-tp-amber" aria-hidden />
              <span>{t("system.version.upToDate")}</span>
            </CardContent>
          </Card>
        )
      ) : null}

      {/* Upgrade commands card */}
      <UpgradeCommandsCard commands={commands} />
    </div>
  );
}

interface VersionPillProps {
  label: string;
  value: string;
  testId: string;
  highlighted?: boolean;
}

function VersionPill({ label, value, testId, highlighted }: VersionPillProps) {
  return (
    <div className="space-y-1">
      <div className="text-xs font-medium uppercase tracking-wide text-tp-ink-3">
        {label}
      </div>
      <div
        data-testid={testId}
        className={cn(
          "inline-flex items-center rounded-full border px-3 py-1 font-mono text-sm",
          highlighted
            ? "border-tp-amber/50 bg-tp-amber-soft text-tp-amber"
            : "border-tp-glass-edge bg-tp-glass-inner text-tp-ink",
        )}
      >
        {value}
      </div>
    </div>
  );
}

interface UpgradeCommandsCardProps {
  commands: UpgradeCommands | undefined;
}

function UpgradeCommandsCard({ commands }: UpgradeCommandsCardProps) {
  const { t } = useTranslation();
  const [active, setActive] = React.useState<UpgradeTabKey>("native");

  return (
    <Card data-testid="system-upgrade-card">
      <CardHeader className="space-y-1.5 p-5 pb-3">
        <CardTitle className="text-sm font-medium text-tp-ink">
          {t("system.upgrade.title")}
        </CardTitle>
        <CardDescription className="text-xs text-tp-ink-3">
          {t("system.upgrade.subtitle")}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 p-5 pt-0">
        <div
          role="tablist"
          aria-label={t("system.upgrade.title")}
          data-testid="system-upgrade-tabs"
          className="inline-flex gap-1 rounded-full border border-tp-glass-edge bg-tp-glass-inner p-1"
        >
          {UPGRADE_TABS.map((tab) => {
            const selected = active === tab.key;
            return (
              <button
                key={tab.key}
                role="tab"
                type="button"
                aria-selected={selected}
                aria-controls={`upgrade-panel-${tab.key}`}
                id={`upgrade-tab-${tab.key}`}
                data-testid={`system-upgrade-tab-${tab.key}`}
                onClick={() => setActive(tab.key)}
                className={cn(
                  "rounded-full px-3 py-1 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
                  selected
                    ? "bg-tp-amber-soft text-tp-amber"
                    : "text-tp-ink-3 hover:text-tp-ink",
                )}
              >
                {t(tab.labelKey)}
              </button>
            );
          })}
        </div>

        <div
          role="tabpanel"
          id={`upgrade-panel-${active}`}
          aria-labelledby={`upgrade-tab-${active}`}
          data-testid={`system-upgrade-panel-${active}`}
        >
          {commands ? (
            <CopyUpgradeCommand
              label={t(
                UPGRADE_TABS.find((tab) => tab.key === active)?.labelKey ??
                  "system.upgrade.tabNative",
              )}
              command={commands[active]}
            />
          ) : (
            <div className="h-24 animate-pulse rounded-md border border-tp-glass-edge bg-tp-glass-inner" />
          )}
        </div>

        <div className="flex flex-col gap-2 border-t border-tp-glass-edge pt-3 text-xs text-tp-ink-3 sm:flex-row sm:items-center sm:justify-between">
          <span>{t("system.upgrade.note")}</span>
          <Link
            href="/dev-settings"
            className="font-medium text-tp-amber underline-offset-2 hover:underline"
            data-testid="system-runbook-link"
          >
            {t("system.upgrade.runbookLink")}
          </Link>
        </div>
      </CardContent>
    </Card>
  );
}
