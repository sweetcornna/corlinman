"use client";

/**
 * `/admin/dev-settings` — Developer pages dashboard.
 *
 * Two halves:
 *   1. Top: a `<Switch>` bound to `useDevMode().setEnabled` that toggles
 *      sidebar visibility of the developer-gated pages (see
 *      `@/lib/nav-registry`). Persisted to `localStorage`.
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
import { devSettingsPages } from "@/lib/nav-registry";
import { Switch } from "@/components/ui/switch";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

/**
 * The card grid derives from `devSettingsPages()` in `@/lib/nav-registry` —
 * exactly the developer-gated pages, in the same order the dev-mode toggle
 * surfaces them in the sidebar. Each page's `id` is the i18n lookup key
 * (`devSettings.pages.<id>.title|description`).
 */
const DEV_PAGES = devSettingsPages();

export default function DevSettingsPage() {
  const { t } = useTranslation();
  const { enabled, setEnabled } = useDevMode();

  return (
    <div className="space-y-6" data-testid="dev-settings-page">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">
          {t("devSettings.title")}
        </h1>
        <p className="max-w-2xl text-sm text-sg-ink-3">
          {t("devSettings.subtitle")}
        </p>
      </header>

      <section
        className={cn(
          "flex flex-col gap-3 rounded-lg border border-sg-border bg-sg-card p-4",
          "sm:flex-row sm:items-center sm:justify-between",
        )}
        data-testid="dev-settings-toggle-row"
      >
        <div className="space-y-1">
          <label
            htmlFor="dev-settings-toggle"
            className="text-sm font-medium text-sg-ink"
          >
            {t("devSettings.toggleLabel")}
          </label>
          <p className="max-w-xl text-xs text-sg-ink-3">
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
        {DEV_PAGES.map((page) => {
          const title = t(`devSettings.pages.${page.id}.title`);
          const description = t(`devSettings.pages.${page.id}.description`);
          return (
            <Link
              key={page.id}
              href={page.href}
              className="group block focus-visible:outline-none"
              data-testid={`dev-settings-card-${page.id}`}
            >
              <Card
                className={cn(
                  "h-full transition-colors duration-150",
                  "hover:border-sg-accent/50 hover:bg-sg-inset-hover",
                  "group-focus-visible:border-sg-accent group-focus-visible:bg-sg-inset-hover",
                )}
              >
                <CardHeader className="space-y-1.5 p-4 pb-2">
                  <CardTitle className="text-sm font-medium text-sg-ink">
                    {title}
                  </CardTitle>
                  <CardDescription className="text-xs text-sg-ink-3">
                    {description}
                  </CardDescription>
                </CardHeader>
                <CardContent className="p-4 pt-0">
                  <span className="text-xs font-medium text-sg-accent opacity-80 transition-opacity group-hover:opacity-100">
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
