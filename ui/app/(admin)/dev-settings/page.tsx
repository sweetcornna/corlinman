"use client";

/**
 * `/admin/dev-settings` — Developer pages dashboard.
 *
 * Two halves:
 *   1. Top: a `<Switch>` bound to `useDevMode().setEnabled` that toggles
 *      sidebar visibility of the 17 power-user pages. Persisted to
 *      `localStorage`.
 *   2. Bottom: a grid of cards — one per hidden page — so the surface stays
 *      discoverable even when the toggle is off.
 *
 * The page itself is always reachable from the always-visible Developer
 * Settings sidebar link, regardless of the toggle state.
 */

import * as React from "react";
import Link from "next/link";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { useDevMode } from "@/lib/dev-mode";
import { Switch } from "@/components/ui/switch";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

/**
 * Static metadata for each hidden page. The route is the source of truth
 * for the i18n lookup (`devSettings.pages.<key>.title|description`).
 *
 * Keep the order in sync with `SIDEBAR_DEV_ITEMS` so the dashboard layout
 * matches the order the toggle would surface in the sidebar.
 */
const DEV_PAGE_KEYS = [
  "config",
  "tenants",
  "credentials",
  "agents",
  "skills",
  "plugins",
  "hooks",
  "rag",
  "profiles",
  "nodes",
  "evolution",
] as const;

type DevPageKey = (typeof DEV_PAGE_KEYS)[number];

const ROUTE_FOR_KEY: Record<DevPageKey, string> = {
  config: "/config",
  tenants: "/tenants",
  credentials: "/credentials",
  agents: "/agents",
  skills: "/skills",
  plugins: "/plugins",
  hooks: "/hooks",
  rag: "/rag",
  profiles: "/profiles",
  nodes: "/nodes",
  evolution: "/evolution",
};

export default function DevSettingsPage() {
  const { t } = useTranslation();
  const { enabled, setEnabled } = useDevMode();

  return (
    <div className="space-y-6" data-testid="dev-settings-page">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">
          {t("devSettings.title")}
        </h1>
        <p className="max-w-2xl text-sm text-tp-ink-3">
          {t("devSettings.subtitle")}
        </p>
      </header>

      <section
        className={cn(
          "flex flex-col gap-3 rounded-lg border border-tp-glass-edge bg-tp-glass p-4",
          "sm:flex-row sm:items-center sm:justify-between",
        )}
        data-testid="dev-settings-toggle-row"
      >
        <div className="space-y-1">
          <label
            htmlFor="dev-settings-toggle"
            className="text-sm font-medium text-tp-ink"
          >
            {t("devSettings.toggleLabel")}
          </label>
          <p className="max-w-xl text-xs text-tp-ink-3">
            {t("devSettings.toggleHint")}
          </p>
        </div>
        <Switch
          id="dev-settings-toggle"
          checked={enabled}
          onCheckedChange={setEnabled}
          aria-label={t("devSettings.toggleLabel")}
          data-testid="dev-settings-toggle"
        />
      </section>

      <section
        className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3"
        data-testid="dev-settings-grid"
      >
        {DEV_PAGE_KEYS.map((key) => {
          const route = ROUTE_FOR_KEY[key];
          const title = t(`devSettings.pages.${key}.title`);
          const description = t(`devSettings.pages.${key}.description`);
          return (
            <Link
              key={key}
              href={route as never}
              className="group block focus-visible:outline-none"
              data-testid={`dev-settings-card-${key}`}
            >
              <Card
                className={cn(
                  "h-full transition-colors duration-150",
                  "hover:border-tp-amber/50 hover:bg-tp-glass-inner-hover",
                  "group-focus-visible:border-tp-amber group-focus-visible:bg-tp-glass-inner-hover",
                )}
              >
                <CardHeader className="space-y-1.5 p-4 pb-2">
                  <CardTitle className="text-sm font-medium text-tp-ink">
                    {title}
                  </CardTitle>
                  <CardDescription className="text-xs text-tp-ink-3">
                    {description}
                  </CardDescription>
                </CardHeader>
                <CardContent className="p-4 pt-0">
                  <span className="text-xs font-medium text-tp-amber opacity-80 transition-opacity group-hover:opacity-100">
                    {t("devSettings.cardOpen")}
                  </span>
                </CardContent>
              </Card>
            </Link>
          );
        })}
      </section>
    </div>
  );
}

/** Exposed for the sidebar test, which asserts dev-mode parity. */
export { DEV_PAGE_KEYS };
