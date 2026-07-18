"use client";

/**
 * ProviderSetupFlow — guided "zero → usable chat model" wizard (PR5).
 *
 * Five linear steps with progressive disclosure, narrow enough for a
 * max-w-md dialog / the onboarding column:
 *
 *   1. Choose provider   — preset grid (`./presets.ts`), OAuth-capable
 *                          presets badged.
 *   2. Authenticate      — API-key input by default (env-var disclosure);
 *                          OAuth presets can hand off to the existing
 *                          `OAuthLoginModal` PKCE surface, which provisions
 *                          provider + aliases + default server-side and
 *                          therefore SKIPS straight to step 5.
 *   3. Test & fetch      — one `probeProviderModels` call (works pre-save)
 *                          doubling as the connection test.
 *   4. Pick models       — multi-select; confirm persists the provider then
 *                          per-model aliases through the SAME guardrails as
 *                          the provider editor dialog (alias-helpers:
 *                          conflict partition + add gate).
 *   5. Set default       — radio over the just-added aliases, saved via
 *                          `setDefaultModel` (`{default}`-only body — never
 *                          a bulk alias write, which would drop omitted
 *                          alias names).
 *
 * Hosts: /models (empty state + quick-setup dialog), onboarding step 1,
 * dashboard getting-started card.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import {
  ArrowRight,
  Check,
  ChevronLeft,
  Loader2,
  LogIn,
  RefreshCw,
} from "@/components/icons";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { OAuthLoginModal } from "@/components/admin/oauth-login-modal";
import {
  fetchModels,
  probeProviderModels,
  setDefaultModel,
  upsertAlias,
  upsertProvider,
  type ModelsResponse,
  type ProviderModel,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  BLANK_DRAFT,
  computeAddModelsGate,
  extractAliasBindings,
  partitionAliasCandidates,
  toModelProbe,
  toUpsert,
  type DraftProvider,
} from "./alias-helpers";
import {
  PRESET_OAUTH_PROVIDER,
  SETUP_PRESETS,
  type SetupPreset,
} from "./presets";

export interface SetupFlowStatus {
  providerRegistered: boolean;
  testPassed: boolean;
  modelsAdded: boolean;
  defaultSet: boolean;
  /** Name of the provider the flow registered (when known). */
  providerName?: string;
}

export interface ProviderSetupFlowProps {
  variant?: "page" | "onboarding" | "dialog";
  onStatusChange?: (status: SetupFlowStatus) => void;
  onComplete?: () => void;
}

type StepId = 1 | 2 | 3 | 4 | 5;

const INITIAL_STATUS: SetupFlowStatus = {
  providerRegistered: false,
  testPassed: false,
  modelsAdded: false,
  defaultSet: false,
};

export function ProviderSetupFlow({
  variant = "page",
  onStatusChange,
  onComplete,
}: ProviderSetupFlowProps) {
  const { t } = useTranslation();
  const qc = useQueryClient();

  const [step, setStep] = React.useState<StepId>(1);
  const [preset, setPreset] = React.useState<SetupPreset | null>(null);
  const [draft, setDraft] = React.useState<DraftProvider>({
    ...BLANK_DRAFT,
    api_key_source: "value",
  });
  const [probe, setProbe] = React.useState<{
    models: ProviderModel[];
    error?: string;
  }>({ models: [] });
  const [selected, setSelected] = React.useState<Set<string>>(
    () => new Set(),
  );
  const [addedAliases, setAddedAliases] = React.useState<string[]>([]);
  const [defaultChoice, setDefaultChoice] = React.useState<string>("");
  const [oauthOpen, setOauthOpen] = React.useState(false);
  const [oauthDone, setOauthDone] = React.useState(false);
  const [done, setDone] = React.useState(false);

  // ── status reporting ──────────────────────────────────────────────
  const statusRef = React.useRef<SetupFlowStatus>(INITIAL_STATUS);
  const onStatusChangeRef = React.useRef(onStatusChange);
  onStatusChangeRef.current = onStatusChange;
  const updateStatus = React.useCallback(
    (patch: Partial<SetupFlowStatus>) => {
      const next = { ...statusRef.current, ...patch };
      statusRef.current = next;
      onStatusChangeRef.current?.(next);
    },
    [],
  );

  // Mirrors the provider editor: once the [providers.<name>] block is
  // persisted we never re-upsert it; ANY draft edit invalidates it.
  const providerPersistedRef = React.useRef(false);

  const updateDraft = React.useCallback((patch: Partial<DraftProvider>) => {
    setDraft((prev) => ({ ...prev, ...patch }));
    providerPersistedRef.current = false;
    setProbe({ models: [] });
  }, []);

  // Shared cache with the Models page / provider editor — used for the
  // alias-conflict guardrail and the step-5 option list.
  const modelsQuery = useQuery<ModelsResponse>({
    queryKey: ["admin", "models"],
    queryFn: fetchModels,
    retry: false,
  });

  // ── step 1 → 2 ────────────────────────────────────────────────────
  const choosePreset = React.useCallback(
    (p: SetupPreset) => {
      setPreset(p);
      setDraft({
        ...BLANK_DRAFT,
        name: p.id === "custom" ? "" : p.id,
        kind: p.kind,
        base_url: p.defaultBaseUrl ?? "",
        api_key_source: "value",
        api_key_env_name: p.suggestedEnvVar,
      });
      providerPersistedRef.current = false;
      setProbe({ models: [] });
      setSelected(new Set());
      setStep(2);
    },
    [],
  );

  const nameOk = draft.name.trim().length > 0;
  const baseUrlOk =
    draft.kind !== "openai_compatible" || draft.base_url.trim().length > 0;

  // ── step 3: probe (doubles as test-connection) ────────────────────
  const probeMutation = useMutation({
    mutationFn: () => probeProviderModels(toModelProbe(draft, null)),
    onSuccess: (res) => {
      if (res.error) {
        setProbe({ models: [], error: res.error });
        updateStatus({ testPassed: false });
        return;
      }
      const models = res.models ?? [];
      if (models.length === 0) {
        setProbe({ models: [], error: t("setupFlow.probeEmpty") });
        updateStatus({ testPassed: false });
        return;
      }
      setProbe({ models });
      setSelected(new Set());
      updateStatus({ testPassed: true });
      setStep(4);
    },
    onError: (err) => {
      setProbe({
        models: [],
        error: err instanceof Error ? err.message : String(err),
      });
      updateStatus({ testPassed: false });
    },
  });

  // ── step 4: persist provider + aliases (same rails as the editor) ──
  const addModelsMutation = useMutation({
    mutationFn: async (ids: string[]) => {
      const providerName = draft.name.trim();
      if (!providerName) {
        throw new Error(t("providers.modelsAddNeedsName"));
      }
      // An alias with the same model id may already route to a DIFFERENT
      // provider; overwriting it would silently reroute every chat using
      // it — skip those, upsert only the safe ids.
      const { safe, conflicting } = partitionAliasCandidates(
        ids,
        extractAliasBindings(modelsQuery.data),
        providerName,
      );
      if (safe.length > 0) {
        if (!providerPersistedRef.current) {
          await upsertProvider(toUpsert(draft));
          providerPersistedRef.current = true;
        }
        for (const id of safe) {
          await upsertAlias({ name: id, provider: providerName, model: id });
        }
      }
      return { added: safe, skipped: conflicting };
    },
    onSuccess: ({ added, skipped }) => {
      if (skipped.length > 0) {
        toast.warning(
          t("providers.modelsAddSkippedConflicts", {
            count: skipped.length,
            defaultValue:
              "Skipped {{count}} model(s): alias already routed to another provider",
          }),
        );
      }
      if (added.length > 0) {
        setAddedAliases(added);
        qc.invalidateQueries({ queryKey: ["admin", "models"] });
        qc.invalidateQueries({ queryKey: ["admin", "providers"] });
        updateStatus({
          providerRegistered: true,
          modelsAdded: true,
          providerName: draft.name.trim(),
        });
        const current = (modelsQuery.data?.default ?? "").trim();
        setDefaultChoice(
          current && added.includes(current) ? current : added[0]!,
        );
        setStep(5);
      }
    },
    onError: (err) => {
      toast.error(
        t("providers.modelsAddFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  const addGate = computeAddModelsGate({
    nameOk,
    baseUrlOk,
    hasErrors: false,
    enabled: draft.enabled,
  });

  // ── step 5: default (NEVER a bulk alias write) ────────────────────
  const defaultMutation = useMutation({
    mutationFn: (model: string) => setDefaultModel(model),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin", "models"] });
      updateStatus({ defaultSet: true });
      setDone(true);
    },
    onError: (err) => {
      toast.error(
        t("setupFlow.defaultSaveFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  // ── OAuth hand-off — backend provisions provider + aliases + default
  const oauthProvider = preset?.oauth
    ? PRESET_OAUTH_PROVIDER[preset.id]
    : undefined;
  const handleOauthSuccess = React.useCallback(() => {
    setOauthDone(true);
    qc.invalidateQueries({ queryKey: ["admin", "oauth", "status"] });
    qc.invalidateQueries({ queryKey: ["admin", "providers"] });
    qc.invalidateQueries({ queryKey: ["admin", "models"] });
    updateStatus({
      providerRegistered: true,
      testPassed: true,
      modelsAdded: true,
      providerName: oauthProvider,
    });
    setStep(5);
  }, [qc, updateStatus, oauthProvider]);

  // Step-5 option list: key path → the aliases this session just added;
  // OAuth path → everything the backend provisioned (refetched via the
  // invalidation above). The current default is always offered.
  const currentDefault = (modelsQuery.data?.default ?? "").trim();
  const defaultOptions = React.useMemo(() => {
    const names = new Set<string>();
    if (oauthDone) {
      for (const a of extractAliasBindings(modelsQuery.data)) {
        if (a.name !== "mock" && a.provider !== "mock") names.add(a.name);
      }
    } else {
      for (const id of addedAliases) names.add(id);
    }
    if (currentDefault && currentDefault !== "mock") names.add(currentDefault);
    return [...names];
  }, [oauthDone, modelsQuery.data, addedAliases, currentDefault]);

  // OAuth path lands on step 5 with no local choice yet — preselect the
  // server-set default once it arrives, and reflect it in the status (the
  // backend already picked one, so the deployment is chat-ready).
  React.useEffect(() => {
    if (step !== 5 || !oauthDone) return;
    if (currentDefault && currentDefault !== "mock") {
      setDefaultChoice((prev) => prev || currentDefault);
      if (!statusRef.current.defaultSet) updateStatus({ defaultSet: true });
    } else if (!defaultChoice && defaultOptions.length > 0) {
      setDefaultChoice(defaultOptions[0]!);
    }
  }, [
    step,
    oauthDone,
    currentDefault,
    defaultChoice,
    defaultOptions,
    updateStatus,
  ]);

  const selectedIds = React.useMemo(
    () => probe.models.map((m) => m.id).filter((id) => selected.has(id)),
    [probe.models, selected],
  );
  const allSelected =
    probe.models.length > 0 && selectedIds.length === probe.models.length;

  const stepTitles: Record<StepId, string> = {
    1: t("setupFlow.step1Title"),
    2: t("setupFlow.step2Title"),
    3: t("setupFlow.step3Title"),
    4: t("setupFlow.step4Title"),
    5: t("setupFlow.step5Title"),
  };

  // Step 5 normally has no back (the key path already committed the
  // provider + aliases). But the OAuth path jumps straight to step 5 and
  // can land there with an empty option list if provisioning yielded
  // nothing — without an escape the save button is permanently disabled
  // and the only way out is closing the dialog (self-review P3). Allow
  // back on the OAuth step 5 to retreat to the preset choice.
  const canGoBack =
    !done && step > 1 && (step !== 5 || oauthDone);
  const handleBack = React.useCallback(() => {
    if (step === 5 && oauthDone) {
      // OAuth session is orphaned on retreat — reset so a fresh attempt
      // (or a different preset) starts clean.
      setOauthDone(false);
      updateStatus({
        providerRegistered: false,
        testPassed: false,
        modelsAdded: false,
        defaultSet: false,
      });
      setStep(1);
      return;
    }
    setStep((s) => Math.max(1, s - 1) as StepId);
  }, [step, oauthDone, updateStatus]);

  return (
    <div
      className={cn(
        "flex w-full flex-col gap-4",
        variant === "onboarding" ? "" : "max-w-md",
      )}
      data-testid="provider-setup-flow"
      data-step={step}
      data-variant={variant}
    >
      {/* header: dots + title (+ back) */}
      <div className="flex items-center gap-2">
        {canGoBack ? (
          <button
            type="button"
            onClick={handleBack}
            className="inline-flex h-6 w-6 items-center justify-center rounded-md text-sg-ink-3 transition-colors hover:bg-sg-inset-hover hover:text-sg-ink"
            aria-label={t("setupFlow.back")}
            data-testid="setup-back"
          >
            <ChevronLeft className="h-4 w-4" aria-hidden />
          </button>
        ) : null}
        <div className="flex items-center gap-1" aria-hidden>
          {([1, 2, 3, 4, 5] as const).map((n) => (
            <span
              key={n}
              className={cn(
                "h-1.5 w-1.5 rounded-full transition-colors",
                n <= step ? "bg-sg-accent" : "bg-sg-border",
              )}
            />
          ))}
        </div>
        <span className="text-xs font-medium text-sg-ink-2">
          {stepTitles[step]}
        </span>
      </div>

      {/* ── step 1: preset grid ─────────────────────────────────── */}
      {step === 1 ? (
        <div className="grid grid-cols-2 gap-2" data-testid="setup-presets">
          {SETUP_PRESETS.map((p) => (
            <button
              key={p.id}
              type="button"
              onClick={() => choosePreset(p)}
              className="flex flex-col items-start gap-1 rounded-md border border-sg-border bg-sg-card p-3 text-left text-sm transition-colors hover:border-sg-accent/40 hover:bg-sg-inset-hover"
              data-testid={`setup-preset-${p.id}`}
            >
              <span className="font-medium">{t(p.labelKey)}</span>
              {p.oauth ? (
                <span className="rounded-full bg-sg-ok-soft px-1.5 py-0.5 text-[10px] font-medium text-sg-ok">
                  {t("setupFlow.oauthBadge")}
                </span>
              ) : (
                <span className="text-[10px] text-sg-ink-4">
                  {t("setupFlow.keyBadge")}
                </span>
              )}
            </button>
          ))}
        </div>
      ) : null}

      {/* ── step 2: authenticate ────────────────────────────────── */}
      {step === 2 && preset ? (
        <div className="flex flex-col gap-3">
          {oauthProvider ? (
            <div className="flex flex-col gap-2 rounded-md border border-sg-border bg-sg-inset p-3">
              <p className="text-xs text-sg-ink-3">
                {t("setupFlow.oauthHint")}
              </p>
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="self-start"
                onClick={() => setOauthOpen(true)}
                data-testid="setup-oauth-btn"
              >
                <LogIn className="h-3.5 w-3.5" aria-hidden />
                {t("setupFlow.oauthLogin")}
              </Button>
            </div>
          ) : null}

          <div className="space-y-1.5">
            <Label htmlFor="setup-name" className="text-xs">
              {t("setupFlow.fieldName")}
            </Label>
            <Input
              id="setup-name"
              value={draft.name}
              onChange={(e) => updateDraft({ name: e.target.value })}
              className="font-mono text-xs"
              placeholder={preset.id === "custom" ? "my-relay" : preset.id}
              data-testid="setup-name-input"
            />
          </div>

          {draft.kind === "openai_compatible" ? (
            <div className="space-y-1.5">
              <Label htmlFor="setup-base-url" className="text-xs">
                {t("setupFlow.fieldBaseUrl")}
              </Label>
              <Input
                id="setup-base-url"
                value={draft.base_url}
                onChange={(e) => updateDraft({ base_url: e.target.value })}
                className="font-mono text-xs"
                placeholder="https://api.example.com/v1"
                data-testid="setup-base-url-input"
              />
            </div>
          ) : null}

          {draft.api_key_source === "value" ? (
            <div className="space-y-1.5">
              <Label htmlFor="setup-key" className="text-xs">
                {t("setupFlow.fieldKey")}
              </Label>
              <Input
                id="setup-key"
                type="password"
                value={draft.api_key_value}
                onChange={(e) =>
                  updateDraft({ api_key_value: e.target.value })
                }
                className="font-mono text-xs"
                placeholder={t("setupFlow.fieldKeyPlaceholder")}
                autoComplete="off"
                data-testid="setup-key-input"
              />
            </div>
          ) : (
            <div className="space-y-1.5">
              <Label htmlFor="setup-env" className="text-xs">
                {t("setupFlow.fieldEnv")}
              </Label>
              <Input
                id="setup-env"
                value={draft.api_key_env_name}
                onChange={(e) =>
                  updateDraft({ api_key_env_name: e.target.value })
                }
                className="font-mono text-xs"
                placeholder={preset.suggestedEnvVar}
                data-testid="setup-env-input"
              />
            </div>
          )}
          <button
            type="button"
            onClick={() =>
              updateDraft({
                api_key_source:
                  draft.api_key_source === "value" ? "env" : "value",
                api_key_env_name: draft.api_key_env_name.trim()
                  ? draft.api_key_env_name
                  : preset.suggestedEnvVar,
              })
            }
            className="self-start text-[11px] text-sg-ink-3 underline-offset-2 hover:text-sg-ink hover:underline"
            data-testid="setup-env-toggle"
          >
            {draft.api_key_source === "value"
              ? t("setupFlow.useEnvVar")
              : t("setupFlow.useLiteralKey")}
          </button>

          <Button
            type="button"
            onClick={() => setStep(3)}
            disabled={!nameOk || !baseUrlOk}
            data-testid="setup-auth-next"
          >
            {t("setupFlow.next")}
            <ArrowRight className="h-4 w-4" aria-hidden />
          </Button>
        </div>
      ) : null}

      {/* ── step 3: test & fetch models ─────────────────────────── */}
      {step === 3 ? (
        <div className="flex flex-col gap-3">
          <p className="text-xs text-sg-ink-3">{t("setupFlow.probeHint")}</p>
          <Button
            type="button"
            onClick={() => probeMutation.mutate()}
            disabled={probeMutation.isPending}
            data-testid="setup-probe-btn"
          >
            {probeMutation.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            ) : (
              <RefreshCw className="h-4 w-4" aria-hidden />
            )}
            {probeMutation.isPending
              ? t("setupFlow.probing")
              : t("setupFlow.probeBtn")}
          </Button>
          {probe.error ? (
            <div
              className="space-y-1 rounded-md border border-sg-err/30 bg-sg-err-soft p-2"
              data-testid="setup-probe-error"
              role="alert"
            >
              <p className="text-xs text-sg-err">{probe.error}</p>
              <p className="text-[11px] text-sg-ink-3">
                {t("setupFlow.probeFailedHint")}
              </p>
            </div>
          ) : null}
        </div>
      ) : null}

      {/* ── step 4: pick models ─────────────────────────────────── */}
      {step === 4 ? (
        <div className="flex flex-col gap-3">
          <label className="flex items-center gap-2 text-xs text-sg-ink-2">
            <input
              type="checkbox"
              checked={allSelected}
              onChange={(e) =>
                setSelected(
                  e.target.checked
                    ? new Set(probe.models.map((m) => m.id))
                    : new Set(),
                )
              }
              data-testid="setup-select-all"
            />
            {t("setupFlow.selectAll")}
            <span className="text-sg-ink-4">
              {t("setupFlow.selectedCount", { n: selectedIds.length })}
            </span>
          </label>
          <div
            className="grid max-h-52 gap-1 overflow-y-auto pr-1"
            data-testid="setup-model-list"
          >
            {probe.models.map((m) => (
              <label
                key={m.id}
                className="flex min-h-8 cursor-pointer items-center gap-2 rounded-md border border-sg-border bg-sg-inset px-2 text-[11px]"
              >
                <input
                  type="checkbox"
                  checked={selected.has(m.id)}
                  onChange={(e) =>
                    setSelected((prev) => {
                      const next = new Set(prev);
                      if (e.target.checked) next.add(m.id);
                      else next.delete(m.id);
                      return next;
                    })
                  }
                  data-testid={`setup-model-checkbox-${m.id}`}
                />
                <span className="min-w-0 truncate font-mono" title={m.id}>
                  {m.id}
                </span>
              </label>
            ))}
          </div>
          <Button
            type="button"
            onClick={() => addModelsMutation.mutate(selectedIds)}
            disabled={
              !addGate.canAdd ||
              selectedIds.length === 0 ||
              addModelsMutation.isPending
            }
            title={
              addGate.canAdd ? undefined : t("providers.modelsAddNeedsName")
            }
            data-testid="setup-add-models-btn"
          >
            {addModelsMutation.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            ) : null}
            {addModelsMutation.isPending
              ? t("setupFlow.adding")
              : t("setupFlow.addModels", { n: selectedIds.length })}
          </Button>
        </div>
      ) : null}

      {/* ── step 5: default ─────────────────────────────────────── */}
      {step === 5 ? (
        done ? (
          <div
            className="flex flex-col gap-3 rounded-md border border-sg-border bg-sg-inset p-4"
            data-testid="setup-done"
          >
            <div className="flex items-center gap-2 text-sm font-medium text-sg-ok">
              <Check className="h-4 w-4" aria-hidden />
              {t("setupFlow.doneTitle")}
            </div>
            <p className="text-xs text-sg-ink-3">
              {t("setupFlow.doneBody", {
                provider:
                  statusRef.current.providerName ?? draft.name.trim(),
                model: defaultChoice,
              })}
            </p>
            {onComplete ? (
              <Button
                type="button"
                size="sm"
                className="self-start"
                onClick={() => onComplete()}
                data-testid="setup-finish"
              >
                {t("setupFlow.finish")}
              </Button>
            ) : null}
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {oauthDone ? (
              <p className="text-xs text-sg-ok" data-testid="setup-oauth-notice">
                {t("setupFlow.oauthProvisioned")}
              </p>
            ) : null}
            <p className="text-xs text-sg-ink-3">
              {t("setupFlow.defaultHint")}
            </p>
            {defaultOptions.length === 0 ? (
              <p className="text-xs text-sg-ink-4" data-testid="setup-no-aliases">
                {t("setupFlow.noAliases")}
              </p>
            ) : (
              <div
                className="grid max-h-52 gap-1 overflow-y-auto pr-1"
                role="radiogroup"
                aria-label={t("setupFlow.step5Title")}
              >
                {defaultOptions.map((name) => (
                  <label
                    key={name}
                    className="flex min-h-8 cursor-pointer items-center gap-2 rounded-md border border-sg-border bg-sg-inset px-2 text-[11px]"
                  >
                    <input
                      type="radio"
                      name="setup-default"
                      checked={defaultChoice === name}
                      onChange={() => setDefaultChoice(name)}
                      data-testid={`setup-default-radio-${name}`}
                    />
                    <span className="min-w-0 truncate font-mono" title={name}>
                      {name}
                    </span>
                    {name === currentDefault ? (
                      <span className="ml-auto shrink-0 text-[10px] text-sg-ink-4">
                        {t("setupFlow.currentDefault")}
                      </span>
                    ) : null}
                  </label>
                ))}
              </div>
            )}
            <Button
              type="button"
              onClick={() => defaultMutation.mutate(defaultChoice)}
              disabled={!defaultChoice || defaultMutation.isPending}
              data-testid="setup-save-default-btn"
            >
              {defaultMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
              ) : null}
              {defaultMutation.isPending
                ? t("setupFlow.saving")
                : t("setupFlow.saveDefault")}
            </Button>
          </div>
        )
      ) : null}

      {oauthProvider ? (
        <OAuthLoginModal
          open={oauthOpen}
          provider={oauthProvider}
          onOpenChange={setOauthOpen}
          onSuccess={handleOauthSuccess}
        />
      ) : null}
    </div>
  );
}
