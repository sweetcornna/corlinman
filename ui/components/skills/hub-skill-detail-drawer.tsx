"use client";

/**
 * `<HubSkillDetailDrawer>` — modal drawer with full hub-skill detail (W2.2).
 *
 * Opens when a `<HubSkillCard>` is clicked. Fetches `getHubSkill(slug)` on
 * mount and renders:
 *   - emoji + name + version + homepage link (if any)
 *   - description, scan_summary chip, versions list
 *   - readme excerpt (plain-text rendered; the project has no markdown
 *     renderer yet, mirroring the local `<SkillDrawer>` body section)
 * Footer carries the Install button, which opens `<InstallProgressModal>`.
 */

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Download, ExternalLink, Loader2, Star } from "@/components/icons";

import { Drawer } from "@/components/ui/drawer";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/alert";
import { cn } from "@/lib/utils";
import { getHubSkill, type HubSkillSummary } from "@/lib/api";
import { InstallProgressModal } from "./install-progress-modal";

export interface HubSkillDetailDrawerProps {
  /** Card row that opened the drawer. `null` → closed. */
  summary: HubSkillSummary | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function HubSkillDetailDrawer({
  summary,
  open,
  onOpenChange,
}: HubSkillDetailDrawerProps): React.JSX.Element {
  const { t } = useTranslation();
  const slug = summary?.slug ?? null;

  // Detail fetch — disabled until we have a slug; query is keyed off the
  // slug so opening a different card always refetches.
  const query = useQuery({
    queryKey: ["hub-skill", slug],
    queryFn: () => getHubSkill(slug as string),
    enabled: open && slug !== null,
    retry: false,
  });

  const [installOpen, setInstallOpen] = React.useState(false);

  // Reset the install modal whenever the drawer is dismissed.
  React.useEffect(() => {
    if (!open) setInstallOpen(false);
  }, [open]);

  const detail = query.data;
  const headerName = detail?.name ?? summary?.name ?? "";
  const headerEmoji = detail?.emoji ?? summary?.emoji ?? "✦";
  const version = detail?.latest_version ?? summary?.latest_version ?? "";

  return (
    <>
      <Drawer
        open={open}
        onOpenChange={onOpenChange}
        width="lg"
        title={headerName}
        description={summary?.description}
        footer={
          <div className="flex w-full items-center justify-end gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => onOpenChange(false)}
              data-testid="hub-detail-close"
            >
              {t("skills.hub.detail.close")}
            </Button>
            <Button
              size="sm"
              disabled={!detail}
              onClick={() => setInstallOpen(true)}
              data-testid="hub-detail-install"
            >
              {t("skills.hub.detail.install")}
            </Button>
          </div>
        }
      >
        {summary ? (
          <div
            className="flex flex-col gap-5 px-5 py-5 text-sm"
            data-testid="hub-detail-body"
          >
            {/* Header row */}
            <div className="flex flex-wrap items-center gap-3">
              <div
                className={cn(
                  "flex h-11 w-11 shrink-0 items-center justify-center rounded-full",
                  "border border-sg-accent/25 bg-sg-accent-soft text-[20px] leading-none",
                )}
                aria-hidden
              >
                <span className="opacity-85">{headerEmoji}</span>
              </div>
              <div className="min-w-0 flex-1">
                <h2 className="truncate text-[18px] font-medium leading-tight tracking-[-0.01em] text-sg-ink">
                  {headerName}
                </h2>
                <div className="mt-0.5 flex flex-wrap items-center gap-2 font-mono text-[10.5px] text-sg-ink-4">
                  <span>v{version}</span>
                  <span aria-hidden>·</span>
                  <span className="inline-flex items-center gap-1">
                    <Star className="h-3 w-3" aria-hidden />
                    {summary.stars}
                  </span>
                  <span aria-hidden>·</span>
                  <span className="inline-flex items-center gap-1">
                    <Download className="h-3 w-3" aria-hidden />
                    {summary.downloads}
                  </span>
                </div>
              </div>
              {detail?.homepage ? (
                <a
                  href={detail.homepage}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 rounded-full border border-sg-border px-2.5 py-[3px] font-mono text-[10.5px] text-sg-ink-3 hover:bg-sg-inset-hover"
                  data-testid="hub-detail-homepage"
                >
                  <ExternalLink className="h-3 w-3" aria-hidden />
                  {t("skills.hub.detail.homepage")}
                </a>
              ) : null}
            </div>

            {/* Description */}
            <p className="text-[14px] leading-[1.6] text-sg-ink-2">
              {detail?.description ?? summary.description}
            </p>

            {/* Loading state */}
            {query.isPending ? (
              <div className="flex items-center gap-2 text-[12.5px] text-sg-ink-3">
                <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
                {t("skills.hub.detail.loading")}
              </div>
            ) : null}

            {/* Error state */}
            {query.isError ? (
              <Alert variant="danger" data-testid="hub-detail-error">
                {(query.error as Error | undefined)?.message ??
                  t("skills.hub.detail.errorUnknown")}
              </Alert>
            ) : null}

            {/* Scan summary chip */}
            {detail?.scan_summary ? (
              <Section title={t("skills.hub.detail.scanTitle")}>
                <span
                  data-testid="hub-detail-scan"
                  className={cn(
                    "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-[3px] font-mono text-[10.5px]",
                    detail.scan_summary === "pass" &&
                      "border-sg-ok/30 bg-sg-ok-soft text-sg-ok",
                    detail.scan_summary === "warn" &&
                      "border-sg-warn/30 bg-sg-warn-soft text-sg-warn",
                    detail.scan_summary === "fail" &&
                      "border-sg-err/40 bg-sg-err-soft text-sg-err",
                  )}
                >
                  {t(`skills.hub.detail.scan.${detail.scan_summary}`)}
                </span>
              </Section>
            ) : null}

            {/* Versions list */}
            {detail?.versions && detail.versions.length > 0 ? (
              <Section
                title={`${t("skills.hub.detail.versionsTitle")} (${detail.versions.length})`}
              >
                <ul
                  className="flex flex-wrap gap-1.5"
                  data-testid="hub-detail-versions"
                >
                  {detail.versions.map((v) => (
                    <li
                      key={v}
                      className={cn(
                        "inline-flex items-center rounded-md border border-sg-border bg-sg-inset px-2 py-[3px] font-mono text-[11px]",
                        v === detail.latest_version
                          ? "text-sg-accent"
                          : "text-sg-ink-3",
                      )}
                    >
                      v{v}
                    </li>
                  ))}
                </ul>
              </Section>
            ) : null}

            {/* README excerpt */}
            {detail?.readme_excerpt ? (
              <Section title={t("skills.hub.detail.readmeTitle")}>
                <p
                  className="whitespace-pre-wrap text-[13px] leading-[1.6] text-sg-ink-2"
                  data-testid="hub-detail-readme"
                >
                  {detail.readme_excerpt}
                </p>
              </Section>
            ) : null}
          </div>
        ) : null}
      </Drawer>

      {summary ? (
        <InstallProgressModal
          open={installOpen}
          onOpenChange={(next) => {
            setInstallOpen(next);
            if (!next) onOpenChange(false);
          }}
          slug={summary.slug}
          name={summary.name}
        />
      ) : null}
    </>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="space-y-2">
      <h4 className="font-mono text-[10px] uppercase tracking-[0.12em] text-sg-ink-4">
        {title}
      </h4>
      {children}
    </section>
  );
}

export default HubSkillDetailDrawer;
