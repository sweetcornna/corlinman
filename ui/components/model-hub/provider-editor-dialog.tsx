"use client";

/**
 * Provider add/edit dialog.
 *
 * Extracted verbatim from `app/(admin)/providers/page.tsx` (PR4 model-hub
 * consolidation). Upserts via POST /admin/providers with a dynamic params
 * form driven by the provider's `params_schema`, and hosts the model
 * discovery + "add models as aliases" flow (see `./alias-helpers.ts` for
 * the pure safety rails behind it).
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { motion } from "framer-motion";
import {
  Check,
  Copy,
  Loader2,
  Plus,
  PlusCircle,
  RefreshCw,
} from "@/components/icons";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  fetchModels,
  getProviderModels,
  probeProviderModels,
  upsertAlias,
  upsertProvider,
  type ModelsResponse,
  type ProviderKind,
  type ProviderModel,
  type ProviderView,
} from "@/lib/api";
import {
  DynamicParamsForm,
  validateAgainstSchema,
} from "@/components/dynamic-params-form";
import { cn } from "@/lib/utils";
import { SETUP_PRESETS } from "./presets";
import {
  BLANK_DRAFT,
  KINDS,
  computeAddModelsGate,
  extractAliasBindings,
  partitionAliasCandidates,
  patchAffectsPersistedProvider,
  shouldUseSavedModelDiscovery,
  toDraft,
  toModelProbe,
  toUpsert,
  type DraftProvider,
  type KeySource,
  type ModelDiscoveryRequest,
} from "./alias-helpers";

export interface ProviderEditorDialogProps {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  editing: ProviderView | null;
}

/**
 * Canonical api-key env var for a provider kind, sourced from the SAME
 * presets the guided setup flow uses (`./presets.ts`). Prefers the preset
 * whose id matches the kind; for `openai_compatible` that falls through to
 * the generic "custom" preset (the vendor-pinned xai preset is skipped via
 * its `defaultBaseUrl`). Kinds without a preset yield "" — no prefill.
 */
function suggestedEnvVarForKind(kind: ProviderKind): string {
  const exact = SETUP_PRESETS.find((p) => p.id === kind);
  if (exact) return exact.suggestedEnvVar;
  const generic = SETUP_PRESETS.find(
    (p) => p.kind === kind && !p.defaultBaseUrl,
  );
  return generic?.suggestedEnvVar ?? "";
}

export function ProviderEditorDialog({
  open,
  onOpenChange,
  editing,
}: ProviderEditorDialogProps) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [draft, setDraft] = React.useState<DraftProvider>(BLANK_DRAFT);
  const [modelDiscovery, setModelDiscovery] = React.useState<{
    models: ProviderModel[];
    error?: string;
  }>({ models: [] });
  const modelDiscoveryGeneration = React.useRef(0);
  // Params live behind the "advanced" disclosure; errors are computed
  // directly (not bubbled from the form) so save-gating stays correct even
  // while the DynamicParamsForm is collapsed/unmounted.
  const [showAdvanced, setShowAdvanced] = React.useState(false);
  // Per-model "add to corlinman" state: ids registered as aliases (bound to
  // this provider) during this dialog session, plus the in-flight set for
  // spinner / disabled affordances.
  const [addedModels, setAddedModels] = React.useState<Set<string>>(
    () => new Set(),
  );
  const [pendingAdds, setPendingAdds] = React.useState<Set<string>>(
    () => new Set(),
  );
  // Once the provider behind the aliases is persisted (existing edit, or the
  // first add on a brand-new draft) we don't re-upsert it on later adds.
  const providerPersistedRef = React.useRef(false);

  React.useEffect(() => {
    modelDiscoveryGeneration.current += 1;
    if (open) {
      // A brand-new draft prefills the canonical env var for its kind (kept
      // editable); an editing draft keeps whatever the server stored.
      setDraft(
        editing
          ? toDraft(editing)
          : {
              ...BLANK_DRAFT,
              api_key_env_name: suggestedEnvVarForKind(BLANK_DRAFT.kind),
            },
      );
      setModelDiscovery({ models: [] });
      setShowAdvanced(false);
      setAddedModels(new Set());
      setPendingAdds(new Set());
      // An existing provider already has its ``[providers.<name>]`` block;
      // a brand-new draft is persisted lazily on the first add.
      providerPersistedRef.current = !!editing;
    }
  }, [open, editing]);

  const updateDraft = React.useCallback((patch: Partial<DraftProvider>) => {
    modelDiscoveryGeneration.current += 1;
    setDraft((prev) => ({ ...prev, ...patch }));
    setModelDiscovery({ models: [] });
    if (patchAffectsPersistedProvider(patch)) {
      // ANY provider-config edit (base_url, key, kind, params, enabled, …)
      // invalidates the persisted ``[providers.<name>]`` block — force a
      // fresh upsert on the next add so aliases never bind against a stale
      // stored config.
      providerPersistedRef.current = false;
    }
    if (patch.name !== undefined) {
      // Provider identity changed — aliases would bind to the new name, so
      // also drop the per-model added markers.
      setAddedModels(new Set());
      setPendingAdds(new Set());
    }
  }, []);

  const schema = editing?.params_schema ?? { type: "object", properties: {} };
  const paramErrors = React.useMemo(
    () => validateAgainstSchema(schema, draft.params),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [editing, draft.params],
  );
  const hasErrors = Object.keys(paramErrors).length > 0;
  const nameOk = draft.name.trim().length > 0;
  const showBaseUrl = draft.kind === "openai_compatible";
  const baseUrlOk = !showBaseUrl || draft.base_url.trim().length > 0;

  // Invalid stored params would silently disable Save while collapsed —
  // force the disclosure open so the operator can see why.
  React.useEffect(() => {
    if (hasErrors) setShowAdvanced(true);
  }, [hasErrors]);

  // What actually goes over the wire. For every kind except
  // openai_compatible the backend supplies the base_url default, so the
  // (hidden) field is never sent.
  const effectiveDraft = React.useMemo<DraftProvider>(
    () => (showBaseUrl ? draft : { ...draft, base_url: "" }),
    [draft, showBaseUrl],
  );

  const saveMutation = useMutation({
    mutationFn: () => upsertProvider(toUpsert(effectiveDraft)),
    onSuccess: () => {
      toast.success(t("providers.saveSuccess"));
      qc.invalidateQueries({ queryKey: ["admin", "providers"] });
      onOpenChange(false);
    },
    onError: (err) =>
      toast.error(
        t("providers.saveFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      ),
  });

  const modelDiscoveryMutation = useMutation({
    mutationFn: async (request: ModelDiscoveryRequest) => {
      const res = shouldUseSavedModelDiscovery(
        request.editing,
        request.draft,
      )
        ? await getProviderModels(request.editing!.name)
        : await probeProviderModels(toModelProbe(request.draft, request.editing));
      return { generation: request.generation, res };
    },
    onSuccess: ({ generation, res }) => {
      if (generation !== modelDiscoveryGeneration.current) {
        return;
      }
      setModelDiscovery({ models: res.models ?? [], error: res.error });
      if (res.error) {
        toast.error(
          t("providers.modelsFetchFailed", {
            msg: res.error,
          }),
        );
      }
    },
    onError: (err, request) => {
      if (request.generation !== modelDiscoveryGeneration.current) {
        return;
      }
      const msg = err instanceof Error ? err.message : String(err);
      setModelDiscovery({ models: [], error: msg });
      toast.error(t("providers.modelsFetchFailed", { msg }));
    },
  });

  // Existing alias bindings — used to refuse a silent rebind of an alias
  // that currently routes to a different provider (see addModelsMutation).
  // Shares the ["admin", "models"] cache with the Models page.
  const modelsQuery = useQuery<ModelsResponse>({
    queryKey: ["admin", "models"],
    queryFn: fetchModels,
    enabled: open,
    retry: false,
  });

  const addModelsMutation = useMutation({
    mutationFn: async (ids: string[]) => {
      const providerName = draft.name.trim();
      if (!providerName) {
        throw new Error(t("providers.modelsAddNeedsName"));
      }
      // An alias with the same model id may already route to a DIFFERENT
      // provider; overwriting it would silently reroute every chat using it.
      // Partition against the loaded alias list and only upsert the safe ids
      // (no alias yet, or alias already on this provider).
      const { safe, conflicting } = partitionAliasCandidates(
        ids,
        extractAliasBindings(modelsQuery.data),
        providerName,
      );
      if (safe.length > 0) {
        // Persist the provider once so each alias references a real
        // ``[providers.<name>]`` block reflecting the CURRENT draft. A
        // brand-new draft is persisted on the first add; an editing draft is
        // re-persisted only after a config field changed this session
        // (updateDraft resets the flag — a pristine editing session never
        // re-upserts).
        if (!providerPersistedRef.current) {
          await upsertProvider(toUpsert(effectiveDraft));
          providerPersistedRef.current = true;
        }
        // Alias name == upstream model id, bound to this provider. This is
        // the mechanism that makes the model routable: chat resolves the
        // alias to (provider, model), so a custom provider's models stop
        // falling through to the public OpenAI default.
        for (const id of safe) {
          await upsertAlias({ name: id, provider: providerName, model: id });
        }
      }
      return { added: safe, skipped: conflicting };
    },
    onMutate: (ids) => {
      setPendingAdds((prev) => new Set([...prev, ...ids]));
    },
    onSuccess: ({ added, skipped }) => {
      if (added.length > 0) {
        setAddedModels((prev) => new Set([...prev, ...added]));
        toast.success(
          t("providers.modelsAddedToast", { count: added.length }),
        );
        // Surface the new aliases on the Models page + chat picker, and
        // refresh the provider table (a brand-new draft may have just been
        // persisted).
        qc.invalidateQueries({ queryKey: ["admin", "models"] });
        qc.invalidateQueries({ queryKey: ["admin", "providers"] });
      }
      if (skipped.length > 0) {
        toast.warning(
          t("providers.modelsAddSkippedConflicts", {
            count: skipped.length,
            defaultValue:
              "Skipped {{count}} model(s): alias already routed to another provider",
          }),
        );
      }
    },
    onError: (err) => {
      toast.error(
        t("providers.modelsAddFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
    onSettled: (_data, _err, ids) => {
      setPendingAdds((prev) => {
        const next = new Set(prev);
        for (const id of ids ?? []) next.delete(id);
        return next;
      });
    },
  });

  async function copyModelId(id: string) {
    try {
      await navigator.clipboard.writeText(id);
      toast.success(t("providers.modelsCopied"));
    } catch (err) {
      toast.error(
        t("providers.modelsCopyFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    }
  }

  // A valid provider identity is required before a model can be bound to it
  // as an alias (name + — for openai_compatible — a base_url, and no param
  // errors — mirrors the save-button gating), AND the draft must be enabled:
  // the runtime registry never builds a disabled provider, so its aliases
  // would not resolve.
  const addModelsGate = computeAddModelsGate({
    nameOk,
    baseUrlOk,
    hasErrors,
    enabled: draft.enabled,
  });
  const canAddModels = addModelsGate.canAdd;
  const addModelsBlockedTitle = canAddModels
    ? undefined
    : addModelsGate.reason === "disabled"
      ? t("providers.modelsAddNeedsEnabled", {
          defaultValue:
            "Enable the provider first — a disabled provider's models cannot be routed",
        })
      : t("providers.modelsAddNeedsName");
  const remainingModelIds = modelDiscovery.models
    .map((m) => m.id)
    .filter((id) => !addedModels.has(id) && !pendingAdds.has(id));

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <motion.div
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.18, ease: "easeOut" }}
        >
          <DialogHeader>
            <DialogTitle>
              {editing
                ? t("providers.modalEditTitle", { name: editing.name })
                : t("providers.modalAddTitle")}
            </DialogTitle>
            <DialogDescription>
              {t("providers.modalDesc")}
            </DialogDescription>
          </DialogHeader>

          <div className="max-h-[60vh] space-y-4 overflow-y-auto pr-1">
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="provider-name" className="text-xs">
                  {t("providers.fieldName")}
                </Label>
                <Input
                  id="provider-name"
                  value={draft.name}
                  disabled={!!editing}
                  onChange={(e) =>
                    updateDraft({ name: e.target.value })
                  }
                  className="font-mono text-xs"
                  placeholder="my-local-llm"
                />
                <p className="text-[11px] text-sg-ink-3">
                  {t("providers.fieldNameHint")}
                </p>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="provider-kind" className="text-xs">
                  {t("providers.fieldKind")}
                </Label>
                <select
                  id="provider-kind"
                  value={draft.kind}
                  onChange={(e) => {
                    const kind = e.target.value as ProviderKind;
                    const patch: Partial<DraftProvider> = { kind };
                    // Follow the kind with its canonical env var unless the
                    // operator typed a custom one (empty or still equal to
                    // the previous kind's suggestion → safe to replace).
                    const current = draft.api_key_env_name.trim();
                    if (
                      current === "" ||
                      current === suggestedEnvVarForKind(draft.kind)
                    ) {
                      patch.api_key_env_name = suggestedEnvVarForKind(kind);
                    }
                    updateDraft(patch);
                  }}
                  className="flex h-9 w-full rounded-md border border-input bg-transparent px-2 text-sm"
                >
                  {KINDS.map((k) => (
                    <option key={k} value={k}>
                      {k}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            {showBaseUrl ? (
              <div className="space-y-1.5">
                <Label htmlFor="provider-base-url" className="text-xs">
                  {t("providers.fieldBaseUrl")}
                </Label>
                <Input
                  id="provider-base-url"
                  value={draft.base_url}
                  onChange={(e) =>
                    updateDraft({ base_url: e.target.value })
                  }
                  className="font-mono text-xs"
                  placeholder="https://api.openai.com/v1"
                />
                <p className="text-[11px] text-sg-ink-3">
                  {t("providers.fieldBaseUrlHint")}
                </p>
              </div>
            ) : null}

            <div className="space-y-1.5">
              <Label className="text-xs">
                {t("providers.fieldApiKeySource")}
              </Label>
              <div className="flex gap-2">
                {(["env", "value", "unset"] as KeySource[]).map((src) => (
                  <button
                    key={src}
                    type="button"
                    onClick={() =>
                      updateDraft({ api_key_source: src })
                    }
                    className={cn(
                      "flex-1 rounded-md border px-3 py-1.5 text-xs transition-colors",
                      draft.api_key_source === src
                        ? "border-primary bg-sg-accent-soft text-sg-ink"
                        : "border-sg-border bg-transparent text-sg-ink-3 hover:bg-sg-inset-hover",
                    )}
                  >
                    {src === "env"
                      ? t("providers.fieldApiKeyEnv")
                      : src === "value"
                        ? t("providers.fieldApiKeyValue")
                        : t("providers.fieldApiKeyNone")}
                  </button>
                ))}
              </div>
              {draft.api_key_source === "env" ? (
                <Input
                  value={draft.api_key_env_name}
                  onChange={(e) =>
                    updateDraft({ api_key_env_name: e.target.value })
                  }
                  placeholder={t("providers.fieldApiKeyEnvPlaceholder")}
                  className="font-mono text-xs"
                />
              ) : null}
              {draft.api_key_source === "value" ? (
                <Input
                  type="password"
                  value={draft.api_key_value}
                  onChange={(e) =>
                    updateDraft({ api_key_value: e.target.value })
                  }
                  placeholder={t("providers.fieldApiKeyValuePlaceholder")}
                  className="font-mono text-xs"
                />
              ) : null}
            </div>

            <div className="space-y-2 rounded-md border border-sg-border p-3">
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0 space-y-0.5">
                  <h3 className="text-sm font-semibold">
                    {t("providers.modelsTitle")}
                  </h3>
                  <p className="text-[11px] text-sg-ink-3">
                    {t("providers.modelsHintAdd")}
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  {modelDiscovery.models.length > 0 ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() =>
                        addModelsMutation.mutate(remainingModelIds)
                      }
                      disabled={
                        !canAddModels ||
                        remainingModelIds.length === 0 ||
                        addModelsMutation.isPending
                      }
                      title={addModelsBlockedTitle}
                      data-testid="provider-add-all-models-btn"
                    >
                      {addModelsMutation.isPending ? (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      ) : (
                        <PlusCircle className="h-3 w-3" />
                      )}
                      {t("providers.modelsAddAll")}
                    </Button>
                  ) : null}
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() =>
                      modelDiscoveryMutation.mutate({
                        generation: modelDiscoveryGeneration.current,
                        draft: effectiveDraft,
                        editing,
                      })
                    }
                    disabled={
                      !baseUrlOk || hasErrors || modelDiscoveryMutation.isPending
                    }
                    data-testid="provider-fetch-models-btn"
                  >
                    {modelDiscoveryMutation.isPending ? (
                      <Loader2 className="h-3 w-3 animate-spin" />
                    ) : (
                      <RefreshCw className="h-3 w-3" />
                    )}
                    {modelDiscoveryMutation.isPending
                      ? t("providers.modelsFetching")
                      : t("providers.modelsFetch")}
                  </Button>
                </div>
              </div>
              {modelDiscovery.error ? (
                <p
                  className="text-[11px] text-sg-err"
                  data-testid="provider-models-error"
                >
                  {modelDiscovery.error}
                </p>
              ) : null}
              {modelDiscovery.models.length > 0 ? (
                <div
                  className="grid max-h-40 gap-1 overflow-y-auto pr-1"
                  data-testid="provider-models-list"
                >
                  {modelDiscovery.models.map((m) => {
                    const added = addedModels.has(m.id);
                    const pending = pendingAdds.has(m.id);
                    return (
                      <div
                        key={m.id}
                        className="flex min-h-9 items-center justify-between gap-2 rounded-md border border-sg-border bg-sg-inset px-2"
                        data-testid={`provider-model-row-${m.id}`}
                      >
                        <span
                          className="min-w-0 truncate font-mono text-[11px]"
                          title={m.id}
                        >
                          {m.id}
                        </span>
                        <div className="flex shrink-0 items-center gap-1">
                          {added ? (
                            <span
                              className="inline-flex items-center gap-1 px-1.5 text-[10px] text-sg-ok"
                              aria-label={t("providers.modelsAddedAria", {
                                id: m.id,
                              })}
                              data-testid={`provider-model-added-${m.id}`}
                            >
                              <Check className="h-3 w-3" />
                              {t("providers.modelsAdded")}
                            </span>
                          ) : (
                            <Button
                              type="button"
                              variant="ghost"
                              size="sm"
                              className="h-7 px-2"
                              aria-label={t("providers.modelsAddAria", {
                                id: m.id,
                              })}
                              title={addModelsBlockedTitle}
                              disabled={
                                !canAddModels ||
                                pending ||
                                addModelsMutation.isPending
                              }
                              onClick={() => addModelsMutation.mutate([m.id])}
                              data-testid={`provider-model-add-${m.id}`}
                            >
                              {pending ? (
                                <Loader2 className="h-3 w-3 animate-spin" />
                              ) : (
                                <Plus className="h-3 w-3" />
                              )}
                            </Button>
                          )}
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            className="h-7 px-2"
                            aria-label={t("providers.modelsCopyAria", {
                              id: m.id,
                            })}
                            onClick={() => copyModelId(m.id)}
                          >
                            <Copy className="h-3 w-3" />
                          </Button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <p className="text-[11px] text-sg-ink-3">
                  {t("providers.modelsEmpty")}
                </p>
              )}
            </div>

            <div className="flex items-center gap-3">
              <Label htmlFor="provider-enabled" className="text-xs">
                {t("providers.fieldEnabled")}
              </Label>
              <button
                id="provider-enabled"
                type="button"
                role="switch"
                aria-checked={draft.enabled}
                onClick={() =>
                  updateDraft({ enabled: !draft.enabled })
                }
                className={cn(
                  "inline-flex h-6 w-11 items-center rounded-full border border-input transition-colors",
                  draft.enabled
                    ? "bg-[color-mix(in_oklch,var(--sg-accent)_34%,transparent)]"
                    : "bg-sg-inset",
                )}
              >
                <span
                  className={cn(
                    "inline-block h-4 w-4 transform rounded-full border border-sg-border-strong bg-[color-mix(in_oklch,var(--sg-ink)_18%,transparent)] shadow-sg-2 transition-transform",
                    draft.enabled
                      ? "translate-x-[22px]"
                      : "translate-x-[3px]",
                  )}
                />
              </button>
            </div>

            <div>
              <button
                type="button"
                onClick={() => setShowAdvanced((v) => !v)}
                data-testid="provider-toggle-advanced"
                className="text-[11px] text-sg-ink-3 underline-offset-2 hover:text-sg-ink hover:underline focus-visible:outline-none focus-visible:underline"
              >
                {showAdvanced ? "—" : "+"}{" "}
                {t("common.advancedOptions", { defaultValue: "高级选项" })}
              </button>
            </div>

            {showAdvanced ? (
              <div className="space-y-2 rounded-md border border-sg-border p-3">
                <div>
                  <h3 className="text-sm font-semibold">
                    {t("providers.fieldParams")}
                  </h3>
                  <p className="text-[11px] text-sg-ink-3">
                    {t("providers.fieldParamsHint")}
                  </p>
                </div>
                <DynamicParamsForm
                  schema={schema}
                  value={draft.params}
                  onChange={(next) => updateDraft({ params: next })}
                  testIdPrefix="provider-params"
                />
              </div>
            ) : null}
          </div>

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={saveMutation.isPending}
            >
              {t("common.cancel")}
            </Button>
            <Button
              onClick={() => saveMutation.mutate()}
              disabled={
                !nameOk ||
                !baseUrlOk ||
                hasErrors ||
                saveMutation.isPending
              }
              data-testid="providers-save-btn"
            >
              {saveMutation.isPending
                ? t("providers.savingLabel")
                : t("providers.saveLabel")}
            </Button>
          </DialogFooter>
        </motion.div>
      </DialogContent>
    </Dialog>
  );
}
