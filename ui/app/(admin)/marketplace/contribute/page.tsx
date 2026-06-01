"use client";

import * as React from "react";
import Link from "next/link";
import { useTranslation } from "react-i18next";
import { motion } from "framer-motion";
import {
  ChevronLeft,
  GitFork,
  FilePlus2,
  Hammer,
  GitPullRequest,
  ExternalLink,
  BookOpen,
  ShieldCheck,
} from "lucide-react";

import { useMotionVariants } from "@/lib/motion";
import { GlassPanel } from "@/components/ui/glass-panel";

/**
 * Marketplace · Contribute — a self-serve tutorial for publishing a custom
 * skill / MCP server / plugin to the registry via a pull request. Mirrors the
 * Acceleration sub-route layout (glass hero + back link). All prose is
 * i18n-driven (en + zh-CN); the code snippets are language-neutral.
 */

const REPO_URL = "https://github.com/sweetcornna/corlinman-marketplace";
const GUIDE_URL = `${REPO_URL}#contributing--submit-a-skill-mcp-server-or-plugin`;

const STEP_ICONS = [GitFork, FilePlus2, Hammer, GitPullRequest] as const;

const SKILL_SNIPPET = `skills/<slug>/SKILL.md
---
name: my-skill
description: what it does + when to use it
---
<your instructions in markdown>`;

const MCP_SNIPPET = `mcp/<slug>/manifest.json
{
  "name": "my-server",
  "transport": "stdio",
  "command": "npx",
  "args": ["-y", "@scope/my-mcp"],
  "requires": { "env": ["MY_API_KEY"] }
}`;

const PLUGIN_SNIPPET = `plugins/<slug>/
  plugin-manifest.toml   # hot-load manifest
  manifest.json          # index metadata
  main.py                # entry script`;

const PR_SNIPPET = `python scripts/build-registry.py
python scripts/build-registry.py --check
python scripts/validate-index.py`;

function Code({ children }: { children: string }) {
  return (
    <pre className="overflow-x-auto rounded-lg border border-tp-glass-edge bg-tp-glass-inner px-3.5 py-3 font-mono text-[12px] leading-[1.6] text-tp-ink-2">
      <code>{children}</code>
    </pre>
  );
}

export default function MarketplaceContributePage() {
  const { t } = useTranslation();
  const variants = useMotionVariants();

  const steps = [
    { titleKey: "stepForkTitle", bodyKey: "stepForkBody" },
    { titleKey: "stepAddTitle", bodyKey: "stepAddBody" },
    { titleKey: "stepBuildTitle", bodyKey: "stepBuildBody" },
    { titleKey: "stepPrTitle", bodyKey: "stepPrBody" },
  ] as const;

  const kinds = [
    { titleKey: "skillTitle", bodyKey: "skillBody", snippet: SKILL_SNIPPET },
    { titleKey: "mcpTitle", bodyKey: "mcpBody", snippet: MCP_SNIPPET },
    { titleKey: "pluginTitle", bodyKey: "pluginBody", snippet: PLUGIN_SNIPPET },
  ] as const;

  const checks = ["check1", "check2", "check3", "check4", "check5"] as const;

  return (
    <motion.div
      className="flex flex-col gap-4"
      variants={variants.fadeUp}
      initial="hidden"
      animate="visible"
    >
      {/* Hero */}
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
            data-testid="contribute-back-link"
          >
            <ChevronLeft className="h-3.5 w-3.5" aria-hidden />
            {t("marketplace.title")}
          </Link>
          <div className="inline-flex w-fit items-center gap-2.5 rounded-full border border-tp-glass-edge bg-tp-glass-inner-strong py-1 pl-3 pr-3 font-mono text-[11px] text-tp-ink-2">
            {t("marketplace.contribute.leadPill")}
          </div>
          <h1 className="text-balance font-sans text-[28px] font-semibold leading-[1.15] tracking-[-0.025em] text-tp-ink sm:text-[32px]">
            {t("marketplace.contribute.title")}
          </h1>
          <p className="max-w-[72ch] text-[14.5px] leading-[1.6] text-tp-ink-2">
            {t("marketplace.contribute.subtitle")}
          </p>
          <div className="mt-1 flex flex-wrap items-center gap-2.5">
            <a
              href={REPO_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-2 rounded-lg border border-tp-glass-edge bg-tp-glass-inner px-3 py-2 text-[13px] font-medium text-tp-ink-2 transition-colors hover:bg-tp-glass-inner-hover hover:text-tp-ink"
            >
              <ExternalLink className="h-3.5 w-3.5" aria-hidden />
              {t("marketplace.contribute.openRepo")}
            </a>
            <a
              href={GUIDE_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-2 rounded-lg border border-tp-glass-edge bg-tp-glass-inner px-3 py-2 text-[13px] font-medium text-tp-ink-2 transition-colors hover:bg-tp-glass-inner-hover hover:text-tp-ink"
            >
              <BookOpen className="h-3.5 w-3.5" aria-hidden />
              {t("marketplace.contribute.viewGuide")}
            </a>
          </div>
        </div>
      </GlassPanel>

      {/* How it works + steps */}
      <GlassPanel as="section" className="flex flex-col gap-4 p-6">
        <div className="flex flex-col gap-1.5">
          <h2 className="font-sans text-[18px] font-semibold tracking-[-0.02em] text-tp-ink">
            {t("marketplace.contribute.howTitle")}
          </h2>
          <p className="max-w-[78ch] text-[14px] leading-[1.6] text-tp-ink-2">
            {t("marketplace.contribute.howBody")}
          </p>
        </div>
        <ol className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {steps.map((step, i) => {
            const Icon = STEP_ICONS[i];
            return (
              <li
                key={step.titleKey}
                className="flex flex-col gap-2 rounded-xl border border-tp-glass-edge bg-tp-glass-inner p-4"
              >
                <div className="flex items-center gap-2">
                  <span className="inline-flex h-6 w-6 items-center justify-center rounded-full border border-tp-glass-edge font-mono text-[11px] text-tp-ink-2">
                    {i + 1}
                  </span>
                  <Icon className="h-4 w-4 text-tp-amber" aria-hidden />
                </div>
                <p className="font-sans text-[13.5px] font-semibold text-tp-ink">
                  {t(`marketplace.contribute.${step.titleKey}`)}
                </p>
                <p className="text-[12.5px] leading-[1.55] text-tp-ink-2">
                  {t(`marketplace.contribute.${step.bodyKey}`)}
                </p>
              </li>
            );
          })}
        </ol>
      </GlassPanel>

      {/* What you can submit */}
      <GlassPanel as="section" className="flex flex-col gap-4 p-6">
        <h2 className="font-sans text-[18px] font-semibold tracking-[-0.02em] text-tp-ink">
          {t("marketplace.contribute.kindsTitle")}
        </h2>
        <div className="grid gap-3 lg:grid-cols-3">
          {kinds.map((k) => (
            <div
              key={k.titleKey}
              className="flex flex-col gap-2.5 rounded-xl border border-tp-glass-edge bg-tp-glass-inner p-4"
            >
              <p className="font-sans text-[14px] font-semibold text-tp-ink">
                {t(`marketplace.contribute.${k.titleKey}`)}
              </p>
              <p className="text-[12.5px] leading-[1.55] text-tp-ink-2">
                {t(`marketplace.contribute.${k.bodyKey}`)}
              </p>
              <div className="mt-auto">
                <p className="mb-1.5 font-mono text-[10.5px] uppercase tracking-wide text-tp-ink-3">
                  {t("marketplace.contribute.filesLabel")}
                </p>
                <Code>{k.snippet}</Code>
              </div>
            </div>
          ))}
        </div>
      </GlassPanel>

      {/* Open the PR + checklist */}
      <GlassPanel as="section" className="flex flex-col gap-4 p-6">
        <div className="flex flex-col gap-1.5">
          <h2 className="font-sans text-[18px] font-semibold tracking-[-0.02em] text-tp-ink">
            {t("marketplace.contribute.prTitle")}
          </h2>
          <p className="max-w-[78ch] text-[14px] leading-[1.6] text-tp-ink-2">
            {t("marketplace.contribute.prBody")}
          </p>
        </div>
        <Code>{PR_SNIPPET}</Code>
        <div className="flex flex-col gap-2">
          <p className="font-sans text-[13.5px] font-semibold text-tp-ink">
            {t("marketplace.contribute.checklistTitle")}
          </p>
          <ul className="flex flex-col gap-1.5">
            {checks.map((c) => (
              <li
                key={c}
                className="flex items-start gap-2 text-[13px] leading-[1.55] text-tp-ink-2"
              >
                <span className="mt-[3px] inline-flex h-3.5 w-3.5 flex-none items-center justify-center rounded-[4px] border border-tp-glass-edge font-mono text-[9px] text-tp-amber">
                  ✓
                </span>
                {t(`marketplace.contribute.${c}`)}
              </li>
            ))}
          </ul>
        </div>
      </GlassPanel>

      {/* Security */}
      <GlassPanel as="section" className="flex items-start gap-3 p-6">
        <ShieldCheck className="mt-0.5 h-5 w-5 flex-none text-tp-amber" aria-hidden />
        <div className="flex flex-col gap-1">
          <h2 className="font-sans text-[15px] font-semibold tracking-[-0.01em] text-tp-ink">
            {t("marketplace.contribute.securityTitle")}
          </h2>
          <p className="max-w-[80ch] text-[13px] leading-[1.6] text-tp-ink-2">
            {t("marketplace.contribute.securityBody")}
          </p>
        </div>
      </GlassPanel>
    </motion.div>
  );
}
