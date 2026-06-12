"use client";

/**
 * Providers admin page (Feature C).
 *
 * Table of every entry in `[providers.*]` plus an Add/Edit/Delete modal.
 * - Add / Edit: POST /admin/providers (upsert) with a dynamic params form
 *   driven by the provider's `params_schema`.
 * - Delete: DELETE /admin/providers/:name; a 409 surfaces the list of
 *   referencing aliases/embedding so the user can unbind them first.
 *
 * When the gateway returns 503 we render a dedicated empty state rather
 * than toasting — the v0.1.x gateway simply does not ship this surface yet.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { motion } from "framer-motion";
import { Copy, Loader2, Pencil, Plug, Plus, RefreshCw, Trash2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  CorlinmanApiError,
  deleteCustomProvider,
  deleteProvider,
  fetchProviders,
  getProviderModels,
  listCustomProviders,
  probeProviderModels,
  upsertProvider,
  type CustomProviderRow,
  type ProviderKind,
  type ProviderModel,
  type ProviderModelProbeRequest,
  type ProviderUpsert,
  type ProviderView,
} from "@/lib/api";
import { DynamicParamsForm } from "@/components/dynamic-params-form";
import { AddCustomProviderModal } from "@/components/providers/add-custom-modal";
import { TestConnectionButton } from "@/components/providers/test-connection-button";
import { cn } from "@/lib/utils";

const KINDS: ProviderKind[] = [
  "anthropic",
  "openai",
  "google",
  "deepseek",
  "qwen",
  "glm",
  "openai_compatible",
  // Market kinds added with the free-form-providers refactor. The backend
  // accepts them via /admin/providers and the dropdown now offers them as
  // first-class choices instead of forcing operators to reach for
  // openai_compatible + a hand-rolled base_url.
  "mistral",
  "cohere",
  "together",
  "groq",
  "replicate",
  "bedrock",
  "azure",
];

type KeySource = "env" | "value" | "unset";

type DraftProvider = {
  name: string;
  kind: ProviderKind;
  enabled: boolean;
  base_url: string;
  api_key_source: KeySource;
  api_key_env_name: string;
  api_key_value: string;
  params: Record<string, unknown>;
};

const BLANK_DRAFT: DraftProvider = {
  name: "",
  kind: "openai_compatible",
  enabled: true,
  base_url: "",
  api_key_source: "env",
  api_key_env_name: "",
  api_key_value: "",
  params: {},
};

function toDraft(p: ProviderView): DraftProvider {
  return {
    name: p.name,
    kind: p.kind,
    enabled: p.enabled,
    base_url: p.base_url ?? "",
    api_key_source: p.api_key_source,
    api_key_env_name: p.api_key_env_name ?? "",
    api_key_value: "",
    params: p.params ?? {},
  };
}

function toUpsert(d: DraftProvider): ProviderUpsert {
  let api_key: ProviderUpsert["api_key"] = null;
  if (d.api_key_source === "env" && d.api_key_env_name.trim()) {
    api_key = { env: d.api_key_env_name.trim() };
  } else if (d.api_key_source === "value" && d.api_key_value.trim()) {
    api_key = { value: d.api_key_value.trim() };
  }
  return {
    name: d.name.trim(),
    kind: d.kind,
    enabled: d.enabled,
    base_url: d.base_url.trim() || undefined,
    api_key,
    params: d.params,
  };
}

function canReuseSavedLiteralKey(
  editing: ProviderView | null,
  draft: DraftProvider,
): editing is ProviderView {
  return (
    !!editing &&
    draft.name.trim() === editing.name &&
    editing.api_key_source === "value" &&
    draft.api_key_source === "value" &&
    !draft.api_key_value.trim()
  );
}

function toModelProbe(
  d: DraftProvider,
  editing: ProviderView | null,
): ProviderModelProbeRequest {
  const body: ProviderModelProbeRequest = {
    kind: d.kind,
    params: d.params,
  };
  const baseUrl = d.base_url.trim();
  if (baseUrl) {
    body.base_url = baseUrl;
  }
  if (d.api_key_source === "env" && d.api_key_env_name.trim()) {
    body.api_key = { env: d.api_key_env_name.trim() };
  } else if (d.api_key_source === "value" && d.api_key_value.trim()) {
    body.api_key = { value: d.api_key_value.trim() };
  } else if (canReuseSavedLiteralKey(editing, d)) {
    body.existing_name = editing.name;
  }
  return body;
}

function sameDiscoveryParams(
  left: Record<string, unknown>,
  right: Record<string, unknown>,
) {
  return JSON.stringify(left ?? {}) === JSON.stringify(right ?? {});
}

function shouldUseSavedModelDiscovery(
  editing: ProviderView | null,
  draft: DraftProvider,
) {
  return (
    canReuseSavedLiteralKey(editing, draft) &&
    draft.kind === editing.kind &&
    draft.base_url.trim() === (editing.base_url ?? "").trim() &&
    sameDiscoveryParams(draft.params, editing.params ?? {})
  );
}

type ModelDiscoveryRequest = {
  generation: number;
  draft: DraftProvider;
  editing: ProviderView | null;
};

/**
 * Exported as a named function so `/admin/credentials` can mount the
 * same admin content inline (UX merge: providers + credentials are a
 * single sidebar entry). The default export stays so `/admin/providers`
 * still works as a deep link.
 */
export type ProvidersAdminContentProps = {
  onCustomProvidersChanged?: () => void;
};

export function ProvidersAdminContent({
  onCustomProvidersChanged,
}: ProvidersAdminContentProps = {}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const providers = useQuery<ProviderView[]>({
    queryKey: ["admin", "providers"],
    queryFn: fetchProviders,
    retry: false,
  });

  const [editorOpen, setEditorOpen] = React.useState(false);
  const [editing, setEditing] = React.useState<ProviderView | null>(null);
  const [deleting, setDeleting] = React.useState<ProviderView | null>(null);
  const [deleteBlock, setDeleteBlock] = React.useState<string[] | null>(null);

  const backendPending =
    providers.isError &&
    providers.error instanceof CorlinmanApiError &&
    providers.error.status === 503;

  const deleteMutation = useMutation({
    mutationFn: (name: string) => deleteProvider(name),
    onSuccess: () => {
      toast.success(t("providers.deleteSuccess"));
      setDeleting(null);
      setDeleteBlock(null);
      qc.invalidateQueries({ queryKey: ["admin", "providers"] });
    },
    onError: (err) => {
      if (err instanceof CorlinmanApiError && err.status === 409) {
        // The server wraps references in the body; extract + surface.
        const parsed = parseReferences(err.message);
        setDeleteBlock(parsed);
        return;
      }
      toast.error(
        t("providers.deleteFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  return (
    <>
      <header className="flex items-end justify-between gap-3">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight">
            {t("providers.title")}
          </h1>
          <p className="text-sm text-sg-ink-3">
            {t("providers.subtitle")}
          </p>
        </div>
        <Button
          size="sm"
          onClick={() => {
            setEditing(null);
            setEditorOpen(true);
          }}
          data-testid="providers-add-btn"
        >
          <Plus className="h-3 w-3" />
          {t("providers.add")}
        </Button>
      </header>

      <section className="space-y-3 rounded-lg border border-sg-border bg-sg-card p-4">
        {providers.isPending ? (
          <Skeleton className="h-24 w-full" />
        ) : backendPending ? (
          <BackendPendingBanner label={t("providers.backendPending")} />
        ) : providers.isError ? (
          <p className="text-xs text-sg-err">
            {t("providers.loadFailed")}:{" "}
            {(providers.error as Error).message}
          </p>
        ) : (providers.data ?? []).length === 0 ? (
          <EmptyProviders
            title={t("providers.noneTitle")}
            hint={t("providers.noneHint")}
          />
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="border-b border-sg-border hover:bg-transparent">
                <TableHead className="w-44 pl-3">
                  {t("providers.colName")}
                </TableHead>
                <TableHead className="w-40">
                  {t("providers.colKind")}
                </TableHead>
                <TableHead>{t("providers.colBaseUrl")}</TableHead>
                <TableHead className="w-44">
                  {t("providers.colKey")}
                </TableHead>
                <TableHead className="w-24">
                  {t("providers.colEnabled")}
                </TableHead>
                <TableHead className="w-44"></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {providers.data!.map((p) => (
                <TableRow
                  key={p.name}
                  className="border-b border-sg-border"
                  data-testid={`provider-row-${p.name}`}
                >
                  <TableCell className="pl-3 font-mono text-xs">
                    {p.name}
                  </TableCell>
                  <TableCell>
                    <Badge variant="secondary" className="font-mono">
                      {p.kind}
                    </Badge>
                  </TableCell>
                  <TableCell className="font-mono text-[11px] text-sg-ink-3">
                    {p.base_url ?? t("providers.baseUrlDefault")}
                  </TableCell>
                  <TableCell className="text-xs">
                    {p.api_key_source === "env" ? (
                      <span className="font-mono text-sg-ink-3">
                        {t("providers.keyFromEnv", {
                          name: p.api_key_env_name ?? "?",
                        })}
                      </span>
                    ) : p.api_key_source === "value" ? (
                      <span className="text-sg-ink-3">
                        {t("providers.keyLiteral")}
                      </span>
                    ) : (
                      <span className="text-sg-err">
                        {t("providers.keyUnset")}
                      </span>
                    )}
                  </TableCell>
                  <TableCell>
                    {p.enabled ? (
                      <Badge className="border-transparent bg-sg-ok-soft text-sg-ok">
                        {t("common.enabled")}
                      </Badge>
                    ) : (
                      <Badge variant="secondary">
                        {t("common.disabled")}
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-1">
                      <TestConnectionButton name={p.name} />
                      <Button
                        size="sm"
                        variant="ghost"
                        aria-label={t("providers.edit")}
                        onClick={() => {
                          setEditing(p);
                          setEditorOpen(true);
                        }}
                      >
                        <Pencil className="h-3.5 w-3.5" />
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        aria-label={t("providers.remove")}
                        onClick={() => setDeleting(p)}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </section>

      <CustomProvidersSection
        onCustomProvidersChanged={onCustomProvidersChanged}
      />

      <ProviderEditorDialog
        open={editorOpen}
        onOpenChange={(o) => {
          setEditorOpen(o);
          if (!o) setEditing(null);
        }}
        editing={editing}
      />

      <Dialog
        open={!!deleting}
        onOpenChange={(o) => {
          if (!o) {
            setDeleting(null);
            setDeleteBlock(null);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {deleteBlock
                ? t("providers.deleteBlockedTitle", {
                    name: deleting?.name ?? "",
                  })
                : t("providers.deleteConfirmTitle", {
                    name: deleting?.name ?? "",
                  })}
            </DialogTitle>
            <DialogDescription>
              {deleteBlock
                ? t("providers.deleteBlockedBody")
                : t("providers.deleteConfirmBody")}
            </DialogDescription>
          </DialogHeader>
          {deleteBlock ? (
            <ul className="space-y-1 text-xs font-mono">
              {deleteBlock.map((ref) => (
                <li key={ref} className="text-sg-err">
                  • {ref}
                </li>
              ))}
            </ul>
          ) : null}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setDeleting(null);
                setDeleteBlock(null);
              }}
            >
              {t("providers.deleteCancel")}
            </Button>
            {!deleteBlock ? (
              <Button
                variant="destructive"
                disabled={deleteMutation.isPending}
                onClick={() =>
                  deleting && deleteMutation.mutate(deleting.name)
                }
                data-testid="providers-confirm-delete-btn"
              >
                {t("providers.deleteConfirm")}
              </Button>
            ) : null}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

// ----------------------------- dialog -------------------------------------

interface EditorProps {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  editing: ProviderView | null;
}

function ProviderEditorDialog({ open, onOpenChange, editing }: EditorProps) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [draft, setDraft] = React.useState<DraftProvider>(BLANK_DRAFT);
  const [modelDiscovery, setModelDiscovery] = React.useState<{
    models: ProviderModel[];
    error?: string;
  }>({ models: [] });
  const modelDiscoveryGeneration = React.useRef(0);
  const [paramErrors, setParamErrors] = React.useState<
    Record<string, string>
  >({});

  React.useEffect(() => {
    modelDiscoveryGeneration.current += 1;
    if (open) {
      setDraft(editing ? toDraft(editing) : { ...BLANK_DRAFT });
      setModelDiscovery({ models: [] });
      setParamErrors({});
    }
  }, [open, editing]);

  const updateDraft = React.useCallback((patch: Partial<DraftProvider>) => {
    modelDiscoveryGeneration.current += 1;
    setDraft((prev) => ({ ...prev, ...patch }));
    setModelDiscovery({ models: [] });
  }, []);

  const schema = editing?.params_schema ?? { type: "object", properties: {} };
  const hasErrors = Object.keys(paramErrors).length > 0;
  const nameOk = draft.name.trim().length > 0;
  const baseUrlOk =
    draft.kind !== "openai_compatible" || draft.base_url.trim().length > 0;

  const saveMutation = useMutation({
    mutationFn: () => upsertProvider(toUpsert(draft)),
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
                  onChange={(e) =>
                    updateDraft({
                      kind: e.target.value as ProviderKind,
                    })
                  }
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
                    {t("providers.modelsHint")}
                  </p>
                </div>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() =>
                    modelDiscoveryMutation.mutate({
                      generation: modelDiscoveryGeneration.current,
                      draft,
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
                  {modelDiscovery.models.map((m) => (
                    <div
                      key={m.id}
                      className="flex min-h-9 items-center justify-between gap-2 rounded-md border border-sg-border bg-sg-inset px-2"
                    >
                      <span
                        className="min-w-0 truncate font-mono text-[11px]"
                        title={m.id}
                      >
                        {m.id}
                      </span>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="h-7 shrink-0 px-2"
                        aria-label={t("providers.modelsCopyAria", {
                          id: m.id,
                        })}
                        onClick={() => copyModelId(m.id)}
                      >
                        <Copy className="h-3 w-3" />
                      </Button>
                    </div>
                  ))}
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
                onErrorsChange={setParamErrors}
                testIdPrefix="provider-params"
              />
            </div>
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

// ----------------------------- helpers ------------------------------------

function EmptyProviders({ title, hint }: { title: string; hint: string }) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-md border border-dashed border-sg-border py-10 text-center">
      <Plug className="h-6 w-6 text-sg-ink-3/60" />
      <p className="text-sm font-medium">{title}</p>
      <p className="max-w-sm text-xs text-sg-ink-3">{hint}</p>
    </div>
  );
}

function BackendPendingBanner({ label }: { label: string }) {
  return (
    <div
      className="rounded-md border border-dashed border-sg-border bg-sg-card px-4 py-6 text-center text-xs text-sg-ink-3"
      data-testid="backend-pending"
    >
      {label}
    </div>
  );
}

/** The server conflict body may be JSON-ish (`{"error": "...", "references":
 *  ["alias.smart", "embedding"]}`) or a plain string. Be liberal in what we
 *  accept so a mis-shaped 409 doesn't crash the page. */
function parseReferences(raw: string): string[] {
  try {
    const parsed = JSON.parse(raw) as { references?: unknown };
    if (Array.isArray(parsed.references)) {
      return parsed.references.map((r) => String(r));
    }
  } catch {
    /* not JSON */
  }
  return [raw];
}

// ----------------------------- custom providers ---------------------------
//
// W-B2 — separate "Custom providers" section below the built-in registry.
// Reads from `GET /admin/providers/custom`, deletes via
// `DELETE /admin/providers/custom/{slug}`, adds via the
// AddCustomProviderModal which posts to `POST /admin/providers/custom`.
//
// Backend pending (503) renders the same "feature pending" banner used
// upstairs so a v0.1 gateway doesn't toast-spam the operator.

type CustomProvidersSectionProps = {
  onCustomProvidersChanged?: () => void;
};

function CustomProvidersSection({
  onCustomProvidersChanged,
}: CustomProvidersSectionProps) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [addOpen, setAddOpen] = React.useState(false);
  const [deleting, setDeleting] =
    React.useState<CustomProviderRow | null>(null);

  const customs = useQuery<CustomProviderRow[]>({
    queryKey: ["admin", "providers", "custom"],
    queryFn: listCustomProviders,
    retry: false,
  });

  const backendPending =
    customs.isError &&
    customs.error instanceof CorlinmanApiError &&
    customs.error.status === 503;

  const deleteMutation = useMutation({
    mutationFn: (slug: string) => deleteCustomProvider(slug),
    onSuccess: () => {
      toast.success(`Custom provider "${deleting?.slug ?? ""}" deleted`);
      setDeleting(null);
      qc.invalidateQueries({ queryKey: ["admin", "providers", "custom"] });
      // Built-in section pulls from the same TOML — refresh both so the
      // operator doesn't see a stale ghost row.
      qc.invalidateQueries({ queryKey: ["admin", "providers"] });
      onCustomProvidersChanged?.();
    },
    onError: (err) => {
      toast.error(
        `Delete failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    },
  });

  return (
    <>
      <header className="flex items-end justify-between gap-3 pt-2">
        <div className="space-y-1">
          <h2 className="text-lg font-semibold tracking-tight">
            Custom providers
          </h2>
          <p className="text-xs text-sg-ink-3">
            Operator-defined providers registered via{" "}
            <code>/admin/providers/custom</code>. The transport kind picks
            which built-in protocol (OpenAI-compatible, Anthropic, etc.)
            ferries the requests.
          </p>
        </div>
        <Button
          size="sm"
          onClick={() => setAddOpen(true)}
          data-testid="custom-providers-add-btn"
        >
          <Plus className="h-3 w-3" />
          Add custom provider
        </Button>
      </header>

      <section className="space-y-3 rounded-lg border border-sg-border bg-sg-card p-4">
        {customs.isPending ? (
          <Skeleton className="h-24 w-full" />
        ) : backendPending ? (
          <BackendPendingBanner label={t("providers.backendPending")} />
        ) : customs.isError ? (
          <p className="text-xs text-sg-err">
            Load failed: {(customs.error as Error).message}
          </p>
        ) : (customs.data ?? []).length === 0 ? (
          <EmptyProviders
            title="No custom providers yet."
            hint='Click "Add custom provider" to register an OpenAI-compatible endpoint or any other supported transport against a slug of your choice.'
          />
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="border-b border-sg-border hover:bg-transparent">
                <TableHead className="w-44 pl-3">Slug</TableHead>
                <TableHead className="w-40">Kind</TableHead>
                <TableHead>Base URL</TableHead>
                <TableHead className="w-28">API key</TableHead>
                <TableHead className="w-24" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {customs.data!.map((p) => (
                <TableRow
                  key={p.slug}
                  className="border-b border-sg-border"
                  data-testid={`custom-provider-row-${p.slug}`}
                >
                  <TableCell className="pl-3 font-mono text-xs">
                    {p.slug}
                  </TableCell>
                  <TableCell>
                    <Badge variant="secondary" className="font-mono">
                      {p.kind}
                    </Badge>
                  </TableCell>
                  <TableCell className="font-mono text-[11px] text-sg-ink-3">
                    {p.base_url ?? t("providers.baseUrlDefault")}
                  </TableCell>
                  <TableCell className="text-xs">
                    {p.has_api_key ? (
                      <Badge className="border-transparent bg-sg-ok-soft text-sg-ok">
                        set
                      </Badge>
                    ) : (
                      <Badge variant="secondary">unset</Badge>
                    )}
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-1">
                      <Button
                        size="sm"
                        variant="ghost"
                        aria-label={`Delete ${p.slug}`}
                        onClick={() => setDeleting(p)}
                        data-testid={`custom-provider-delete-${p.slug}`}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </section>

      <AddCustomProviderModal
        open={addOpen}
        onOpenChange={setAddOpen}
        onCreated={() => {
          qc.invalidateQueries({
            queryKey: ["admin", "providers", "custom"],
          });
          qc.invalidateQueries({ queryKey: ["admin", "providers"] });
          onCustomProvidersChanged?.();
        }}
      />

      {/* Confirm-delete dialog — same shape as the built-in section's
          delete confirm, scoped to a single custom row. */}
      <Dialog
        open={!!deleting}
        onOpenChange={(o) => {
          if (!o) setDeleting(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete {deleting?.slug ?? ""}?</DialogTitle>
            <DialogDescription>
              This removes the <code>[providers.{deleting?.slug ?? ""}]</code>{" "}
              block from <code>config.toml</code> and cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleting(null)}>
              {t("providers.deleteCancel")}
            </Button>
            <Button
              variant="destructive"
              disabled={deleteMutation.isPending}
              onClick={() =>
                deleting && deleteMutation.mutate(deleting.slug)
              }
              data-testid="custom-providers-confirm-delete-btn"
            >
              {deleteMutation.isPending
                ? t("providers.savingLabel")
                : t("providers.deleteConfirm")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

export default function ProvidersPage() {
  return <ProvidersAdminContent />;
}
