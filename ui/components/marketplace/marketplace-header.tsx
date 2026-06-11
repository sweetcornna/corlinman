"use client";

import * as React from "react";
import Link from "next/link";
import { useTranslation } from "react-i18next";
import { Search, Zap, GitPullRequest } from "lucide-react";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";
import { useCommandPalette } from "@/components/cmdk-palette";

/**
 * `<MarketplaceHeader>` — hero-strip for the unified Marketplace surface.
 *
 * Mirrors `<SkillsHeader>` (lead pill · title · prose · ⌘K CTA) so the
 * Marketplace reads as a sibling of the Skills gallery. Adds a quick link
 * to the read-only Acceleration settings card.
 */
export interface MarketplaceHeaderProps {
  /** true when the underlying market query errored / hasn't loaded. */
  offline?: boolean;
}

export function MarketplaceHeader({ offline = false }: MarketplaceHeaderProps) {
  const { t } = useTranslation();
  const palette = useCommandPalette();

  return (
    <GlassPanel
      variant="strong"
      as="section"
      lively
      className="relative overflow-hidden p-7"
    >
      <div
        aria-hidden
        className="pointer-events-none absolute bottom-[-90px] right-[-40px] h-[240px] w-[360px] rounded-full opacity-60 blur-3xl"
        style={{
          background:
            "radial-gradient(closest-side, var(--sg-accent-glow), transparent 70%)",
        }}
      />
      <div
        aria-hidden
        className="pointer-events-none absolute top-[-60px] left-[-40px] h-[180px] w-[260px] rounded-full opacity-40 blur-[50px]"
        style={{
          background:
            "radial-gradient(closest-side, color-mix(in oklch, var(--sg-accent-2) 35%, transparent), transparent 70%)",
        }}
      />

      <div className="relative flex min-w-0 flex-col gap-4">
        <div className="inline-flex w-fit items-center gap-2.5 rounded-full border border-sg-border bg-sg-inset-strong py-1 pl-2 pr-3 font-mono text-[11px] text-sg-ink-2">
          <span
            className={cn(
              "h-1.5 w-1.5 rounded-full",
              offline ? "bg-sg-err" : "bg-sg-accent sg-breathe-accent",
            )}
          />
          {offline ? t("marketplace.common.offlineTitle") : t("marketplace.leadPill")}
        </div>

        <h1 className="text-balance font-sans text-[28px] font-semibold leading-[1.15] tracking-[-0.025em] text-sg-ink sm:text-[32px]">
          {t("marketplace.title")}
        </h1>

        <p className="max-w-[72ch] text-[14.5px] leading-[1.6] text-sg-ink-2">
          {t("marketplace.proseLead")} {t("marketplace.proseTail")}
        </p>

        <div className="mt-1 flex flex-wrap items-center gap-2.5">
          <button
            type="button"
            onClick={() => palette.setOpen(true)}
            className="inline-flex items-center gap-2 rounded-lg border border-sg-border bg-sg-inset px-3 py-2 text-[13px] font-medium text-sg-ink-2 transition-colors hover:bg-sg-inset-hover hover:text-sg-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40"
          >
            <Search className="h-3.5 w-3.5" aria-hidden />
            {t("marketplace.ctaPaletteHint")}
            <span className="ml-1 rounded bg-black/5 px-1.5 py-0.5 font-mono text-[10px] text-sg-ink-3 dark:bg-white/5">
              ⌘K
            </span>
          </button>
          <Link
            href="/marketplace/acceleration"
            className="inline-flex items-center gap-2 rounded-lg border border-sg-border bg-sg-inset px-3 py-2 text-[13px] font-medium text-sg-ink-2 transition-colors hover:bg-sg-inset-hover hover:text-sg-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40"
            data-testid="marketplace-accel-link"
          >
            <Zap className="h-3.5 w-3.5" aria-hidden />
            {t("marketplace.accelLink")}
          </Link>
          <Link
            href="/marketplace/contribute"
            className="inline-flex items-center gap-2 rounded-lg border border-sg-border bg-sg-inset px-3 py-2 text-[13px] font-medium text-sg-ink-2 transition-colors hover:bg-sg-inset-hover hover:text-sg-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40"
            data-testid="marketplace-contribute-link"
          >
            <GitPullRequest className="h-3.5 w-3.5" aria-hidden />
            {t("marketplace.contributeLink")}
          </Link>
        </div>
      </div>
    </GlassPanel>
  );
}

export default MarketplaceHeader;
