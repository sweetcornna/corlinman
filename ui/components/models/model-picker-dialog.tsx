"use client";

/**
 * <ModelPickerDialog> — hermes-style two-column model picker.
 *
 * Layout:
 *   ┌─ search ────────────────────────────────────┐
 *   │ provider (200px) │ model (1fr)              │
 *   └─ cancel · confirm ─────────────────────────-┘
 *
 * Single search box filters BOTH columns: providers stay visible if their
 * name/kind matches OR if any of their models matches; the right column is
 * filtered against the same query.
 *
 * Caching: a `Map<string, Model[]>` lives for the lifetime of a single open
 * cycle. Closing + reopening the dialog refetches (handled by the parent
 * un/remounting the component).
 *
 * Used from:
 *   - /admin/models (add alias / change alias target)
 *   - /admin/agents per-agent override (TODO: surface not yet built)
 *
 * Models for the selected provider are fetched via
 * :func:`getProviderModels` (W2.3) which hits
 * ``GET /admin/providers/{name}/models``.
 */

import * as React from "react";
import { useTranslation } from "react-i18next";
import { Check, Search } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import {
  fetchProviders,
  getProviderModels,
  type ProviderView,
} from "@/lib/api";
import { useQuery } from "@tanstack/react-query";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ModelPickerProvider = {
  name: string;
  kind: string;
  enabled: boolean;
};

export type ModelEntry = {
  id: string;
  display_name?: string;
  created_at?: string;
};

export type ModelPickerSelection = {
  provider: string;
  model: string;
};

export type ModelPickerDialogProps = {
  open: boolean;
  onClose: () => void;
  initialProvider?: string;
  initialModel?: string;
  /** When true, a single model click immediately confirms the selection. */
  confirmOnModelClick?: boolean;
  /**
   * Optional explicit provider list. If omitted, the dialog fetches from
   * `/admin/providers` (only enabled providers are displayed by default).
   */
  providers?: ModelPickerProvider[];
  onConfirm: (selection: ModelPickerSelection) => void;
};

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function ModelPickerDialog({
  open,
  onClose,
  initialProvider,
  initialModel,
  confirmOnModelClick = false,
  providers: providersProp,
  onConfirm,
}: ModelPickerDialogProps) {
  const { t } = useTranslation();

  // Fall back to /admin/providers when the caller doesn't supply a list.
  const providersQuery = useQuery<ProviderView[]>({
    queryKey: ["admin", "providers"],
    queryFn: fetchProviders,
    enabled: open && !providersProp,
    staleTime: 30_000,
  });

  const providers: ModelPickerProvider[] = React.useMemo(() => {
    if (providersProp) return providersProp;
    const arr = providersQuery.data ?? [];
    return arr
      .filter((p) => p.enabled)
      .map((p) => ({ name: p.name, kind: p.kind, enabled: p.enabled }));
  }, [providersProp, providersQuery.data]);

  const [selectedProvider, setSelectedProvider] = React.useState<string>("");
  const [selectedModel, setSelectedModel] = React.useState<string>("");
  const [query, setQuery] = React.useState("");

  // Models cache (per dialog-open lifetime, not survived across remounts).
  const [modelsCache, setModelsCache] = React.useState<
    Record<string, ModelEntry[]>
  >({});
  const [modelsLoading, setModelsLoading] = React.useState<
    Record<string, boolean>
  >({});
  const [modelsError, setModelsError] = React.useState<
    Record<string, string | null>
  >({});
  const modelsRequestSeqRef = React.useRef(0);
  const latestModelsRequestRef = React.useRef<Record<string, number>>({});

  // Seed selection from initial* on open.
  React.useEffect(() => {
    if (!open) return;
    setQuery("");
    if (initialProvider && providers.some((p) => p.name === initialProvider)) {
      setSelectedProvider(initialProvider);
      setSelectedModel(initialModel ?? "");
    } else {
      setSelectedProvider("");
      setSelectedModel("");
    }
    // We intentionally only re-seed on open transitions, not on every
    // initialProvider change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // If the dialog opens before providers finish loading, seed the
  // requested initial provider once it becomes available.
  React.useEffect(() => {
    if (!open || selectedProvider || !initialProvider) return;
    if (!providers.some((p) => p.name === initialProvider)) return;
    setSelectedProvider(initialProvider);
    setSelectedModel(initialModel ?? "");
  }, [open, providers, selectedProvider, initialProvider, initialModel]);

  // Auto-pick first provider once the list resolves and nothing's selected.
  React.useEffect(() => {
    if (!open || selectedProvider) return;
    if (providers.length === 0) return;
    if (initialProvider && providers.some((p) => p.name === initialProvider))
      return; // honour pending init seed
    setSelectedProvider(providers[0]!.name);
  }, [open, providers, selectedProvider, initialProvider]);

  // Lazy-load the models list for the selected provider.
  React.useEffect(() => {
    if (!open || !selectedProvider) return;
    if (modelsCache[selectedProvider]) return;

    const providerName = selectedProvider;
    const requestId = modelsRequestSeqRef.current + 1;
    modelsRequestSeqRef.current = requestId;
    latestModelsRequestRef.current[providerName] = requestId;
    const controller = new AbortController();

    setModelsLoading((s) => ({ ...s, [providerName]: true }));
    setModelsError((s) => ({ ...s, [providerName]: null }));
    getProviderModels(providerName, { signal: controller.signal })
      .then((res) => {
        if (latestModelsRequestRef.current[providerName] !== requestId) return;
        if (res.error) {
          setModelsError((s) => ({ ...s, [providerName]: res.error ?? null }));
          setModelsCache((s) => ({ ...s, [providerName]: [] }));
          return;
        }
        setModelsCache((s) => ({ ...s, [providerName]: res.models ?? [] }));
      })
      .catch((err: unknown) => {
        if (latestModelsRequestRef.current[providerName] !== requestId) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        setModelsError((s) => ({
          ...s,
          [providerName]: err instanceof Error ? err.message : String(err),
        }));
      })
      .finally(() => {
        if (latestModelsRequestRef.current[providerName] !== requestId) return;
        setModelsLoading((s) => ({ ...s, [providerName]: false }));
      });
    return () => {
      controller.abort();
    };
  }, [open, selectedProvider, modelsCache]);

  const needle = query.trim().toLowerCase();

  const filteredProviders = React.useMemo(() => {
    if (!needle) return providers;
    return providers.filter((p) => {
      if (p.name.toLowerCase().includes(needle)) return true;
      if (p.kind.toLowerCase().includes(needle)) return true;
      const ms = modelsCache[p.name] ?? [];
      return ms.some((m) =>
        (m.id + " " + (m.display_name ?? "")).toLowerCase().includes(needle),
      );
    });
  }, [providers, needle, modelsCache]);

  const allModelsForSelected = React.useMemo<ModelEntry[]>(
    () => (selectedProvider ? modelsCache[selectedProvider] ?? [] : []),
    [selectedProvider, modelsCache],
  );

  const filteredModels = React.useMemo(() => {
    if (!needle) return allModelsForSelected;
    return allModelsForSelected.filter((m) =>
      (m.id + " " + (m.display_name ?? "")).toLowerCase().includes(needle),
    );
  }, [allModelsForSelected, needle]);

  const canConfirm = !!selectedProvider && !!selectedModel;
  const confirm = React.useCallback(() => {
    if (!canConfirm) return;
    onConfirm({ provider: selectedProvider, model: selectedModel });
    onClose();
  }, [canConfirm, onConfirm, onClose, selectedProvider, selectedModel]);

  // ESC closes.
  React.useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const providersLoading = !providersProp && providersQuery.isPending;
  const providersError = !providersProp && providersQuery.isError;

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen) onClose();
      }}
    >
      <DialogContent
        className="!z-[100] flex h-[min(480px,85dvh)] !max-w-[600px] flex-col overflow-hidden p-0"
        data-testid="model-picker-dialog"
      >
        <header className="border-b border-sg-border px-4 py-3">
          <DialogTitle className="text-sm font-semibold tracking-tight">
            {t("models.picker.title")}
          </DialogTitle>
          <DialogDescription className="sr-only">
            {t("models.picker.searchPlaceholder")}
          </DialogDescription>
        </header>

        <div className="border-b border-sg-border px-4 py-2">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-sg-ink-3" />
            <Input
              autoFocus
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t("models.picker.searchPlaceholder")}
              className="h-8 pl-7 font-mono text-xs"
              data-testid="model-picker-search"
            />
          </div>
        </div>

        <div className="grid min-h-0 flex-1 grid-cols-[200px_1fr] overflow-hidden">
          <ProviderColumn
            loading={providersLoading}
            error={providersError ? t("models.picker.loadFailed") : null}
            providers={filteredProviders}
            total={providers.length}
            selected={selectedProvider}
            onSelect={(name) => {
              setSelectedProvider(name);
              setSelectedModel("");
            }}
            emptyLabel={t("models.picker.empty")}
            modelsCache={modelsCache}
          />

          <ModelColumn
            providerName={selectedProvider}
            loading={!!selectedProvider && !!modelsLoading[selectedProvider]}
            error={selectedProvider ? modelsError[selectedProvider] : null}
            models={filteredModels}
            allModels={allModelsForSelected}
            selectedModel={selectedModel}
            onSelect={setSelectedModel}
            confirmOnModelClick={confirmOnModelClick}
            onConfirm={(id) => {
              setSelectedModel(id);
              // Wait a tick so the new selectedModel is visible to confirm().
              window.setTimeout(() => {
                onConfirm({ provider: selectedProvider, model: id });
                onClose();
              }, 0);
            }}
            modelsFetchErrorLabel={t("models.picker.modelsFetchError")}
            loadingLabel={t("models.picker.loading")}
          />
        </div>

        <footer className="flex items-center justify-end gap-2 border-t border-sg-border px-4 py-3">
          <Button variant="outline" size="sm" onClick={onClose}>
            {t("models.picker.cancel")}
          </Button>
          <Button
            size="sm"
            onClick={confirm}
            disabled={!canConfirm}
            data-testid="model-picker-confirm"
          >
            {t("models.picker.confirm")}
          </Button>
        </footer>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Provider column
// ---------------------------------------------------------------------------

function ProviderColumn({
  loading,
  error,
  providers,
  total,
  selected,
  onSelect,
  emptyLabel,
  modelsCache,
}: {
  loading: boolean;
  error: string | null;
  providers: ModelPickerProvider[];
  total: number;
  selected: string;
  onSelect: (name: string) => void;
  emptyLabel: string;
  modelsCache: Record<string, ModelEntry[]>;
}) {
  return (
    <div className="overflow-y-auto border-r border-sg-border">
      {loading ? (
        <div className="p-3 text-xs text-sg-ink-3">…</div>
      ) : error ? (
        <div className="p-3 text-xs text-destructive">{error}</div>
      ) : providers.length === 0 ? (
        <div className="p-3 text-xs italic text-sg-ink-3">
          {total === 0 ? (
            <>
              {emptyLabel}{" "}
              <a className="underline" href="/providers">
                /admin/providers
              </a>
            </>
          ) : (
            "—"
          )}
        </div>
      ) : (
        providers.map((p) => {
          const active = p.name === selected;
          const count = (modelsCache[p.name] ?? []).length;
          return (
            <button
              key={p.name}
              type="button"
              onClick={() => onSelect(p.name)}
              className={cn(
                "block w-full border-l-2 px-3 py-2 text-left text-xs transition-colors hover:bg-sg-inset-hover",
                active
                  ? "border-l-sg-accent bg-sg-inset-hover"
                  : "border-l-transparent",
              )}
              data-testid={`model-picker-provider-${p.name}`}
            >
              <div className="truncate font-medium">{p.name}</div>
              <div className="truncate font-mono text-[10px] text-sg-ink-3">
                {p.kind}
                {count > 0 ? ` · ${count}` : ""}
              </div>
            </button>
          );
        })
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Model column
// ---------------------------------------------------------------------------

function ModelColumn({
  providerName,
  loading,
  error,
  models,
  allModels,
  selectedModel,
  onSelect,
  confirmOnModelClick,
  onConfirm,
  modelsFetchErrorLabel,
  loadingLabel,
}: {
  providerName: string;
  loading: boolean;
  error: string | null | undefined;
  models: ModelEntry[];
  allModels: ModelEntry[];
  selectedModel: string;
  onSelect: (id: string) => void;
  confirmOnModelClick: boolean;
  onConfirm: (id: string) => void;
  modelsFetchErrorLabel: string;
  loadingLabel: string;
}) {
  if (!providerName) {
    return (
      <div className="overflow-y-auto p-3 text-xs italic text-sg-ink-3">
        ←
      </div>
    );
  }

  if (loading) {
    return (
      <div className="overflow-y-auto p-3 text-xs text-sg-ink-3">
        {loadingLabel}
      </div>
    );
  }

  if (error) {
    return (
      <div className="overflow-y-auto p-3 text-xs text-destructive">
        {modelsFetchErrorLabel}
        {": "}
        <span className="font-mono">{error}</span>
      </div>
    );
  }

  if (models.length === 0) {
    return (
      <div className="overflow-y-auto p-3 text-xs italic text-sg-ink-3">
        {allModels.length === 0 ? "—" : "no matches"}
      </div>
    );
  }

  return (
    <div className="overflow-y-auto">
      {models.map((m) => {
        const active = m.id === selectedModel;
        return (
          <button
            key={m.id}
            type="button"
            onClick={() => {
              onSelect(m.id);
              if (confirmOnModelClick) onConfirm(m.id);
            }}
            onDoubleClick={() => onConfirm(m.id)}
            className={cn(
              "flex w-full items-center gap-2 px-3 py-1.5 text-left font-mono text-xs transition-colors hover:bg-sg-inset-hover",
              active && "bg-sg-inset-hover",
            )}
            data-testid={`model-picker-model-${m.id}`}
          >
            <Check
              className={cn(
                "h-3 w-3 shrink-0",
                active ? "text-sg-accent" : "text-transparent",
              )}
            />
            <span className="flex-1 truncate">
              {m.display_name ?? m.id}
              {m.display_name && m.display_name !== m.id ? (
                <span className="ml-2 text-sg-ink-3">({m.id})</span>
              ) : null}
            </span>
          </button>
        );
      })}
    </div>
  );
}

export default ModelPickerDialog;
