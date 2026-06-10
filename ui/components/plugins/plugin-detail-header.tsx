"use client";

import * as React from "react";
import Link from "next/link";
import { motion } from "framer-motion";
import { useTranslation } from "react-i18next";
import { ArrowLeft } from "lucide-react";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";
import { useMotion } from "@/components/ui/motion-safe";
import type { PluginStatus } from "@/lib/api";

/**
 * `<PluginDetailHeader>` — strong-glass hero for the plugin detail route.
 *
 * Renders the back-link, plugin name, status pill, and manifest version.
 * Uses `motion.header` with a shared `layoutId` so transitioning from the
 * list card carries the hover-lift into place (skipped under
 * `prefers-reduced-motion: reduce`).
 */

const statusToTone: Record<PluginStatus, { dot: string; text: string; ring: string; label: string }> = {
  loaded: {
    dot: "bg-sg-ok",
    text: "text-sg-ok",
    ring: "border-sg-ok/30 bg-sg-ok-soft",
    label: "loaded",
  },
  disabled: {
    dot: "bg-sg-ink-4",
    text: "text-sg-ink-3",
    ring: "border-sg-border bg-sg-inset",
    label: "disabled",
  },
  error: {
    dot: "bg-sg-err",
    text: "text-sg-err",
    ring: "border-sg-err/30 bg-sg-err-soft",
    label: "error",
  },
};

export interface PluginDetailHeaderProps {
  name: string;
  version?: string;
  status?: PluginStatus;
  /** Subtitle prose — typically plugin description. */
  description?: string;
  /** Error banner string if the plugin failed to load. */
  errorMessage?: string;
}

export function PluginDetailHeader({
  name,
  version,
  status,
  description,
  errorMessage,
}: PluginDetailHeaderProps) {
  const { t } = useTranslation();
  const { reduced } = useMotion();
  const tone = status ? statusToTone[status] : undefined;

  return (
    <motion.div
      layoutId={reduced ? undefined : `plugin-card-${name}`}
      className="contents"
    >
      <GlassPanel variant="strong" as="header" className="relative overflow-hidden p-6">
        <div
          aria-hidden
          className="pointer-events-none absolute bottom-[-80px] right-[-40px] h-[200px] w-[320px] rounded-full opacity-55 blur-3xl"
          style={{
            background: "radial-gradient(closest-side, var(--sg-accent-glow), transparent 70%)",
          }}
        />
        <div className="relative flex flex-col gap-3">
          <Link
            href="/plugins"
            className="inline-flex w-fit items-center gap-1.5 font-mono text-[11px] text-sg-ink-3 transition-colors hover:text-sg-ink"
          >
            <ArrowLeft className="h-3 w-3" aria-hidden />
            {t("plugins.tp.detailBack")}
          </Link>

          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-balance font-sans text-[28px] font-semibold leading-[1.1] tracking-[-0.025em] text-sg-ink sm:text-[32px]">
              {name}
            </h1>

            {tone ? (
              <span
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-[3px] font-mono text-[11px] tracking-wide",
                  tone.ring,
                )}
              >
                <span className={cn("h-[5px] w-[5px] rounded-full", tone.dot)} aria-hidden />
                <span className={tone.text}>{tone.label}</span>
              </span>
            ) : null}

            {version ? (
              <span className="rounded-full border border-sg-border bg-sg-inset px-2.5 py-[3px] font-mono text-[11px] tracking-wide text-sg-ink-3">
                <span className="text-sg-ink-4">{t("plugins.tp.detailVersionLabel")}</span>{" "}
                {version}
              </span>
            ) : null}
          </div>

          {description ? (
            <p className="max-w-[72ch] text-[14px] leading-[1.6] text-sg-ink-2">{description}</p>
          ) : null}

          {errorMessage ? (
            <p className="rounded-lg border border-sg-err/30 bg-sg-err-soft px-3 py-2 font-mono text-[12px] text-sg-err">
              {errorMessage}
            </p>
          ) : null}
        </div>
      </GlassPanel>
    </motion.div>
  );
}

export default PluginDetailHeader;
