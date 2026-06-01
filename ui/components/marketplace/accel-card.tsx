"use client";

/**
 * `<AccelCard>` — read-only GitHub-acceleration settings + a probe button.
 *
 * Shows the current mode/preset/enabled state, the registry repo, and the
 * accelerated index URL. The "Test acceleration" button POSTs
 * `/admin/marketplace/accel/test` and renders the two `ProbeLeg` results
 * (direct vs accelerated) with ok/status/ms. All values are read-only — a
 * hint points operators at the Config TOML ([marketplace.github_proxy]) for
 * edits.
 */

import * as React from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { CheckCircle2, Loader2, XCircle, Zap } from "lucide-react";
import { toast } from "sonner";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";
import { Button } from "@/components/ui/button";
import {
  CorlinmanApiError,
  getMarketplaceSettings,
  testMarketplaceAccel,
  type MarketplaceSettings,
  type ProbeLeg,
} from "@/lib/api";

export function AccelCard(): React.JSX.Element {
  const { t } = useTranslation();

  const query = useQuery<MarketplaceSettings>({
    queryKey: ["marketplace-settings"],
    queryFn: () => getMarketplaceSettings(),
    retry: false,
  });

  const test = useMutation({
    mutationFn: () => testMarketplaceAccel(),
    onError: (err) => {
      const msg =
        err instanceof CorlinmanApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : String(err);
      toast.error(t("marketplace.accel.testFailed", { message: msg }));
    },
  });

  if (query.isPending) {
    return (
      <GlassPanel variant="soft" className="flex flex-col gap-4 p-6">
        <div className="h-4 w-40 rounded bg-tp-glass-inner-strong" />
        <div className="h-3 w-2/3 rounded bg-tp-glass-inner" />
        <div className="h-3 w-1/2 rounded bg-tp-glass-inner" />
      </GlassPanel>
    );
  }

  if (query.isError || !query.data) {
    return (
      <GlassPanel
        variant="soft"
        className="flex flex-col items-center gap-2 p-8 text-center"
        data-testid="accel-card-offline"
      >
        <div className="font-mono text-[11px] uppercase tracking-[0.12em] text-tp-err">
          {t("marketplace.accel.offlineTitle")}
        </div>
        <p className="max-w-prose text-[13px] text-tp-ink-2">
          {t("marketplace.accel.offlineHint")}
        </p>
      </GlassPanel>
    );
  }

  const s = query.data;
  const accel = s.accel;

  return (
    <GlassPanel
      variant="soft"
      className="flex flex-col gap-5 p-6"
      data-testid="accel-card"
    >
      {/* Title */}
      <div className="flex items-center gap-2.5">
        <span className="inline-flex h-9 w-9 items-center justify-center rounded-full border border-tp-amber/25 bg-tp-amber-soft text-tp-amber">
          <Zap className="h-4 w-4" aria-hidden />
        </span>
        <div className="min-w-0">
          <h2 className="text-[16px] font-medium leading-tight text-tp-ink">
            {t("marketplace.accel.heroTitle")}
          </h2>
          <p className="text-[12.5px] text-tp-ink-3">
            {t("marketplace.accel.subtitle")}
          </p>
        </div>
      </div>

      {/* Settings grid */}
      <dl className="grid grid-cols-1 gap-x-6 gap-y-3 sm:grid-cols-2">
        <Field label={t("marketplace.accel.mode")}>
          <span
            data-testid="accel-mode"
            className="inline-flex items-center rounded-full border border-tp-glass-edge bg-tp-glass-inner px-2 py-[2px] font-mono text-[11px] text-tp-ink-2"
          >
            {accel.mode}
          </span>
        </Field>
        <Field label={t("marketplace.accel.preset")}>
          <span
            data-testid="accel-preset"
            className="inline-flex items-center rounded-full border border-tp-glass-edge bg-tp-glass-inner px-2 py-[2px] font-mono text-[11px] text-tp-ink-2"
          >
            {accel.preset}
          </span>
        </Field>
        <Field label={t("marketplace.accel.enabled")}>
          <span
            data-testid="accel-enabled"
            className={cn(
              "inline-flex items-center gap-1.5 rounded-full border px-2 py-[2px] font-mono text-[11px]",
              accel.enabled
                ? "border-tp-ok/30 bg-tp-ok-soft text-tp-ok"
                : "border-tp-ink-3/30 bg-tp-glass-inner-strong text-tp-ink-2",
            )}
          >
            <span
              aria-hidden
              className={cn(
                "h-[5px] w-[5px] rounded-full",
                accel.enabled ? "bg-tp-ok" : "bg-tp-ink-3",
              )}
            />
            {accel.enabled
              ? t("marketplace.accel.enabled")
              : t("marketplace.accel.disabled")}
          </span>
        </Field>
        <Field label={t("marketplace.accel.githubToken")}>
          <span className="font-mono text-[12px] text-tp-ink-2">
            {s.github_token_set
              ? t("marketplace.accel.githubTokenSet")
              : t("marketplace.accel.githubTokenUnset")}
          </span>
        </Field>
        <Field label={t("marketplace.accel.registryRepo")}>
          <code className="break-all font-mono text-[12px] text-tp-ink-2">
            {s.registry_repo}
          </code>
        </Field>
        <Field label={t("marketplace.accel.registryRef")}>
          <code className="break-all font-mono text-[12px] text-tp-ink-2">
            {s.registry_ref}
          </code>
        </Field>
        <Field label={t("marketplace.accel.indexUrl")} full>
          <code className="break-all font-mono text-[12px] text-tp-ink-3">
            {s.index_url}
          </code>
        </Field>
        <Field label={t("marketplace.accel.acceleratedIndexUrl")} full>
          <code
            data-testid="accel-index-url"
            className="break-all font-mono text-[12px] text-tp-ink-2"
          >
            {s.accelerated_index_url}
          </code>
        </Field>
      </dl>

      {/* Read-only hint */}
      <p className="rounded-md border border-tp-glass-edge bg-tp-glass-inner px-3 py-2 text-[12px] text-tp-ink-3">
        {t("marketplace.accel.readOnlyHint")}
      </p>

      {/* Test button */}
      <div className="flex items-center gap-3">
        <Button
          size="sm"
          variant="outline"
          disabled={test.isPending}
          onClick={() => test.mutate()}
          data-testid="accel-test-button"
        >
          {test.isPending ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
          ) : (
            <Zap className="h-3.5 w-3.5" aria-hidden />
          )}
          {test.isPending
            ? t("marketplace.accel.testing")
            : t("marketplace.accel.test")}
        </Button>
      </div>

      {/* Probe results */}
      {test.data ? (
        <div
          className="grid grid-cols-1 gap-3 sm:grid-cols-2"
          data-testid="accel-test-results"
        >
          <ProbeCard
            title={t("marketplace.accel.testDirect")}
            leg={test.data.direct}
            testId="accel-probe-direct"
          />
          <ProbeCard
            title={t("marketplace.accel.testAccelerated")}
            leg={test.data.accelerated}
            testId="accel-probe-accelerated"
          />
        </div>
      ) : null}
    </GlassPanel>
  );
}

function Field({
  label,
  children,
  full = false,
}: {
  label: string;
  children: React.ReactNode;
  full?: boolean;
}) {
  return (
    <div className={cn("flex flex-col gap-1", full && "sm:col-span-2")}>
      <dt className="font-mono text-[10px] uppercase tracking-[0.12em] text-tp-ink-4">
        {label}
      </dt>
      <dd>{children}</dd>
    </div>
  );
}

function ProbeCard({
  title,
  leg,
  testId,
}: {
  title: string;
  leg: ProbeLeg;
  testId: string;
}) {
  const { t } = useTranslation();
  return (
    <div
      data-testid={testId}
      data-ok={leg.ok}
      className={cn(
        "flex flex-col gap-1.5 rounded-lg border p-3",
        leg.ok
          ? "border-emerald-500/40 bg-emerald-500/5"
          : "border-red-500/40 bg-red-500/5",
      )}
    >
      <div className="flex items-center gap-2">
        {leg.ok ? (
          <CheckCircle2 className="h-4 w-4 text-emerald-600" aria-hidden />
        ) : (
          <XCircle className="h-4 w-4 text-red-600" aria-hidden />
        )}
        <span className="text-[13px] font-medium text-tp-ink">{title}</span>
        <span
          className={cn(
            "ml-auto font-mono text-[11px]",
            leg.ok ? "text-emerald-700" : "text-red-600",
          )}
        >
          {leg.ok ? t("marketplace.accel.probeOk") : t("marketplace.accel.probeFail")}
        </span>
      </div>
      <code className="break-all font-mono text-[11px] text-tp-ink-3">
        {leg.url}
      </code>
      <div className="flex flex-wrap items-center gap-2 font-mono text-[10.5px] text-tp-ink-4">
        <span>
          {leg.status !== null
            ? t("marketplace.accel.probeStatus", { status: leg.status })
            : t("marketplace.accel.probeNoStatus")}
        </span>
        {leg.ms !== null ? (
          <>
            <span aria-hidden>·</span>
            <span>{t("marketplace.accel.probeMs", { ms: leg.ms })}</span>
          </>
        ) : null}
      </div>
      {leg.error ? (
        <p className="break-words text-[11px] text-red-600">{leg.error}</p>
      ) : null}
    </div>
  );
}

export default AccelCard;
