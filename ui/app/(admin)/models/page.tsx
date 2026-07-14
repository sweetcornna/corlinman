"use client";

/**
 * /models — canonical "Models & Keys" page (PR4 model-hub consolidation).
 *
 * Hosts the three surfaces that used to be spread across /providers,
 * /credentials, and /models as tabs:
 *
 *   - providers  ("Providers & Keys"): built-in + custom provider registry
 *                (ProvidersAdminContent) with the OAuth panel below it.
 *   - routing    ("Model routing"): alias table + default model + per-alias
 *                params (RoutingSection).
 *   - advanced   ("Advanced credentials"): raw per-provider credential
 *                fields (CredentialsAdvanced) behind a "this does not
 *                register a provider" warning.
 *
 * The active tab is driven by the `?tab=` query param (static-export-safe
 * via `useSearchParams`; default = providers) so deep links from the old
 * redirect stubs keep working. Only the ACTIVE tab's content is mounted so
 * a visit doesn't fire every query family at once.
 */

import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { useTranslation } from "react-i18next";

import { ProvidersAdminContent } from "@/components/model-hub/providers-admin-content";
import { OAuthPanel } from "@/components/model-hub/oauth-panel";
import { RoutingSection } from "@/components/model-hub/routing-section";
import { CredentialsAdvanced } from "@/components/model-hub/credentials-advanced";

const TABS = ["providers", "routing", "advanced"] as const;
type TabId = (typeof TABS)[number];

const TAB_LABEL_KEYS: Record<TabId, string> = {
  providers: "modelHub.tabs.providers",
  routing: "modelHub.tabs.routing",
  advanced: "modelHub.tabs.advanced",
};

function resolveTab(raw: string | null): TabId {
  return TABS.includes(raw as TabId) ? (raw as TabId) : "providers";
}

function ModelHub() {
  const { t } = useTranslation();
  const router = useRouter();
  const qc = useQueryClient();
  const searchParams = useSearchParams();
  const tab = resolveTab(searchParams?.get("tab") ?? null);

  const setTab = React.useCallback(
    (next: TabId) => {
      // Keep the URL shareable — deep links land on the same tab.
      router.replace(`/models?tab=${next}`, { scroll: false });
    },
    [router],
  );

  return (
    <>
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">
          {t("modelHub.title")}
        </h1>
        <p className="text-sm text-sg-ink-3">{t("modelHub.subtitle")}</p>
      </header>

      <nav
        role="tablist"
        aria-label={t("modelHub.title")}
        className="flex items-center gap-1 border-b border-sg-border"
      >
        {TABS.map((id) => {
          const active = tab === id;
          return (
            <button
              key={id}
              role="tab"
              type="button"
              aria-selected={active}
              data-testid={`model-hub-tab-${id}`}
              onClick={() => setTab(id)}
              className={
                "px-3 py-1.5 text-[12.5px] font-medium transition-colors " +
                (active
                  ? "border-b-2 border-sg-accent text-sg-ink"
                  : "border-b-2 border-transparent text-sg-ink-3 hover:text-sg-ink-2")
              }
            >
              {t(TAB_LABEL_KEYS[id])}
            </button>
          );
        })}
      </nav>

      {/* Mount only the active tab so a visit doesn't fire every query
          family (providers + custom + oauth + models + credentials) at
          once. */}
      {tab === "providers" ? (
        <div className="flex flex-col gap-6" data-testid="model-hub-panel-providers">
          <ProvidersAdminContent
            onCustomProvidersChanged={() =>
              // The advanced tab's credential cards derive from the same
              // TOML — mark them stale so they refetch on next mount.
              qc.invalidateQueries({ queryKey: ["admin", "credentials"] })
            }
          />
          <OAuthPanel />
        </div>
      ) : tab === "routing" ? (
        <div className="flex flex-col gap-6" data-testid="model-hub-panel-routing">
          <RoutingSection />
        </div>
      ) : (
        <div className="flex flex-col gap-6" data-testid="model-hub-panel-advanced">
          <CredentialsAdvanced />
        </div>
      )}
    </>
  );
}

export default function ModelsPage() {
  // `useSearchParams` needs a Suspense boundary to be static-export-safe.
  return (
    <React.Suspense fallback={null}>
      <ModelHub />
    </React.Suspense>
  );
}
