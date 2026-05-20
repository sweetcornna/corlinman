"use client";

/**
 * Embedding admin page (Feature C).
 *
 * Single-page form: provider + model + dimension + enabled + a dynamic
 * params panel driven by the selected provider's `params_schema`.
 *
 * The benchmark tool underneath POSTs to `/admin/embedding/benchmark`
 * with a small set of sample strings and renders:
 *   - dimension, p50 + p99 latency
 *   - a CSS-grid similarity heatmap (opacity ramps with cosine)
 *   - a warnings list
 *
 * When either `/admin/embedding` or `/admin/providers` returns 503 we
 * render the "backend feature pending" banner rather than toasting.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { motion } from "framer-motion";
import { Sparkles } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  benchmarkEmbedding,
  CorlinmanApiError,
  fetchEmbedding,
  fetchProviders,
  upsertEmbedding,
  type BenchmarkView,
  type EmbeddingView,
  type ProviderView,
} from "@/lib/api";
import { DynamicParamsForm } from "@/components/dynamic-params-form";
import { cn } from "@/lib/utils";

export default function EmbeddingPage() {
  const { t } = useTranslation();
  const qc = useQueryClient();

  const providers = useQuery<ProviderView[]>({
    queryKey: ["admin", "providers"],
    queryFn: fetchProviders,
    retry: false,
  });

  const embedding = useQuery<EmbeddingView>({
    queryKey: ["admin", "embedding"],
    queryFn: fetchEmbedding,
    retry: false,
  });

  const [draft, setDraft] = React.useState<EmbeddingView | null>(null);
  const [initialized, setInitialized] = React.useState(false);
  const [paramErrors, setParamErrors] = React.useState<
    Record<string, string>
  >({});

  React.useEffect(() => {
    if (embedding.data && !initialized) {
      setDraft(embedding.data);
      setInitialized(true);
    }
  }, [embedding.data, initialized]);

  const capableProviders = React.useMemo(() => {
    const all = providers.data ?? [];
    // `capabilities.embedding` is optional in the contract. When the field
    // is absent we treat the provider as capable-unknown and include it.
    const capable = all.filter(
      (p) => p.enabled && (p.capabilities?.embedding ?? true),
    );
    return capable;
  }, [providers.data]);

  // When the picked provider changes, swap in that provider's params_schema
  // on the draft so the DynamicParamsForm renders against the right schema.
  const selectedProvider = React.useMemo(
    () => capableProviders.find((p) => p.name === draft?.provider) ?? null,
    [capableProviders, draft?.provider],
  );

  const schemaForDraft = React.useMemo(
    () => selectedProvider?.params_schema ?? draft?.params_schema ?? null,
    [selectedProvider, draft?.params_schema],
  );

  const backendPending =
    (embedding.isError &&
      embedding.error instanceof CorlinmanApiError &&
      embedding.error.status === 503) ||
    (providers.isError &&
      providers.error instanceof CorlinmanApiError &&
      providers.error.status === 503);

  const saveMutation = useMutation({
    mutationFn: () => {
      if (!draft) throw new Error("draft not loaded");
      return upsertEmbedding(draft);
    },
    onSuccess: (data) => {
      toast.success(t("embedding.saveSuccess"));
      setDraft(data);
      qc.invalidateQueries({ queryKey: ["admin", "embedding"] });
    },
    onError: (err) =>
      toast.error(
        t("embedding.saveFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      ),
  });

  if (backendPending) {
    return (
      <>
        <header className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight">
            {t("embedding.title")}
          </h1>
          <p className="text-sm text-tp-ink-3">
            {t("embedding.subtitle")}
          </p>
        </header>
        <BackendPendingBanner label={t("embedding.backendPending")} />
      </>
    );
  }

  const loading = embedding.isPending || providers.isPending;

  return (
    <>
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">
          {t("embedding.title")}
        </h1>
        <p className="text-sm text-tp-ink-3">
          {t("embedding.subtitle")}
        </p>
      </header>

      <section className="space-y-4 rounded-lg border border-tp-glass-edge bg-tp-glass p-4">
        {loading ? (
          <Skeleton className="h-40 w-full" />
        ) : !draft ? (
          <p className="text-xs text-destructive">
            {t("embedding.loadFailed")}
          </p>
        ) : (
          <>
            {capableProviders.length === 0 ? (
              <p className="rounded-md border border-dashed border-tp-glass-edge p-4 text-center text-xs text-tp-ink-3">
                {t("embedding.providerNoneCapable")}
              </p>
            ) : null}
            <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
              <div className="space-y-1.5">
                <Label htmlFor="embedding-provider" className="text-xs">
                  {t("embedding.providerLabel")}
                </Label>
                <select
                  id="embedding-provider"
                  value={draft.provider}
                  onChange={(e) =>
                    setDraft({ ...draft, provider: e.target.value })
                  }
                  className="flex h-9 w-full rounded-md border border-input bg-transparent px-2 text-sm"
                >
                  <option value="" disabled>
                    {t("embedding.providerPlaceholder")}
                  </option>
                  {capableProviders.map((p) => (
                    <option key={p.name} value={p.name}>
                      {p.name} ({p.kind})
                    </option>
                  ))}
                </select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="embedding-model" className="text-xs">
                  {t("embedding.modelLabel")}
                </Label>
                <Input
                  id="embedding-model"
                  value={draft.model}
                  onChange={(e) =>
                    setDraft({ ...draft, model: e.target.value })
                  }
                  placeholder={t("embedding.modelPlaceholder")}
                  className="font-mono text-xs"
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="embedding-dim" className="text-xs">
                  {t("embedding.dimensionLabel")}
                </Label>
                <Input
                  id="embedding-dim"
                  type="number"
                  min={1}
                  value={draft.dimension}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      dimension: parseInt(e.target.value, 10) || 0,
                    })
                  }
                  className="font-mono text-xs"
                />
                <p className="text-[11px] text-tp-ink-3">
                  {t("embedding.dimensionHint")}
                </p>
              </div>
            </div>

            <div className="flex items-center gap-3">
              <Label className="text-xs" htmlFor="embedding-enabled">
                {t("embedding.enabledLabel")}
              </Label>
              <button
                id="embedding-enabled"
                type="button"
                role="switch"
                aria-checked={draft.enabled}
                onClick={() =>
                  setDraft({ ...draft, enabled: !draft.enabled })
                }
                className={cn(
                  "inline-flex h-6 w-11 items-center rounded-full border border-input backdrop-blur-glass backdrop-saturate-glass transition-colors",
                  draft.enabled
                    ? "bg-[color-mix(in_oklch,var(--tp-amber)_34%,transparent)]"
                    : "bg-tp-glass-inner",
                )}
              >
                <span
                  className={cn(
                    "inline-block h-4 w-4 transform rounded-full border border-tp-glass-edge-strong bg-[color-mix(in_oklch,var(--tp-ink)_18%,transparent)] shadow-tp-panel transition-transform",
                    draft.enabled
                      ? "translate-x-[22px]"
                      : "translate-x-[3px]",
                  )}
                />
              </button>
            </div>

            <div className="space-y-2 rounded-md border border-tp-glass-edge p-3">
              <h3 className="text-sm font-semibold">
                {t("embedding.paramsTitle")}
              </h3>
              <DynamicParamsForm
                schema={schemaForDraft}
                value={draft.params ?? {}}
                onChange={(next) => setDraft({ ...draft, params: next })}
                onErrorsChange={setParamErrors}
                testIdPrefix="embedding-params"
              />
            </div>

            <div className="flex items-center justify-end gap-2">
              <Button
                onClick={() => saveMutation.mutate()}
                disabled={
                  Object.keys(paramErrors).length > 0 ||
                  saveMutation.isPending ||
                  !draft.provider
                }
                data-testid="embedding-save-btn"
              >
                {saveMutation.isPending
                  ? t("embedding.savingLabel")
                  : t("embedding.saveLabel")}
              </Button>
            </div>
          </>
        )}
      </section>

      <BenchmarkPanel />
    </>
  );
}

// --------------------------- benchmark ------------------------------------

const MAX_SAMPLES = 20;

function BenchmarkPanel() {
  const { t } = useTranslation();
  const [raw, setRaw] = React.useState(
    "Hello, world.\nBonjour le monde.\nThree tigers.",
  );
  const [result, setResult] = React.useState<BenchmarkView | null>(null);

  const samples = React.useMemo(
    () =>
      raw
        .split(/\r?\n/)
        .map((s) => s.trim())
        .filter(Boolean),
    [raw],
  );

  const run = useMutation({
    mutationFn: () => benchmarkEmbedding(samples),
    onSuccess: (data) => setResult(data),
    onError: (err) =>
      toast.error(
        t("embedding.benchmarkFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      ),
  });

  const tooMany = samples.length > MAX_SAMPLES;
  const empty = samples.length === 0;

  return (
    <section className="space-y-3 rounded-lg border border-tp-glass-edge bg-tp-glass p-4">
      <div className="flex items-center gap-2">
        <Sparkles className="h-4 w-4 text-tp-ink-3" />
        <h2 className="text-sm font-semibold">
          {t("embedding.benchmarkTitle")}
        </h2>
      </div>
      <p className="text-xs text-tp-ink-3">
        {t("embedding.benchmarkHint")}
      </p>

      <textarea
        value={raw}
        onChange={(e) => setRaw(e.target.value)}
        placeholder={t("embedding.benchmarkPlaceholder")}
        data-testid="benchmark-samples"
        className="min-h-[120px] w-full rounded-md border border-input bg-transparent p-3 font-mono text-xs"
      />

      <div className="flex items-center justify-between">
        <span className="text-[11px] text-tp-ink-3">
          {empty
            ? t("embedding.benchmarkEmpty")
            : tooMany
              ? t("embedding.benchmarkTooMany", { n: samples.length })
              : `${samples.length} samples`}
        </span>
        <Button
          size="sm"
          onClick={() => run.mutate()}
          disabled={empty || tooMany || run.isPending}
          data-testid="benchmark-run-btn"
        >
          {run.isPending
            ? t("embedding.benchmarkRunning")
            : t("embedding.benchmarkRun")}
        </Button>
      </div>

      {result ? (
        <motion.div
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.18, ease: "easeOut" }}
          className="space-y-4 border-t border-tp-glass-edge pt-4"
        >
          <div className="grid grid-cols-3 gap-3">
            <Stat
              label={t("embedding.benchmarkDim")}
              value={String(result.dimension)}
            />
            <Stat
              label={t("embedding.benchmarkP50")}
              value={t("embedding.ms", { n: result.latency_ms_p50.toFixed(1) })}
            />
            <Stat
              label={t("embedding.benchmarkP99")}
              value={t("embedding.ms", { n: result.latency_ms_p99.toFixed(1) })}
            />
          </div>

          <div className="space-y-2">
            <div>
              <h3 className="text-sm font-semibold">
                {t("embedding.benchmarkHeatmap")}
              </h3>
              <p className="text-[11px] text-tp-ink-3">
                {t("embedding.benchmarkHeatmapHint")}
              </p>
            </div>
            <SimilarityHeatmap matrix={result.similarity_matrix} />
          </div>

          <div className="space-y-1.5">
            <h3 className="text-sm font-semibold">
              {t("embedding.benchmarkWarnings")}
            </h3>
            {result.warnings.length === 0 ? (
              <p className="text-[11px] text-tp-ink-3">
                {t("embedding.benchmarkNoWarnings")}
              </p>
            ) : (
              <ul className="space-y-0.5 text-xs text-amber-400">
                {result.warnings.map((w, i) => (
                  <li key={i}>• {w}</li>
                ))}
              </ul>
            )}
          </div>
        </motion.div>
      ) : null}
    </section>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-tp-glass-edge bg-tp-glass/40 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-tp-ink-3">
        {label}
      </div>
      <div className="font-mono text-sm">{value}</div>
    </div>
  );
}

function SimilarityHeatmap({ matrix }: { matrix: number[][] }) {
  const n = matrix.length;
  if (n === 0) return null;
  // CSS grid of n×n cells. Opacity ramps linearly with the cosine value
  // clamped to [0, 1] (negatives clipped — they're rare in normalized
  // embeddings but we still want the diagonal to stand out).
  return (
    <div
      className="inline-grid rounded-md border border-tp-glass-edge p-1"
      style={{
        gridTemplateColumns: `repeat(${n}, minmax(22px, 1fr))`,
        gridTemplateRows: `repeat(${n}, minmax(22px, 1fr))`,
        gap: "2px",
      }}
      role="grid"
      aria-label="similarity-matrix"
    >
      {matrix.flatMap((row, i) =>
        row.map((cell, j) => {
          const clamped = Math.max(0, Math.min(1, cell));
          const alpha = 0.15 + clamped * 0.85;
          return (
            <div
              key={`${i}-${j}`}
              role="gridcell"
              title={`(${i},${j}) = ${cell.toFixed(3)}`}
              style={{
                backgroundColor: `rgba(99, 102, 241, ${alpha.toFixed(3)})`,
              }}
              className="rounded-[2px]"
            />
          );
        }),
      )}
    </div>
  );
}

function BackendPendingBanner({ label }: { label: string }) {
  return (
    <div
      className="rounded-md border border-dashed border-tp-glass-edge bg-tp-glass/40 px-4 py-6 text-center text-xs text-tp-ink-3"
      data-testid="backend-pending"
    >
      {label}
    </div>
  );
}
