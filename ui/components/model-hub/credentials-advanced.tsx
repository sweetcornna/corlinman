"use client";

/**
 * Advanced credentials — the per-provider raw credential cards.
 *
 * Extracted from `app/(admin)/credentials/page.tsx` (PR4 model-hub
 * consolidation). Builds on `/admin/credentials*`; borrows hermes-agent's
 * EnvPage UX:
 *   - provider-grouped sections (collapsed-by-default when empty),
 *   - per-row eye-icon reveal of the "…last4" preview,
 *   - paste-only inputs with a soft "paste, don't type" nudge,
 *   - destructive ops gated behind a confirmation dialog,
 *   - toasts on every mutation.
 *
 * Plaintext values never leave the gateway; the section only ever asks the
 * server to redact + return previews.
 *
 * The warning banner up top exists because keys saved here do NOT register
 * a provider or bind any models — that lives on the Providers tab.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Plug, Search } from "@/components/icons";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import {
  ProviderGroupCard,
  getProviderPriority,
} from "@/components/credentials/provider-group-card";
import {
  CorlinmanApiError,
  deleteCredential,
  listCredentials,
  setCredential,
  setProviderEnabled,
  type CredentialProvider,
} from "@/lib/api";

const FIELD_LABEL_KEYS: Record<string, string> = {
  api_key: "credentials.fieldKeyApiKey",
  base_url: "credentials.fieldKeyBaseUrl",
  org_id: "credentials.fieldKeyOrgId",
  kind: "credentials.fieldKeyKind",
};

const EMPTY_CREDENTIAL_PROVIDERS: CredentialProvider[] = [];

function isProviderConfigured(p: CredentialProvider): boolean {
  return p.fields.some((f) => f.set);
}

export function CredentialsAdvanced() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [search, setSearch] = React.useState("");
  const [showEmpty, setShowEmpty] = React.useState(true);
  const [pendingDelete, setPendingDelete] = React.useState<{
    provider: string;
    key: string;
  } | null>(null);

  const credentials = useQuery({
    queryKey: ["admin", "credentials"],
    queryFn: listCredentials,
    retry: false,
  });

  const saveField = useMutation({
    mutationFn: async (vars: {
      provider: string;
      key: string;
      value: string;
    }) => setCredential(vars.provider, vars.key, vars.value),
    onSuccess: (_data, vars) => {
      toast.success(t("credentials.fieldSaved", { key: vars.key }));
      qc.invalidateQueries({ queryKey: ["admin", "credentials"] });
    },
    onError: (err, vars) => {
      if (err instanceof CorlinmanApiError && err.status === 400) {
        toast.error(t("credentials.unknownField", { key: vars.key }));
        return;
      }
      toast.error(
        t("credentials.saveFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  const removeField = useMutation({
    mutationFn: async (vars: { provider: string; key: string }) =>
      deleteCredential(vars.provider, vars.key),
    onSuccess: (_data, vars) => {
      toast.success(t("credentials.fieldDeleted", { key: vars.key }));
      setPendingDelete(null);
      qc.invalidateQueries({ queryKey: ["admin", "credentials"] });
    },
    onError: (err) => {
      toast.error(
        t("credentials.deleteFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  const toggleProvider = useMutation({
    mutationFn: async (vars: { provider: string; enabled: boolean }) =>
      setProviderEnabled(vars.provider, vars.enabled),
    onSuccess: (_data, vars) => {
      toast.success(
        t(
          vars.enabled
            ? "credentials.providerEnabled"
            : "credentials.providerDisabled",
          { provider: vars.provider },
        ),
      );
      qc.invalidateQueries({ queryKey: ["admin", "credentials"] });
    },
    onError: (err) => {
      toast.error(
        t("credentials.saveFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  const providers = credentials.data?.providers ?? EMPTY_CREDENTIAL_PROVIDERS;

  const filtered = React.useMemo(() => {
    const q = search.trim().toLowerCase();
    const matched = providers.filter((p) => {
      if (!showEmpty && !isProviderConfigured(p)) return false;
      if (!q) return true;
      return (
        p.name.toLowerCase().includes(q) || p.kind.toLowerCase().includes(q)
      );
    });
    // Apply the PROVIDER_GROUPS ordering — known providers float to
    // their declared priority, everything else falls back to alpha.
    return [...matched].sort((a, b) => {
      const pa = getProviderPriority(a.name);
      const pb = getProviderPriority(b.name);
      if (pa !== pb) return pa - pb;
      return a.name.localeCompare(b.name);
    });
  }, [providers, search, showEmpty]);

  const total = providers.length;
  const configured = providers.filter(isProviderConfigured).length;

  return (
    <div className="flex flex-col gap-6">
      <Alert variant="warning" data-testid="credentials-advanced-warning">
        {t("modelHub.advanced.warning")}
      </Alert>

      <div className="flex flex-wrap items-center gap-3">
        <div className="relative flex-1 min-w-[200px] max-w-md">
          <Search
            className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-sg-ink-3"
            aria-hidden
          />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("credentials.search")}
            className="pl-8"
            data-testid="credentials-search"
          />
        </div>
        <label className="flex items-center gap-2 text-xs text-sg-ink-2">
          <Switch
            checked={showEmpty}
            onCheckedChange={setShowEmpty}
            aria-label={t("credentials.showEmpty")}
            data-testid="credentials-show-empty"
          />
          <span>{t("credentials.showEmpty")}</span>
        </label>
        <p
          className="text-xs text-sg-ink-3"
          data-testid="credentials-count-summary"
        >
          {t("credentials.countSummary", { total, configured })}
        </p>
      </div>

      {credentials.isPending ? (
        <Skeleton className="h-40 w-full" />
      ) : credentials.isError ? (
        <p className="text-xs text-sg-err" data-testid="credentials-error">
          {t("credentials.loadFailed")}:{" "}
          {credentials.error instanceof Error
            ? credentials.error.message
            : String(credentials.error)}
        </p>
      ) : filtered.length === 0 ? (
        <Card data-testid="credentials-empty">
          <CardContent className="flex flex-col items-center gap-2 py-10 text-center">
            <Plug className="h-6 w-6 text-sg-ink-3" aria-hidden />
            <p className="text-sm text-sg-ink-3">
              {t("credentials.emptyState")}
            </p>
          </CardContent>
        </Card>
      ) : (
        <div className="flex flex-col gap-3">
          {filtered.map((p) => {
            const fieldLabels: Record<string, string> = {};
            for (const f of p.fields) {
              const labelKey = FIELD_LABEL_KEYS[f.key];
              fieldLabels[f.key] = labelKey ? t(labelKey) : f.key;
            }
            const savingKey =
              saveField.isPending && saveField.variables?.provider === p.name
                ? `${p.name}/${saveField.variables?.key ?? ""}`
                : removeField.isPending &&
                    removeField.variables?.provider === p.name
                  ? `${p.name}/${removeField.variables?.key ?? ""}`
                  : null;
            return (
              <ProviderGroupCard
                key={p.name}
                provider={p}
                fieldLabels={fieldLabels}
                savingKey={savingKey}
                onSaveField={async (key, value) => {
                  await saveField.mutateAsync({
                    provider: p.name,
                    key,
                    value,
                  });
                }}
                onDeleteField={(key) =>
                  setPendingDelete({ provider: p.name, key })
                }
                onToggleEnabled={(next) =>
                  toggleProvider.mutate({ provider: p.name, enabled: next })
                }
              />
            );
          })}
        </div>
      )}

      <Dialog
        open={!!pendingDelete}
        onOpenChange={(o) => {
          if (!o) setPendingDelete(null);
        }}
      >
        <DialogContent data-testid="credentials-delete-dialog">
          <DialogHeader>
            <DialogTitle>
              {pendingDelete
                ? t("credentials.deleteConfirmTitle", {
                    provider: pendingDelete.provider,
                    key: pendingDelete.key,
                  })
                : ""}
            </DialogTitle>
            <DialogDescription>
              {pendingDelete
                ? t("credentials.deleteConfirm", {
                    provider: pendingDelete.provider,
                    key: pendingDelete.key,
                  })
                : ""}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setPendingDelete(null)}
              data-testid="credentials-delete-cancel"
            >
              {t("common.cancel")}
            </Button>
            <Button
              variant="destructive"
              data-testid="credentials-delete-confirm"
              disabled={removeField.isPending}
              onClick={() => {
                if (pendingDelete) removeField.mutate(pendingDelete);
              }}
            >
              {t("common.delete")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
