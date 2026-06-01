"use client";

import * as React from "react";
import Link from "next/link";
import { useTranslation } from "react-i18next";
import { motion } from "framer-motion";
import { ChevronLeft } from "lucide-react";

import { useMotionVariants } from "@/lib/motion";
import { GlassPanel } from "@/components/ui/glass-panel";
import { AccelCard } from "@/components/marketplace/accel-card";

/**
 * Acceleration settings — read-only GitHub-acceleration card for the
 * Marketplace. Sits on its own sub-route so it's directly linkable from the
 * Marketplace hero and the sidebar nav. Editing is done via the Config TOML
 * editor under [marketplace.github_proxy]; this page only displays + probes.
 */
export default function MarketplaceAccelerationPage() {
  const { t } = useTranslation();
  const variants = useMotionVariants();

  return (
    <motion.div
      className="flex flex-col gap-4"
      variants={variants.fadeUp}
      initial="hidden"
      animate="visible"
    >
      <GlassPanel
        variant="strong"
        as="section"
        className="relative overflow-hidden p-7"
      >
        <div
          aria-hidden
          className="pointer-events-none absolute bottom-[-90px] right-[-40px] h-[240px] w-[360px] rounded-full opacity-60 blur-3xl"
          style={{
            background:
              "radial-gradient(closest-side, var(--tp-amber-glow), transparent 70%)",
          }}
        />
        <div className="relative flex min-w-0 flex-col gap-3">
          <Link
            href="/marketplace"
            className="inline-flex w-fit items-center gap-1 font-mono text-[11px] text-tp-ink-3 transition-colors hover:text-tp-ink"
            data-testid="accel-back-link"
          >
            <ChevronLeft className="h-3.5 w-3.5" aria-hidden />
            {t("marketplace.title")}
          </Link>
          <h1 className="text-balance font-sans text-[28px] font-semibold leading-[1.15] tracking-[-0.025em] text-tp-ink sm:text-[32px]">
            {t("marketplace.accel.title")}
          </h1>
          <p className="max-w-[72ch] text-[14.5px] leading-[1.6] text-tp-ink-2">
            {t("marketplace.accel.subtitle")}
          </p>
        </div>
      </GlassPanel>

      <AccelCard />
    </motion.div>
  );
}
