"use client";

/**
 * `/system` — version + update + upgrade-commands surface (W2.1).
 * (Page URL is `/system`: the `(admin)` route group adds no URL segment.
 * `/admin/system/*` is the backend API namespace, not a page route.)
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
import { ChevronRight, ExternalLink, RefreshCcw, Server } from "lucide-react";
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
import { AuditCard } from "@/components/system/audit-card";
import { UpgradeConfirmModal } from "@/components/system/upgrade-confirm-modal";
import { UpgradeProgress } from "@/components/system/upgrade-progress";
import { useSearchParams, useRouter } from "next/navigation";

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
  const searchParams = useSearchParams();
  const router = useRouter();

  // Deep-link: ?upgrade=<request_id> drives <UpgradeProgress> directly,
  // typically set by <UpdateBubble>'s "Upgrade now" handler after the
  // confirm modal POSTs. A local-state copy lets us clear the search
  // param after terminal without re-mounting.
  const deepLinkUpgradeId = searchParams?.get("upgrade") ?? null;
  const [activeUpgradeId, setActiveUpgradeId] = React.useState<string | null>(
    deepLinkUpgradeId,
  );
  React.useEffect(() => {
    setActiveUpgradeId(deepLinkUpgradeId);
  }, [deepLinkUpgradeId]);

  const [confirmOpen, setConfirmOpen] = React.useState(false);

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
      {/* Header — W3: uses `pageTitle`/`pageSubtitle` so the page reads as
          "更新管理 / Update management" (a clear version-update surface),
          not generic "System". The sidebar entry is now "更新 / Updates"
          (see `sidebar.tsx`), so the page title should match. */}
      <header className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight text-sg-ink">
            {t("system.pageTitle")}
          </h1>
          <p className="max-w-2xl text-sm text-sg-ink-3">
            {t("system.pageSubtitle")}
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
          <CardTitle className="text-sm font-medium text-sg-ink">
            {t("system.version.current")}
          </CardTitle>
          <CardDescription className="text-xs text-sg-ink-3">
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
            className="border-sg-accent/40 bg-sg-accent-soft"
          >
            <CardHeader className="space-y-1.5 p-5 pb-3">
              <CardTitle className="flex flex-wrap items-baseline gap-x-2 text-base font-semibold text-sg-ink">
                <span>{t("system.update.available")}</span>
                <span className="font-mono text-sm text-sg-accent">
                  {info.latest}
                </span>
              </CardTitle>
              {published ? (
                <CardDescription className="text-xs text-sg-ink-3">
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
                  <h2 className="text-xs font-semibold uppercase tracking-wide text-sg-ink-3">
                    {t("system.update.releaseNotes")}
                  </h2>
                  <div className="rounded-md border border-sg-border bg-sg-inset p-4">
                    <ReleaseNotes markdown={info.release_notes_md} />
                  </div>
                </div>
              ) : null}
            </CardContent>
          </Card>
        ) : (
          <Card
            data-testid="system-up-to-date"
            className="border-sg-border"
          >
            <CardContent className="flex items-center gap-2 p-5 text-sm text-sg-ink-2">
              <Server className="h-4 w-4 text-sg-accent" aria-hidden />
              <span>{t("system.version.upToDate")}</span>
            </CardContent>
          </Card>
        )
      ) : null}

      {/* Active upgrade — driven by ?upgrade=<id> deep-link or by the
          primary Upgrade button below. */}
      {activeUpgradeId ? (
        <UpgradeProgress
          requestId={activeUpgradeId}
          currentVersion={info?.current ?? null}
          onTerminal={() => {
            // Stop driving the URL once the upgrade lands; let the user
            // come back to a clean page if they refresh post-reload.
            if (deepLinkUpgradeId) {
              router.replace("/system");
            }
          }}
        />
      ) : null}

      {/* Primary upgrade CTA — only when an update is available AND no
          upgrade is currently in flight on this page. */}
      {info?.available && info.latest && !activeUpgradeId ? (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-sg-accent/40 bg-sg-accent/5 p-4">
          <div className="space-y-1">
            <h2 className="text-base font-semibold tracking-tight">
              {t("system.upgrade.button", { tag: info.latest })}
            </h2>
            <p className="text-xs text-sg-ink-3">
              {t("system.upgrade.note")}
            </p>
          </div>
          <Button
            type="button"
            data-testid="system-upgrade-button"
            onClick={() => setConfirmOpen(true)}
          >
            {t("system.upgrade.button", { tag: info.latest })}
          </Button>
        </div>
      ) : null}

      {/* Manual upgrade — copy these commands. Collapsed accordion when
          a one-click path is available; expanded fallback when not. */}
      <details
        className="rounded-lg border border-sg-border bg-sg-card p-4 sm:p-6 [&[open]>summary>svg]:rotate-90"
        open={!info?.available}
      >
        <summary className="flex cursor-pointer items-center gap-2 list-none">
          <ChevronRight
            className="h-4 w-4 transition-transform"
            aria-hidden
          />
          <div className="space-y-0.5">
            <h2 className="text-base font-semibold tracking-tight">
              {t("system.upgrade.manual.title")}
            </h2>
            <p className="text-xs text-sg-ink-3">
              {t("system.upgrade.manual.subtitle")}
            </p>
          </div>
        </summary>
        <div className="mt-4">
          <UpgradeCommandsCard commands={commands} />
        </div>
      </details>

      {/* Audit log — past upgrade events. Self-contained. */}
      <AuditCard />

      {/* Typed-confirmation modal — wired to the primary upgrade button.
          On 202 we redirect to ?upgrade=<id> so a refresh lands back on
          the in-flight upgrade. */}
      {info?.latest ? (
        <UpgradeConfirmModal
          open={confirmOpen}
          onOpenChange={setConfirmOpen}
          tag={info.latest}
          currentVersion={info.current}
          releaseNotesExcerpt={
            info.release_notes_md?.split("\n").slice(0, 2).join(" ") ?? null
          }
          onUpgradeStarted={(res) => {
            setActiveUpgradeId(res.request_id);
            // NOTE: the page route is /system — the (admin) route group does
            // NOT contribute a URL segment; /admin/* is the backend API
            // namespace only. Navigating there 404s.
            router.replace(
              `/system?upgrade=${encodeURIComponent(res.request_id)}`,
            );
          }}
        />
      ) : null}
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
      <div className="text-xs font-medium uppercase tracking-wide text-sg-ink-3">
        {label}
      </div>
      <div
        data-testid={testId}
        className={cn(
          "inline-flex items-center rounded-full border px-3 py-1 font-mono text-sm",
          highlighted
            ? "border-sg-accent/50 bg-sg-accent-soft text-sg-accent"
            : "border-sg-border bg-sg-inset text-sg-ink",
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
        <CardTitle className="text-sm font-medium text-sg-ink">
          {t("system.upgrade.title")}
        </CardTitle>
        <CardDescription className="text-xs text-sg-ink-3">
          {t("system.upgrade.subtitle")}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 p-5 pt-0">
        <div
          role="tablist"
          aria-label={t("system.upgrade.title")}
          data-testid="system-upgrade-tabs"
          className="inline-flex gap-1 rounded-full border border-sg-border bg-sg-inset p-1"
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
                  "rounded-full px-3 py-1 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40",
                  selected
                    ? "bg-sg-accent-soft text-sg-accent"
                    : "text-sg-ink-3 hover:text-sg-ink",
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
            <div className="h-24 animate-pulse rounded-md border border-sg-border bg-sg-inset" />
          )}
        </div>

        <div className="flex flex-col gap-2 border-t border-sg-border pt-3 text-xs text-sg-ink-3 sm:flex-row sm:items-center sm:justify-between">
          <span>{t("system.upgrade.note")}</span>
          <Link
            href="/dev-settings"
            className="font-medium text-sg-accent underline-offset-2 hover:underline"
            data-testid="system-runbook-link"
          >
            {t("system.upgrade.runbookLink")}
          </Link>
        </div>
      </CardContent>
    </Card>
  );
}
