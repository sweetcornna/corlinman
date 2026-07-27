"use client";

import * as React from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import {
  ArrowUpRight,
  Image as ImageIcon,
  Mic,
  Save,
  Search,
  Zap,
} from "@/components/icons";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  getModelCapabilities,
  putImageCapability,
  putSearchCapability,
  type ModelCapabilities,
} from "@/lib/api/model-capabilities";
import { listVoiceBackends, type VoiceBackendsResponse } from "@/lib/api/voice";

/**
 * Capability bindings: one place answering "which model runs when the
 * agent chats, draws, or speaks?".
 *
 * Chat is read-only here and links to the routing tab (its editor already
 * lives there); image is editable inline; speech summarises `[voice]` and
 * links to the voice page, which owns the per-voice picker and audition.
 * Duplicating the voice editor here would give two write paths to the same
 * config with no way to keep them consistent.
 */
export function CapabilitiesSection() {
  const { t } = useTranslation();
  const qc = useQueryClient();

  const caps = useQuery<ModelCapabilities>({
    queryKey: ["admin", "models", "capabilities"],
    queryFn: getModelCapabilities,
    retry: false,
  });
  const backends = useQuery<VoiceBackendsResponse>({
    queryKey: ["admin", "voice", "backends"],
    queryFn: listVoiceBackends,
    retry: false,
  });

  const [imgProvider, setImgProvider] = React.useState<string | null>(null);
  const [imgModel, setImgModel] = React.useState<string | null>(null);
  const [searchBackend, setSearchBackend] = React.useState<string | null>(null);
  // `null` = untouched, so the PUT omits `api_key` and the stored key
  // survives. The read model never echoes it, so we cannot seed this.
  const [searchKey, setSearchKey] = React.useState<string | null>(null);

  // Seed once so a background refetch never clobbers an in-progress edit.
  React.useEffect(() => {
    if (imgProvider === null && caps.data) setImgProvider(caps.data.image.provider);
    if (imgModel === null && caps.data) setImgModel(caps.data.image.model);
    if (searchBackend === null && caps.data) setSearchBackend(caps.data.search.backend);
  }, [caps.data, imgProvider, imgModel, searchBackend]);

  const dirty =
    caps.data != null &&
    ((imgProvider ?? "") !== caps.data.image.provider ||
      (imgModel ?? "") !== caps.data.image.model);

  const save = useMutation({
    mutationFn: () =>
      putImageCapability({
        provider: (imgProvider ?? "").trim(),
        model: (imgModel ?? "").trim(),
      }),
    onSuccess: async () => {
      toast.success(t("modelHub.capabilities.saved", "Image binding saved"));
      await qc.invalidateQueries({ queryKey: ["admin", "models"] });
      setImgProvider(null);
      setImgModel(null);
    },
    onError: (err: Error) =>
      toast.error(t("modelHub.capabilities.saveFailed", "Save failed"), {
        description: err.message,
      }),
  });

  const searchDirty =
    caps.data != null &&
    ((searchBackend ?? "") !== caps.data.search.backend || searchKey !== null);

  const saveSearch = useMutation({
    mutationFn: () =>
      putSearchCapability({
        backend: (searchBackend ?? "").trim(),
        // Omitted entirely when untouched — sending "" would delete the
        // stored key on every unrelated save.
        ...(searchKey === null ? {} : { api_key: searchKey.trim() }),
      }),
    onSuccess: async () => {
      toast.success(t("modelHub.capabilities.searchSaved", "Search binding saved"));
      await qc.invalidateQueries({ queryKey: ["admin", "models"] });
      setSearchBackend(null);
      setSearchKey(null);
    },
    onError: (err: Error) =>
      toast.error(t("modelHub.capabilities.saveFailed", "Save failed"), {
        description: err.message,
      }),
  });

  if (caps.isLoading) {
    return <Skeleton className="h-48 w-full" data-testid="capabilities-skeleton" />;
  }
  if (!caps.data) {
    return (
      <p className="text-sm text-sg-ink-3" data-testid="capabilities-error">
        {t("modelHub.capabilities.loadFailed", "Could not load capability bindings.")}
      </p>
    );
  }

  const voiceBackend = backends.data?.backends.find(
    (b) => b.id === caps.data!.voice.backend,
  );

  return (
    <div className="space-y-4" data-testid="capabilities-section">
      <p className="max-w-2xl text-sm text-sg-ink-2">
        {t(
          "modelHub.capabilities.intro",
          "Which model serves each capability. A persona binding always wins over these defaults; these win over the chat provider.",
        )}
      </p>

      {/* ── Chat ─────────────────────────────────────────────────── */}
      <CapabilityCard
        icon={<Zap className="h-4 w-4 text-sg-ink-3" aria-hidden="true" />}
        title={t("modelHub.capabilities.text", "Chat")}
        value={caps.data.text.model || t("modelHub.capabilities.unset", "not set")}
        testId="capability-text"
        action={
          <Link
            href={{ pathname: "/models", query: { tab: "routing" } }}
            className="inline-flex items-center gap-1 text-xs text-sg-tint hover:underline"
          >
            {t("modelHub.capabilities.editInRouting", "Edit in Routing")}
            <ArrowUpRight className="h-3 w-3" aria-hidden="true" />
          </Link>
        }
      />

      {/* ── Image ────────────────────────────────────────────────── */}
      <div
        className="rounded-sg-md border border-sg-border bg-sg-card p-4"
        data-testid="capability-image"
      >
        <div className="mb-3 flex items-center gap-2">
          <ImageIcon className="h-4 w-4 text-sg-ink-3" aria-hidden="true" />
          <span className="text-sm text-sg-ink">
            {t("modelHub.capabilities.image", "Image generation")}
          </span>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="cap-image-provider">
              {t("modelHub.capabilities.provider", "Provider")}
            </Label>
            <select
              id="cap-image-provider"
              value={imgProvider ?? ""}
              onChange={(e) => setImgProvider(e.target.value)}
              className="h-10 w-full rounded-sg-sm border border-sg-border bg-sg-inset px-3 text-sm text-sg-ink"
              data-testid="capability-image-provider"
            >
              <option value="">
                {t("modelHub.capabilities.inheritChat", "(use the chat provider)")}
              </option>
              {caps.data.image.capable_providers.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
              {/* Keep an unlisted binding visible instead of silently resetting it. */}
              {imgProvider &&
              !caps.data.image.capable_providers.includes(imgProvider) ? (
                <option value={imgProvider}>
                  {imgProvider}
                  {t("modelHub.capabilities.unlisted", " (unlisted)")}
                </option>
              ) : null}
            </select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="cap-image-model">
              {t("modelHub.capabilities.model", "Model")}
            </Label>
            <Input
              id="cap-image-model"
              list="cap-image-model-options"
              value={imgModel ?? ""}
              onChange={(e) => setImgModel(e.target.value)}
              placeholder={t(
                "modelHub.capabilities.imageModelPlaceholder",
                "e.g. gpt-image-2",
              )}
              data-testid="capability-image-model"
            />
            <datalist id="cap-image-model-options">
              {caps.data.aliases.map((a) => (
                <option key={a} value={a} />
              ))}
            </datalist>
          </div>
        </div>
        <div className="mt-3 flex items-center justify-between gap-3">
          <p className="text-xs text-sg-ink-3">
            {t(
              "modelHub.capabilities.imageHint",
              "Leave both blank to fall back to the chat provider, as before.",
            )}
          </p>
          <Button
            size="sm"
            onClick={() => save.mutate()}
            disabled={!dirty || save.isPending}
            data-testid="capability-image-save"
          >
            <Save className="h-4 w-4" aria-hidden="true" />
            {t("modelHub.capabilities.save", "Save")}
          </Button>
        </div>
      </div>

      {/* ── Web search ───────────────────────────────────────────── */}
      <div
        className="rounded-sg-md border border-sg-border bg-sg-card p-4"
        data-testid="capability-search"
      >
        <div className="mb-3 flex items-center gap-2">
          <Search className="h-4 w-4 text-sg-ink-3" aria-hidden="true" />
          <span className="text-sm text-sg-ink">
            {t("modelHub.capabilities.search", "Web search")}
          </span>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="cap-search-backend">
              {t("modelHub.capabilities.backend", "Backend")}
            </Label>
            <select
              id="cap-search-backend"
              value={searchBackend ?? ""}
              onChange={(e) => setSearchBackend(e.target.value)}
              className="h-10 w-full rounded-sg-sm border border-sg-border bg-sg-inset px-3 text-sm text-sg-ink"
              data-testid="capability-search-backend"
            >
              <option value="">
                {t("modelHub.capabilities.searchDefault", "DuckDuckGo (no key)")}
              </option>
              {caps.data.search.backends.map((b) => (
                <option key={b} value={b}>
                  {b}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="cap-search-key">
              {t("modelHub.capabilities.apiKey", "API key")}
            </Label>
            <Input
              id="cap-search-key"
              type="password"
              value={searchKey ?? ""}
              onChange={(e) => setSearchKey(e.target.value)}
              placeholder={
                caps.data.search.api_key_set
                  ? t("modelHub.capabilities.keyStored", "configured — leave blank to keep")
                  : t("modelHub.capabilities.keyNone", "not set")
              }
              data-testid="capability-search-key"
            />
          </div>
        </div>
        <div className="mt-3 flex items-center justify-between gap-3">
          <p className="text-xs text-sg-ink-3">
            {t(
              "modelHub.capabilities.searchHint",
              "Without a key the agent scrapes DuckDuckGo HTML, which is rate-limited and can break.",
            )}
          </p>
          <Button
            size="sm"
            onClick={() => saveSearch.mutate()}
            disabled={!searchDirty || saveSearch.isPending}
            data-testid="capability-search-save"
          >
            <Save className="h-4 w-4" aria-hidden="true" />
            {t("modelHub.capabilities.save", "Save")}
          </Button>
        </div>
      </div>

      {/* ── Speech ───────────────────────────────────────────────── */}
      <CapabilityCard
        icon={<Mic className="h-4 w-4 text-sg-ink-3" aria-hidden="true" />}
        title={t("modelHub.capabilities.voice", "Speech")}
        value={
          caps.data.voice.backend
            ? [
                voiceBackend?.label ?? caps.data.voice.backend,
                caps.data.voice.model,
                caps.data.voice.voice,
              ]
                .filter(Boolean)
                .join(" · ")
            : t("modelHub.capabilities.unset", "not set")
        }
        muted={!caps.data.voice.enabled}
        testId="capability-voice"
        action={
          <Link
            href="/voice"
            className="inline-flex items-center gap-1 text-xs text-sg-tint hover:underline"
          >
            {t("modelHub.capabilities.editInVoice", "Edit & audition")}
            <ArrowUpRight className="h-3 w-3" aria-hidden="true" />
          </Link>
        }
      />
    </div>
  );
}

function CapabilityCard({
  icon,
  title,
  value,
  action,
  testId,
  muted = false,
}: {
  icon: React.ReactNode;
  title: string;
  value: string;
  action: React.ReactNode;
  testId: string;
  muted?: boolean;
}) {
  const { t } = useTranslation();
  return (
    <div
      className="flex items-center justify-between gap-4 rounded-sg-md border border-sg-border bg-sg-card p-4"
      data-testid={testId}
    >
      <div className="flex min-w-0 items-center gap-2">
        {icon}
        <div className="min-w-0">
          <p className="text-sm text-sg-ink">{title}</p>
          <p
            className={cn(
              "truncate font-mono text-xs",
              muted ? "text-sg-ink-4 line-through" : "text-sg-ink-3",
            )}
          >
            {value}
          </p>
        </div>
      </div>
      <div className="shrink-0">
        {muted ? (
          <span className="mr-3 text-xs text-sg-ink-4">
            {t("modelHub.capabilities.disabled", "disabled")}
          </span>
        ) : null}
        {action}
      </div>
    </div>
  );
}
