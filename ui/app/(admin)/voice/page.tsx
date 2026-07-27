"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { AlertCircle, KeyRound, Mic, Play, Save } from "@/components/icons";

import { cn } from "@/lib/utils";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { VoicePicker } from "@/components/voice/voice-picker";
import {
  REDACTED_SECRET,
  getVoiceSettings,
  listVoiceBackends,
  previewVoice,
  putVoiceSettings,
  type VoiceBackend,
  type VoiceBackendsResponse,
  type VoicePreview,
  type VoiceSettings,
} from "@/lib/api/voice";

/**
 * `/voice` — text-to-speech backend, voice selection and audition.
 *
 * The page is driven entirely by `GET /admin/voice/backends`: models,
 * voices, formats and capability flags all come from the server-side
 * registry, so an operator-defined `[voice.backends.*]` provider renders
 * here with no UI change. Nothing switches on a known backend id.
 *
 * Draft state is seeded once from the server snapshot and compared back
 * against it for the dirty flag, so a background refetch never clobbers
 * an in-progress edit — same pattern as the QQ channel page.
 */
export default function VoicePage() {
  const { t } = useTranslation();
  const qc = useQueryClient();

  const backendsQuery = useQuery<VoiceBackendsResponse>({
    queryKey: ["admin", "voice", "backends"],
    queryFn: listVoiceBackends,
    retry: false,
  });
  const settingsQuery = useQuery<VoiceSettings>({
    queryKey: ["admin", "voice", "settings"],
    queryFn: getVoiceSettings,
    retry: false,
  });

  const [draft, setDraft] = React.useState<VoiceSettings | null>(null);
  const [apiKeyInput, setApiKeyInput] = React.useState("");
  const [previewText, setPreviewText] = React.useState("");
  const [preview, setPreview] = React.useState<VoicePreview | null>(null);
  const [previewError, setPreviewError] = React.useState<string | null>(null);
  const [previewingVoice, setPreviewingVoice] = React.useState<string | null>(null);

  // Seed once; later refetches must not overwrite an in-progress edit.
  React.useEffect(() => {
    if (draft === null && settingsQuery.data) setDraft(settingsQuery.data);
  }, [draft, settingsQuery.data]);

  const backends = backendsQuery.data?.backends ?? [];
  const selected: VoiceBackend | undefined = React.useMemo(() => {
    if (!draft) return undefined;
    return backends.find((b) => b.id === draft.backend) ?? backends[0];
  }, [backends, draft]);

  const dirty = React.useMemo(() => {
    if (!draft || !settingsQuery.data) return false;
    return (
      JSON.stringify(draft) !== JSON.stringify(settingsQuery.data) ||
      apiKeyInput.length > 0
    );
  }, [draft, settingsQuery.data, apiKeyInput]);

  const save = useMutation({
    mutationFn: async () => {
      if (!draft || !selected) return;
      const overrides = { ...(draft.backends ?? {}) };
      if (apiKeyInput.trim()) {
        overrides[selected.id] = {
          ...(overrides[selected.id] ?? {}),
          api_key: apiKeyInput.trim(),
        };
      }
      await putVoiceSettings({ ...draft, backends: overrides });
    },
    onSuccess: async () => {
      setApiKeyInput("");
      toast.success(t("voice.saved", "Voice settings saved"));
      await qc.invalidateQueries({ queryKey: ["admin", "voice"] });
      setDraft(null);
    },
    onError: (err: Error) =>
      toast.error(t("voice.saveFailed", "Save failed"), {
        description: err.message,
      }),
  });

  const runPreview = React.useCallback(
    async (voiceId?: string) => {
      if (!draft || !selected) return;
      setPreviewError(null);
      setPreviewingVoice(voiceId ?? draft.voice ?? "");
      try {
        const res = await previewVoice({
          backend: selected.id,
          voice: voiceId ?? draft.voice,
          model: draft.model,
          format: draft.format,
          instructions: draft.instructions,
          text: previewText.trim() || undefined,
        });
        if (res.ok) {
          setPreview(res);
        } else {
          setPreview(null);
          setPreviewError(`${res.error}: ${res.message}`);
        }
      } catch (err) {
        setPreview(null);
        setPreviewError((err as Error).message);
      } finally {
        setPreviewingVoice(null);
      }
    },
    [draft, selected, previewText],
  );

  const patch = React.useCallback((next: Partial<VoiceSettings>) => {
    setDraft((cur) => (cur ? { ...cur, ...next } : cur));
  }, []);

  if (backendsQuery.isLoading || settingsQuery.isLoading || !draft) {
    return <PageSkeleton />;
  }

  const loadFailed = backendsQuery.isError && !backendsQuery.data;

  return (
    <div className="space-y-5">
      <header className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            {t("voice.title", "Voice")}
          </h1>
          <p className="mt-1 max-w-2xl text-sm text-sg-ink-2">
            {t(
              "voice.subtitle",
              "Pick a text-to-speech provider and voice, audition it, and the agent will use it for voice replies across every channel.",
            )}
          </p>
        </div>
        <Button
          onClick={() => save.mutate()}
          disabled={!dirty || save.isPending}
          data-testid="voice-save"
        >
          <Save className="h-4 w-4" aria-hidden="true" />
          {t("voice.save", "Save")}
        </Button>
      </header>

      {loadFailed ? (
        <Alert
          variant="warning"
          title={t("voice.loadFailed.title", "Could not load voice backends")}
        >
          {backendsQuery.error instanceof Error
            ? backendsQuery.error.message
            : String(backendsQuery.error)}
        </Alert>
      ) : null}

      <section className="flex items-center gap-3 rounded-sg-md border border-sg-border bg-sg-card p-4">
        <Switch
          checked={draft.enabled}
          onCheckedChange={(v) => patch({ enabled: v })}
          data-testid="voice-enabled"
          aria-label={t("voice.enabled", "Enable voice replies")}
        />
        <div>
          <p className="text-sm text-sg-ink">
            {t("voice.enabled", "Enable voice replies")}
          </p>
          <p className="text-xs text-sg-ink-3">
            {t(
              "voice.enabledHint",
              "When off, the text_to_speech tool stays available but nothing is synthesised by default.",
            )}
          </p>
        </div>
      </section>

      {/* ── Backend ─────────────────────────────────────────────── */}
      <section className="space-y-2">
        <Label>{t("voice.backend", "Provider")}</Label>
        <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3" data-testid="voice-backends">
          {backends.map((b) => (
            <li key={b.id}>
              <button
                type="button"
                onClick={() =>
                  patch({
                    backend: b.id,
                    voice: b.default_voice,
                    model: b.default_model,
                  })
                }
                className={cn(
                  "h-full w-full rounded-sg-md border p-3 text-left transition-colors",
                  b.id === selected?.id
                    ? "border-sg-tint/60 bg-sg-inset"
                    : "border-sg-border bg-sg-card hover:bg-sg-inset-hover",
                )}
                data-testid={`voice-backend-${b.id}`}
                data-selected={b.id === selected?.id}
              >
                <span className="flex items-center gap-1.5">
                  <span className="truncate text-sm text-sg-ink">{b.label}</span>
                  {b.custom ? (
                    <span className="rounded-full border border-sg-border px-1.5 text-[10px] text-sg-ink-4">
                      {t("voice.customTag", "custom")}
                    </span>
                  ) : null}
                  {!b.credential_set ? (
                    <AlertCircle
                      className="h-3 w-3 shrink-0 text-sg-warn"
                      aria-label={t("voice.noCredential", "No credential")}
                    />
                  ) : null}
                </span>
                <span className="mt-1 block text-xs leading-snug text-sg-ink-3">
                  {b.description}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </section>

      {selected ? (
        <>
          {!selected.credential_set ? (
            <Alert
              variant="warning"
              icon={<KeyRound className="h-4 w-4" aria-hidden />}
              title={t("voice.credentialMissing.title", "No credential configured")}
            >
              {t("voice.credentialMissing.body", {
                defaultValue:
                  "Set an API key below, or export {{env}} on the server.",
                env: selected.api_key_env || "the provider env var",
              })}
            </Alert>
          ) : null}

          <div className="grid gap-5 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
            {/* ── Voices ──────────────────────────────────────── */}
            <section className="space-y-2">
              <Label>{t("voice.voice", "Voice")}</Label>
              <VoicePicker
                backend={selected}
                value={draft.voice}
                onChange={(v) => patch({ voice: v })}
                onPreview={(v) => {
                  patch({ voice: v });
                  void runPreview(v);
                }}
                previewingId={previewingVoice}
              />
            </section>

            {/* ── Parameters ──────────────────────────────────── */}
            <section className="space-y-4">
              <div className="space-y-1.5">
                <Label htmlFor="voice-model">{t("voice.model", "Model")}</Label>
                <select
                  id="voice-model"
                  value={draft.model || selected.default_model}
                  onChange={(e) => patch({ model: e.target.value })}
                  className="h-10 w-full rounded-sg-sm border border-sg-border bg-sg-inset px-3 text-sm text-sg-ink"
                  data-testid="voice-model"
                >
                  {selected.models.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="voice-format">{t("voice.format", "Format")}</Label>
                <select
                  id="voice-format"
                  value={draft.format}
                  onChange={(e) => patch({ format: e.target.value })}
                  className="h-10 w-full rounded-sg-sm border border-sg-border bg-sg-inset px-3 text-sm text-sg-ink"
                  data-testid="voice-format"
                >
                  {selected.formats.map((f) => (
                    <option key={f} value={f}>
                      {f}
                    </option>
                  ))}
                </select>
                <p className="text-xs text-sg-ink-3">
                  {t(
                    "voice.formatHint",
                    "mp3 is the safe default — channels that need another container transcode automatically.",
                  )}
                </p>
              </div>

              {selected.supports_instructions ? (
                <div className="space-y-1.5">
                  <Label htmlFor="voice-instructions">
                    {t("voice.instructions", "Delivery")}
                  </Label>
                  <textarea
                    id="voice-instructions"
                    value={draft.instructions}
                    onChange={(e) => patch({ instructions: e.target.value })}
                    rows={3}
                    className="w-full rounded-sg-sm border border-sg-border bg-sg-inset px-3 py-2 text-sm text-sg-ink"
                    placeholder={t(
                      "voice.instructionsPlaceholder",
                      "e.g. speak slowly and warmly",
                    )}
                    data-testid="voice-instructions"
                  />
                </div>
              ) : null}

              {selected.supports_speed ? (
                <div className="space-y-1.5">
                  <Label htmlFor="voice-speed">{t("voice.speed", "Speed")}</Label>
                  <Input
                    id="voice-speed"
                    type="number"
                    step="0.05"
                    min="0.25"
                    max="4"
                    value={draft.speed ?? ""}
                    onChange={(e) =>
                      patch({
                        speed: e.target.value === "" ? null : Number(e.target.value),
                      })
                    }
                    data-testid="voice-speed"
                  />
                </div>
              ) : null}

              <div className="space-y-1.5">
                <Label htmlFor="voice-api-key">
                  {t("voice.apiKey", "API key")}
                </Label>
                <Input
                  id="voice-api-key"
                  type="password"
                  autoComplete="off"
                  value={apiKeyInput}
                  onChange={(e) => setApiKeyInput(e.target.value)}
                  placeholder={
                    draft.backends?.[selected.id]?.api_key === REDACTED_SECRET
                      ? t("voice.apiKeyStored", "Stored — leave blank to keep")
                      : t("voice.apiKeyPlaceholder", "Paste to set")
                  }
                  data-testid="voice-api-key"
                />
              </div>
            </section>
          </div>

          {/* ── Preview ────────────────────────────────────────── */}
          <section className="space-y-3 rounded-sg-md border border-sg-border bg-sg-card p-4">
            <Label htmlFor="voice-preview-text">
              {t("voice.preview", "Preview")}
            </Label>
            <div className="flex flex-col gap-2 sm:flex-row">
              <Input
                id="voice-preview-text"
                value={previewText}
                onChange={(e) => setPreviewText(e.target.value)}
                placeholder={t(
                  "voice.previewPlaceholder",
                  "Text to read aloud (optional)",
                )}
                data-testid="voice-preview-text"
              />
              <Button
                variant="secondary"
                onClick={() => void runPreview()}
                disabled={previewingVoice !== null}
                data-testid="voice-preview-run"
              >
                {previewingVoice !== null ? (
                  <Mic className="h-4 w-4 animate-pulse" aria-hidden="true" />
                ) : (
                  <Play className="h-4 w-4" aria-hidden="true" />
                )}
                {t("voice.previewRun", "Play sample")}
              </Button>
            </div>

            {previewError ? (
              <Alert variant="danger" title={t("voice.previewFailed", "Preview failed")}>
                <span data-testid="voice-preview-error">{previewError}</span>
              </Alert>
            ) : null}

            {preview ? (
              <div className="space-y-1" data-testid="voice-preview-result">
                {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
                <audio
                  controls
                  autoPlay
                  src={preview.url}
                  className="w-full"
                  data-testid="voice-preview-audio"
                />
                <p className="font-mono text-[10px] text-sg-ink-4">
                  {preview.backend} · {preview.model} · {preview.voice} ·{" "}
                  {preview.format} · {(preview.size_bytes / 1024).toFixed(1)}KB
                </p>
              </div>
            ) : null}
          </section>
        </>
      ) : null}
    </div>
  );
}

function PageSkeleton() {
  return (
    <div className="space-y-4" data-testid="voice-skeleton">
      <Skeleton className="h-8 w-40" />
      <Skeleton className="h-20 w-full" />
      <Skeleton className="h-32 w-full" />
    </div>
  );
}
