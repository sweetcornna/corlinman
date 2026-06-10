"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import {
  Check,
  ChevronDown,
  ChevronRight,
  Key,
  Pencil,
  Plus,
  Trash2,
  X,
} from "lucide-react";
import { AnimatePresence, motion } from "framer-motion";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";
import {
  fetchModels,
  updateAliases,
  upsertAlias,
  type AliasView,
  type JSONSchema,
  type ModelsResponse,
  type ProviderRow,
  type ProviderView,
} from "@/lib/api";
import { DynamicParamsForm } from "@/components/dynamic-params-form";
import { ModelPickerDialog } from "@/components/models/model-picker-dialog";

/**
 * Models admin page.
 *
 * v0.1 behaviour preserved: `aliases: Record<string, alias-target>` edited
 * inline, saved via `POST /admin/models/aliases { aliases, default }`.
 *
 * v0.2 (Feature C) additions:
 *   - The gateway may return `aliases: AliasView[]` and `providers:
 *     ProviderView[]` (richer shape, with `params` + `params_schema`).
 *     When detected, the table renders a provider column + a row-expander
 *     that drops open the per-alias params editor driven by
 *     `<DynamicParamsForm>`.
 *   - The legacy save-all flow still fires on the top-right Save button;
 *     per-alias params save independently via `POST /admin/models/aliases`
 *     with the full `AliasUpsert` body.
 */
export default function ModelsPage() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  // `fetchModels` is typed against the v0.1 shape, but v0.2 gateways reply
  // with the extended shape in the same slot. Treat the response loosely
  // and branch on the observed fields.
  const models = useQuery<ModelsResponse>({
    queryKey: ["admin", "models"],
    queryFn: fetchModels,
  });

  const shape = React.useMemo(
    () => detectShape(models.data),
    [models.data],
  );

  const [aliases, setAliases] = React.useState<Array<[string, string]>>([]);
  const [defaultModel, setDefaultModel] = React.useState("");
  const [initialized, setInitialized] = React.useState(false);
  React.useEffect(() => {
    if (!models.data || initialized) return;
    if (shape === "v2") {
      const v2 = models.data as unknown as V2Models;
      setAliases(v2.aliases.map((a) => [a.name, a.model] as [string, string]));
      setDefaultModel(v2.default);
    } else {
      setAliases(Object.entries(models.data.aliases as Record<string, string>));
      setDefaultModel(models.data.default);
    }
    setInitialized(true);
  }, [models.data, initialized, shape]);

  const saveMutation = useMutation({
    mutationFn: () => {
      const map: Record<string, string> = {};
      for (const [k, v] of aliases) {
        if (k.trim() && v.trim()) map[k.trim()] = v.trim();
      }
      return updateAliases(map, defaultModel.trim() || undefined);
    },
    onSuccess: () => {
      toast.success(t("models.saveSuccess"));
      qc.invalidateQueries({ queryKey: ["admin", "models"] });
    },
    onError: (err) =>
      toast.error(
        t("models.saveFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      ),
  });

  // Derive provider-name → params_schema + view for the v2 path.
  const providersByName = React.useMemo(() => {
    if (!models.data) return new Map<string, ProviderView>();
    const arr = (models.data as unknown as V2Models).providers;
    if (!arr || arr.length === 0) return new Map();
    if (isV2Provider(arr[0]!)) {
      return new Map(arr.map((p) => [p.name, p]));
    }
    return new Map();
  }, [models.data]);

  const aliasViews = React.useMemo<AliasView[] | null>(() => {
    if (shape !== "v2" || !models.data) return null;
    return (models.data as unknown as V2Models).aliases;
  }, [shape, models.data]);

  /**
   * Picker state.
   *   - mode "add"   → on confirm, append a new local alias row prefilled
   *                    with the chosen target. The user names it inline.
   *                    (In v2 we also persist `provider` once the user
   *                    saves via the toolbar Save — but since v1 doesn't
   *                    model `provider`, we drop it; v2 upserts still work
   *                    because the gateway resolves provider from `model`
   *                    on Save when omitted, see /admin/models/aliases.)
   *   - mode "edit"  → on confirm, persist immediately via upsertAlias
   *                    (v2 path) or just patch the target in local state
   *                    (v1 path).
   */
  const [pickerOpen, setPickerOpen] = React.useState(false);
  const [pickerMode, setPickerMode] = React.useState<
    | { kind: "add" }
    | { kind: "edit"; idx: number; aliasName: string }
  >({ kind: "add" });

  const pickerProviders = React.useMemo(
    () =>
      (models.data?.providers ?? [])
        .filter((p) => p.enabled)
        .map((p) => ({
          name: p.name,
          kind: isV2Provider(p) ? p.kind : "",
          enabled: p.enabled,
        })),
    [models.data],
  );

  const editingView =
    pickerMode.kind === "edit" && aliasViews
      ? aliasViews.find((a) => a.name === pickerMode.aliasName)
      : undefined;

  const handlePickerConfirm = React.useCallback(
    (sel: { provider: string; model: string }) => {
      if (pickerMode.kind === "add") {
        // Append a new local row prefilled with the chosen target. Name +
        // (v2: provider) are still required to actually save.
        setAliases((prev) => [...prev, ["", sel.model]]);
        // If we're on the v2 path, immediately upsert so the provider is
        // recorded — otherwise the user has no surface to set provider
        // before the toolbar Save fires (which drops provider entirely).
        // Defer until they type the name; we just toast a hint.
        toast.info(
          t("models.pickerAddedHint", {
            defaultValue:
              "Row added — type an alias name then click Save.",
          }),
        );
        return;
      }
      // edit path
      const idx = pickerMode.idx;
      if (aliasViews && editingView) {
        // v2: persist immediately so provider+model stay aligned.
        upsertAlias({
          name: editingView.name,
          provider: sel.provider,
          model: sel.model,
          params: editingView.params ?? {},
        })
          .then(() => {
            toast.success(t("models.saveSuccess"));
            qc.invalidateQueries({ queryKey: ["admin", "models"] });
          })
          .catch((err: unknown) =>
            toast.error(
              t("models.saveFailed", {
                msg: err instanceof Error ? err.message : String(err),
              }),
            ),
          );
      } else {
        // v1: only target string is tracked; update local state.
        setAliases((prev) => {
          const next = [...prev];
          const current = next[idx];
          if (!current) return prev;
          next[idx] = [current[0], sel.model];
          return next;
        });
      }
    },
    [pickerMode, aliasViews, editingView, qc, t],
  );

  return (
    <>
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">
          {t("models.title")}
        </h1>
        <p className="text-sm text-tp-ink-3">{t("models.subtitle")}</p>
      </header>

      <section className="space-y-2">
        <h2 className="text-sm font-semibold">{t("models.providers")}</h2>
        {models.isPending ? (
          <Skeleton className="h-24 w-full" />
        ) : models.data && models.data.providers.length === 0 ? (
          <p className="rounded-md border border-dashed border-tp-glass-edge p-6 text-center text-sm text-tp-ink-3">
            {t("models.providersEmpty")}
          </p>
        ) : (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {models.data?.providers.map((p) => (
              <ProviderCard key={p.name} provider={p} />
            ))}
          </div>
        )}
      </section>

      <section className="space-y-3 rounded-lg border border-tp-glass-edge bg-tp-glass p-4">
        <div className="flex items-center justify-between gap-2">
          <div>
            <h2 className="text-sm font-semibold">{t("models.aliases")}</h2>
            <p className="text-xs text-tp-ink-3">
              {t("models.aliasesHint")}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-[10px] uppercase tracking-wider text-tp-ink-3">
              {t("models.defaultLabel")}
            </span>
            <Input
              value={defaultModel}
              onChange={(e) => setDefaultModel(e.target.value)}
              className="h-8 w-48 font-mono text-xs"
              placeholder="claude-sonnet-4-5"
            />
            <Button
              size="sm"
              variant="outline"
              onClick={() => setAliases([...aliases, ["", ""]])}
            >
              <Plus className="h-3 w-3" />
              {t("models.addAlias")}
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                setPickerMode({ kind: "add" });
                setPickerOpen(true);
              }}
              data-testid="models-pick-btn"
            >
              {t("models.picker.changeButton")}
            </Button>
            <Button
              size="sm"
              onClick={() => saveMutation.mutate()}
              disabled={saveMutation.isPending}
              data-testid="models-save-btn"
            >
              {saveMutation.isPending ? t("models.saving") : t("models.save")}
            </Button>
          </div>
        </div>
        <Table>
          <TableHeader>
            <TableRow className="border-b border-tp-glass-edge hover:bg-transparent">
              {aliasViews ? <TableHead className="w-8 pl-3"></TableHead> : null}
              <TableHead className={cn("w-52", !aliasViews && "pl-3")}>
                {t("models.aliasHeader")}
              </TableHead>
              {aliasViews ? (
                <TableHead className="w-40">
                  {t("models.aliasProviderHeader")}
                </TableHead>
              ) : null}
              <TableHead>{t("models.aliasTargetHeader")}</TableHead>
              <TableHead className="w-16"></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {aliases.length === 0 ? (
              <TableRow>
                <TableCell
                  colSpan={aliasViews ? 5 : 3}
                  className="py-6 text-center text-sm text-tp-ink-3"
                >
                  {t("models.noAliases")}
                </TableCell>
              </TableRow>
            ) : aliasViews ? (
              aliases.map(([alias, target], idx) => {
                const av = aliasViews.find((a) => a.name === alias);
                return (
                  <AliasRowV2
                    key={`${idx}-${alias}`}
                    alias={alias}
                    target={target}
                    view={av}
                    providersByName={providersByName}
                    onChange={(next) => {
                      const all = [...aliases];
                      all[idx] = next;
                      setAliases(all);
                    }}
                    onRemove={() =>
                      setAliases(aliases.filter((_, i) => i !== idx))
                    }
                    onPickModel={() => {
                      setPickerMode({
                        kind: "edit",
                        idx,
                        aliasName: alias,
                      });
                      setPickerOpen(true);
                    }}
                  />
                );
              })
            ) : (
              aliases.map(([alias, target], idx) => (
                <AliasRow
                  key={idx}
                  alias={alias}
                  target={target}
                  onChange={(next) => {
                    const all = [...aliases];
                    all[idx] = next;
                    setAliases(all);
                  }}
                  onRemove={() =>
                    setAliases(aliases.filter((_, i) => i !== idx))
                  }
                  onPickModel={() => {
                    setPickerMode({ kind: "edit", idx, aliasName: alias });
                    setPickerOpen(true);
                  }}
                />
              ))
            )}
          </TableBody>
        </Table>
        {saveMutation.isError ? (
          <p className="text-xs text-sg-err">
            {(saveMutation.error as Error).message}
          </p>
        ) : saveMutation.isSuccess ? (
          <p className="text-xs text-sg-ok">{t("models.aliasSavedInline")}</p>
        ) : null}
      </section>

      <ModelPickerDialog
        open={pickerOpen}
        onClose={() => setPickerOpen(false)}
        providers={pickerProviders}
        initialProvider={
          pickerMode.kind === "edit" ? editingView?.provider : undefined
        }
        initialModel={
          pickerMode.kind === "edit"
            ? editingView?.model ?? aliases[pickerMode.idx]?.[1]
            : undefined
        }
        onConfirm={handlePickerConfirm}
      />
    </>
  );
}

// --------------------------- shape detection ------------------------------

/** The v0.2 gateway returns `aliases: AliasView[]` + richer `providers`;
 * the v0.1 gateway returns `aliases: Record<string, string>`. Keep a single
 * state shape internally and branch on detection. */
type V2Models = {
  default: string;
  providers: ProviderView[];
  aliases: AliasView[];
};

function detectShape(data: ModelsResponse | undefined): "v1" | "v2" {
  if (!data) return "v1";
  const raw = data as unknown as { aliases: unknown };
  return Array.isArray(raw.aliases) ? "v2" : "v1";
}

function isV2Provider(p: ProviderRow | ProviderView): p is ProviderView {
  return (
    typeof (p as ProviderView).params_schema !== "undefined" ||
    typeof (p as ProviderView).api_key_source !== "undefined"
  );
}

// --------------------------- provider card --------------------------------

function ProviderCard({ provider }: { provider: ProviderRow | ProviderView }) {
  const { t } = useTranslation();
  const enabled = provider.enabled;
  const v2 = isV2Provider(provider) ? provider : null;
  const v1 = !v2 ? (provider as ProviderRow) : null;

  // The v2 "has_api_key" is inferred from api_key_source.
  const keyKindLabel = v2
    ? v2.api_key_source === "env"
      ? "env"
      : v2.api_key_source === "value"
        ? "literal"
        : null
    : v1?.has_api_key
      ? v1.api_key_kind
      : null;

  return (
    <div
      className={cn(
        "flex flex-col gap-2 rounded-lg border p-4 transition-colors",
        enabled
          ? "border-tp-glass-edge bg-tp-glass hover:border-tp-amber/35"
          : "border-tp-glass-edge bg-tp-glass-inner",
      )}
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span
            className={cn(
              "inline-block h-2 w-2 rounded-full",
              enabled ? "bg-sg-ok" : "bg-tp-ink-3/40",
            )}
          />
          <span className="text-sm font-semibold">{provider.name}</span>
          {v2 ? (
            <Badge variant="secondary" className="font-mono text-[10px]">
              {v2.kind}
            </Badge>
          ) : null}
        </div>
        {enabled ? (
          <Badge className="border-transparent bg-sg-ok-soft text-sg-ok">
            {t("common.enabled")}
          </Badge>
        ) : (
          <Badge variant="secondary">{t("common.disabled")}</Badge>
        )}
      </div>
      <div className="flex items-center gap-2 text-xs">
        <Key className="h-3 w-3 text-tp-ink-3" />
        {keyKindLabel ? (
          <span className="font-mono text-tp-ink-3">
            {t("models.keyKind", { kind: keyKindLabel })}
          </span>
        ) : (
          <span className="text-sg-err">
            {t("models.keyMissing")}
          </span>
        )}
      </div>
      <div className="font-mono text-[11px] text-tp-ink-3">
        {provider.base_url ?? t("models.providerDefault")}
      </div>
    </div>
  );
}

// --------------------------- alias rows -----------------------------------

function AliasRowV2({
  alias,
  target,
  view,
  providersByName,
  onChange,
  onRemove,
  onPickModel,
}: {
  alias: string;
  target: string;
  view: AliasView | undefined;
  providersByName: Map<string, ProviderView>;
  onChange: (next: [string, string]) => void;
  onRemove: () => void;
  onPickModel: () => void;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = React.useState(false);
  const providerName = view?.provider ?? "";
  const provider = providersByName.get(providerName);
  const schema: JSONSchema | null =
    view?.effective_params_schema ?? provider?.params_schema ?? null;

  return (
    <>
      <TableRow className="border-b border-tp-glass-edge">
        <TableCell className="w-8 pl-3">
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
            aria-label={expanded ? t("models.collapse") : t("models.expand")}
            className="inline-flex h-6 w-6 items-center justify-center rounded-md text-tp-ink-3 hover:bg-tp-glass-inner-hover hover:text-tp-ink"
            data-testid={`alias-expand-${alias}`}
          >
            {expanded ? (
              <ChevronDown className="h-3.5 w-3.5" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5" />
            )}
          </button>
        </TableCell>
        <TableCell>
          <InlineEdit
            value={alias}
            onCommit={(v) => onChange([v, target])}
            placeholder="smart"
            mono
          />
        </TableCell>
        <TableCell className="font-mono text-[11px] text-tp-ink-3">
          {providerName || "—"}
        </TableCell>
        <TableCell>
          <div className="flex items-center gap-1">
            <InlineEdit
              value={target}
              onCommit={(v) => onChange([alias, v])}
              placeholder="claude-opus-4-7"
              mono
            />
            <Button
              size="sm"
              variant="ghost"
              onClick={onPickModel}
              aria-label={t("models.picker.changeButton")}
              className="h-7 px-2 text-[10px]"
              data-testid={`alias-pick-${alias}`}
            >
              {t("models.picker.changeButton")}
            </Button>
          </div>
        </TableCell>
        <TableCell>
          <Button
            size="sm"
            variant="ghost"
            onClick={onRemove}
            aria-label={t("models.remove")}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </TableCell>
      </TableRow>
      <AnimatePresence initial={false}>
        {expanded ? (
          <TableRow
            className="border-b border-tp-glass-edge bg-tp-glass/40"
            data-testid={`alias-params-row-${alias}`}
          >
            <TableCell colSpan={5} className="p-0">
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.18, ease: "easeOut" }}
                className="overflow-hidden"
              >
                <div className="space-y-3 px-4 py-3">
                  {view ? (
                    <AliasParamsEditor
                      alias={view}
                      provider={provider}
                      schema={schema}
                    />
                  ) : (
                    <p className="text-xs text-tp-ink-3">
                      {t("models.paramsBackendPending")}
                    </p>
                  )}
                </div>
              </motion.div>
            </TableCell>
          </TableRow>
        ) : null}
      </AnimatePresence>
    </>
  );
}

function AliasParamsEditor({
  alias,
  provider,
  schema,
}: {
  alias: AliasView;
  provider: ProviderView | undefined;
  schema: JSONSchema | null;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [params, setParams] = React.useState<Record<string, unknown>>(
    alias.params ?? {},
  );
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const hasErrors = Object.keys(errors).length > 0;

  React.useEffect(() => {
    setParams(alias.params ?? {});
  }, [alias.params]);

  const saveMutation = useMutation({
    mutationFn: () =>
      upsertAlias({
        name: alias.name,
        provider: alias.provider,
        model: alias.model,
        params,
      }),
    onSuccess: () => {
      toast.success(t("models.paramsSaved"));
      qc.invalidateQueries({ queryKey: ["admin", "models"] });
    },
    onError: (err) =>
      toast.error(
        t("models.paramsSaveFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      ),
  });

  return (
    <div className="space-y-3">
      <div>
        <h3 className="text-sm font-semibold">{t("models.paramsTitle")}</h3>
        <p className="text-[11px] text-tp-ink-3">
          {t("models.paramsHint")}
        </p>
      </div>
      {schema ? (
        <DynamicParamsForm
          schema={schema}
          value={params}
          onChange={setParams}
          onErrorsChange={setErrors}
          testIdPrefix={`alias-${alias.name}`}
        />
      ) : (
        <p className="text-xs italic text-tp-ink-3">
          {t("models.paramsNone")}
        </p>
      )}
      <div className="flex items-center justify-end">
        <Button
          size="sm"
          onClick={() => saveMutation.mutate()}
          disabled={hasErrors || saveMutation.isPending || !schema}
          data-testid={`alias-save-${alias.name}`}
        >
          {saveMutation.isPending
            ? t("models.saving")
            : t("models.paramsSave")}
        </Button>
      </div>
      {provider ? null : null}
    </div>
  );
}

/** Inline-edit row. Cell is a span by default; click → input. Enter commits, Esc reverts. */
function AliasRow({
  alias,
  target,
  onChange,
  onRemove,
  onPickModel,
}: {
  alias: string;
  target: string;
  onChange: (next: [string, string]) => void;
  onRemove: () => void;
  onPickModel: () => void;
}) {
  const { t } = useTranslation();
  return (
    <TableRow className="border-b border-tp-glass-edge">
      <TableCell className="pl-3">
        <InlineEdit
          value={alias}
          onCommit={(v) => onChange([v, target])}
          placeholder="smart"
          mono
        />
      </TableCell>
      <TableCell>
        <div className="flex items-center gap-1">
          <InlineEdit
            value={target}
            onCommit={(v) => onChange([alias, v])}
            placeholder="claude-opus-4-7"
            mono
          />
          <Button
            size="sm"
            variant="ghost"
            onClick={onPickModel}
            aria-label={t("models.picker.changeButton")}
            className="h-7 px-2 text-[10px]"
          >
            {t("models.picker.changeButton")}
          </Button>
        </div>
      </TableCell>
      <TableCell>
        <Button
          size="sm"
          variant="ghost"
          onClick={onRemove}
          aria-label={t("models.remove")}
        >
          <Trash2 className="h-3.5 w-3.5" />
        </Button>
      </TableCell>
    </TableRow>
  );
}

function InlineEdit({
  value,
  onCommit,
  placeholder,
  mono,
}: {
  value: string;
  onCommit: (v: string) => void;
  placeholder?: string;
  mono?: boolean;
}) {
  const { t } = useTranslation();
  const [editing, setEditing] = React.useState(!value);
  const [draft, setDraft] = React.useState(value);
  React.useEffect(() => {
    if (!editing) setDraft(value);
  }, [value, editing]);
  if (!editing) {
    return (
      <button
        type="button"
        onClick={() => setEditing(true)}
        className={cn(
          "group inline-flex h-8 w-full items-center justify-between gap-1 rounded px-2 text-left transition-colors hover:bg-tp-glass-inner-hover",
          mono && "font-mono text-xs",
        )}
      >
        <span className={!value ? "text-tp-ink-3" : ""}>
          {value || placeholder || t("models.emptyValue")}
        </span>
        <Pencil className="h-3 w-3 text-tp-ink-3 opacity-0 transition-opacity group-hover:opacity-100" />
      </button>
    );
  }
  return (
    <div className="inline-flex w-full items-center gap-1">
      <Input
        autoFocus
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        placeholder={placeholder}
        className={cn("h-8", mono && "font-mono text-xs")}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            onCommit(draft);
            setEditing(false);
          } else if (e.key === "Escape") {
            setDraft(value);
            setEditing(false);
          }
        }}
      />
      <button
        type="button"
        onClick={() => {
          onCommit(draft);
          setEditing(false);
        }}
        className="inline-flex h-7 w-7 items-center justify-center rounded-md text-tp-ink-3 transition-colors hover:bg-tp-glass-inner-hover hover:text-tp-ink"
        aria-label={t("models.commit")}
      >
        <Check className="h-3.5 w-3.5" />
      </button>
      <button
        type="button"
        onClick={() => {
          setDraft(value);
          setEditing(false);
        }}
        className="inline-flex h-7 w-7 items-center justify-center rounded-md text-tp-ink-3 transition-colors hover:bg-tp-glass-inner-hover hover:text-tp-ink"
        aria-label={t("models.cancel")}
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
