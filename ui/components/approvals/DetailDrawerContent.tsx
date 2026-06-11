"use client";

import { useTranslation } from "react-i18next";
import { Inbox } from "lucide-react";
import { DetailDrawer } from "@/components/ui/detail-drawer";
import { GlassPanel } from "@/components/ui/glass-panel";
import { JsonView } from "@/components/ui/json-view";
import { cn } from "@/lib/utils";
import type { Approval } from "./types";

/**
 * Right-side drawer content for a selected approval row.
 *
 * Composed over the shared `<DetailDrawer>` primitive. When nothing is
 * selected, renders a muted placeholder inside a soft glass panel so the
 * column doesn't collapse and the grid keeps its shape.
 */
export interface DetailDrawerContentProps {
  approval: Approval | null;
}

export function DetailDrawerContent({ approval }: DetailDrawerContentProps) {
  const { t } = useTranslation();
  if (!approval) {
    return (
      <GlassPanel
        as="aside"
        variant="subtle"
        className={cn(
          "flex flex-col items-center justify-center gap-2.5",
          "min-h-[320px] p-8 text-center",
        )}
      >
        <Inbox className="h-7 w-7 text-sg-ink-4" aria-hidden />
        <p className="text-[13.5px] font-medium text-sg-ink-2">
          {t("approvals.tp.drawerSelect")}
        </p>
        <p className="max-w-[28ch] text-[12px] text-sg-ink-3">
          {t("approvals.tp.drawerSelectHint")}
        </p>
      </GlassPanel>
    );
  }

  const decisionLabel = approval.decision
    ? approval.decision === "approved"
      ? t("approvals.statusApproved")
      : approval.decision === "denied"
        ? t("approvals.statusDenied")
        : approval.decision
    : t("approvals.statusPending");

  const prettyArgs = toPretty(approval.args_json);

  return (
    <DetailDrawer
      title={
        <span className="font-mono">
          <span className="text-sg-accent">{approval.plugin}</span>
          <span className="text-sg-ink-4">.</span>
          {approval.tool}
        </span>
      }
      subsystem={t("approvals.tp.drawerSubsystem")}
      meta={
        <>
          <StatusPill decision={approval.decision} label={decisionLabel} />
          <span className="font-mono text-[11px] text-sg-ink-3">
            {formatTime(approval.requested_at)}
          </span>
        </>
      }
      className="min-h-[420px]"
    >
      <DetailDrawer.Section label={t("approvals.tp.drawerSectionArgs")}>
        <JsonView raw={prettyArgs} />
      </DetailDrawer.Section>
      <DetailDrawer.Section label={t("approvals.tp.drawerSectionRequest")}>
        <dl className="grid grid-cols-[120px_1fr] gap-x-3 gap-y-1.5 font-mono text-[12px]">
          <dt className="text-sg-ink-4">{t("approvals.tp.drawerSession")}</dt>
          <dd className="text-sg-ink-2">
            {approval.session_key || t("approvals.noneValue")}
          </dd>
          <dt className="text-sg-ink-4">{t("approvals.tp.drawerRequestedAt")}</dt>
          <dd className="text-sg-ink-2">{formatTime(approval.requested_at)}</dd>
          {approval.decided_at ? (
            <>
              <dt className="text-sg-ink-4">
                {t("approvals.tp.drawerDecidedAt")}
              </dt>
              <dd className="text-sg-ink-2">
                {formatTime(approval.decided_at)}
              </dd>
            </>
          ) : null}
          <dt className="text-sg-ink-4">{t("approvals.tp.drawerStatus")}</dt>
          <dd className="text-sg-ink-2">{decisionLabel}</dd>
        </dl>
      </DetailDrawer.Section>
      <DetailDrawer.Section label={t("approvals.tp.drawerSectionSafety")}>
        <p className="text-[12.5px] leading-[1.6] text-sg-ink-3">
          {t("approvals.tp.drawerSafetyReason")}
        </p>
      </DetailDrawer.Section>
    </DetailDrawer>
  );
}

// ─── pieces ──────────────────────────────────────────────────────────────

function StatusPill({
  decision,
  label,
}: {
  decision: string | null;
  label: string;
}) {
  const cls =
    decision === "approved"
      ? "border-sg-ok/35 bg-sg-ok-soft text-sg-ok"
      : decision === "denied"
        ? "border-sg-err/40 bg-sg-err-soft text-sg-err"
        : "border-sg-accent/30 bg-sg-accent-soft text-sg-accent";
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-[2px]",
        "font-mono text-[10px] uppercase tracking-[0.05em]",
        cls,
      )}
    >
      {label}
    </span>
  );
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function toPretty(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}
