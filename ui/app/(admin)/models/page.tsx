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
import { Zap } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ProvidersAdminContent } from "@/components/model-hub/providers-admin-content";
import { OAuthPanel } from "@/components/model-hub/oauth-panel";
import { RoutingSection } from "@/components/model-hub/routing-section";
import { CredentialsAdvanced } from "@/components/model-hub/credentials-advanced";
import { ProviderSetupFlow } from "@/components/model-hub/provider-setup-flow";
import { useSetupStatus } from "@/lib/hooks/use-setup-status";

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
  const setupStatus = useSetupStatus();
  const [quickSetupOpen, setQuickSetupOpen] = React.useState(false);
  // Bumped when a quick-setup run adds providers/aliases so the routing
  // table remounts and re-seeds from fresh server data. Without this its
  // one-time local snapshot stays stale, and a later "Save" would post
  // the stale full alias map — the backend bulk path drops omitted names,
  // silently wiping the just-added aliases (self-review P2). Safe because
  // the dialog is modal: the routing table has no concurrent edits.
  const [routingSeed, setRoutingSeed] = React.useState(0);

  const setTab = React.useCallback(
    (next: TabId) => {
      // Keep the URL shareable — deep links land on the same tab.
      router.replace(`/models?tab=${next}`, { scroll: false });
    },
    [router],
  );

  // No provider registered at all → the providers tab leads with the
  // guided setup flow instead of an empty table.
  const showInlineFlow =
    !setupStatus.loading &&
    !setupStatus.errored &&
    setupStatus.providerCount === 0;

  return (
    <>
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight">
            {t("modelHub.title")}
          </h1>
          <p className="text-sm text-sg-ink-3">{t("modelHub.subtitle")}</p>
        </div>
        <Button
          type="button"
          size="sm"
          onClick={() => setQuickSetupOpen(true)}
          data-testid="model-hub-quick-setup-btn"
        >
          <Zap className="h-3.5 w-3.5" aria-hidden />
          {t("setupFlow.quickSetup")}
        </Button>
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
          {showInlineFlow ? (
            <section
              className="flex flex-col gap-3 rounded-sg-lg border border-sg-accent/25 bg-sg-card p-4 shadow-sg-2"
              data-testid="model-hub-inline-setup"
            >
              <div className="space-y-0.5">
                <h2 className="text-sm font-semibold">
                  {t("setupFlow.emptyStateTitle")}
                </h2>
                <p className="text-xs text-sg-ink-3">
                  {t("setupFlow.emptyStateBody")}
                </p>
              </div>
              <ProviderSetupFlow variant="page" />
            </section>
          ) : null}
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
          <RoutingSection key={routingSeed} />
        </div>
      ) : (
        <div className="flex flex-col gap-6" data-testid="model-hub-panel-advanced">
          <CredentialsAdvanced />
        </div>
      )}

      <Dialog open={quickSetupOpen} onOpenChange={setQuickSetupOpen}>
        <DialogContent className="max-w-md" data-testid="model-hub-quick-setup-dialog">
          <DialogHeader>
            <DialogTitle>{t("setupFlow.dialogTitle")}</DialogTitle>
            <DialogDescription>{t("setupFlow.dialogDesc")}</DialogDescription>
          </DialogHeader>
          <ProviderSetupFlow
            onStatusChange={(s) => {
              if (s.modelsAdded) setRoutingSeed((n) => n + 1);
            }}
            variant="dialog"
            onComplete={() => setQuickSetupOpen(false)}
          />
        </DialogContent>
      </Dialog>
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
