"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { motion } from "framer-motion";

import { useMotionVariants } from "@/lib/motion";
import { MarketplaceHeader } from "@/components/marketplace/marketplace-header";
import { HubTab } from "@/components/skills/hub-tab";
import { McpBrowseTab } from "@/components/marketplace/mcp-browse-tab";
import { McpInstalledList } from "@/components/marketplace/mcp-installed-list";
import { PluginBrowseTab } from "@/components/marketplace/plugin-browse-tab";
import { PluginInstalledList } from "@/components/marketplace/plugin-installed-list";

/**
 * Marketplace admin page — unified browse + install hub.
 *
 * Structure (chosen over separate pages — mirrors the existing Skills page's
 * own installed/hub tab switcher, so a single tabbed page is the closest fit
 * to the app's conventions):
 *
 *   ┌─────────── glass-strong hero ──────────────┐
 *   │ lead pill · title · prose · ⌘K · accel CTA │
 *   └────────────────────────────────────────────┘
 *   [ Skills | MCP servers | Plugins ]   ← top-level kind switcher
 *     - Skills → reuses the existing <HubTab> (GitHub-backed); we do NOT
 *       rewrite the Skills page, just surface it here.
 *     - MCP / Plugins → Browse | Installed sub-tabs.
 *
 * The Acceleration settings card lives on its own sub-route
 * (`/marketplace/acceleration`) linked from the hero.
 */

type Kind = "skills" | "mcp" | "plugins";

export default function MarketplacePage() {
  const { t } = useTranslation();
  const variants = useMotionVariants();
  const [kind, setKind] = React.useState<Kind>("skills");

  return (
    <motion.div
      className="flex flex-col gap-4"
      variants={variants.fadeUp}
      initial="hidden"
      animate="visible"
    >
      <MarketplaceHeader />

      {/* Kind switcher — Skills | MCP servers | Plugins */}
      <nav
        role="tablist"
        aria-label={t("marketplace.title")}
        className="flex items-center gap-1 border-b border-sg-border"
      >
        {(["skills", "mcp", "plugins"] as const).map((id) => {
          const active = kind === id;
          return (
            <button
              key={id}
              role="tab"
              type="button"
              aria-selected={active}
              data-testid={`marketplace-tab-${id}`}
              onClick={() => setKind(id)}
              className={
                "px-3 py-1.5 text-[12.5px] font-medium transition-colors " +
                (active
                  ? "border-b-2 border-sg-accent text-sg-ink"
                  : "border-b-2 border-transparent text-sg-ink-3 hover:text-sg-ink-2")
              }
            >
              {t(`marketplace.tab.${id}`)}
            </button>
          );
        })}
      </nav>

      {kind === "skills" ? (
        <HubTab />
      ) : kind === "mcp" ? (
        <KindWithInstalled
          browse={<McpBrowseTab />}
          installed={<McpInstalledList />}
          idPrefix="mcp"
        />
      ) : (
        <KindWithInstalled
          browse={<PluginBrowseTab />}
          installed={<PluginInstalledList />}
          idPrefix="plugin"
        />
      )}
    </motion.div>
  );
}

/**
 * Browse | Installed sub-tab shell shared by the MCP and Plugin surfaces.
 * The Browse tab installs (staged, disabled); the Installed tab manages
 * lifecycle.
 */
function KindWithInstalled({
  browse,
  installed,
  idPrefix,
}: {
  browse: React.ReactNode;
  installed: React.ReactNode;
  idPrefix: string;
}) {
  const { t } = useTranslation();
  const [sub, setSub] = React.useState<"browse" | "installed">("browse");

  return (
    <div className="flex flex-col gap-4">
      <nav
        role="tablist"
        aria-label={t("marketplace.common.gridLabel")}
        className="flex items-center gap-1"
      >
        {(["browse", "installed"] as const).map((id) => {
          const active = sub === id;
          return (
            <button
              key={id}
              role="tab"
              type="button"
              aria-selected={active}
              data-testid={`${idPrefix}-subtab-${id}`}
              onClick={() => setSub(id)}
              className={
                "rounded-full px-3 py-1.5 text-[12px] font-medium transition-colors " +
                (active
                  ? "bg-sg-accent-soft text-sg-accent"
                  : "text-sg-ink-3 hover:bg-sg-inset hover:text-sg-ink-2")
              }
            >
              {id === "browse"
                ? t("marketplace.common.browseTab")
                : t("marketplace.common.installedTab")}
            </button>
          );
        })}
      </nav>

      {sub === "browse" ? browse : installed}
    </div>
  );
}
