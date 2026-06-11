"use client";

/**
 * Chat-side model picker — a lightweight popover-style dialog that lets
 * the operator override the chat composer's LLM or image model on the
 * fly. Two surfaces in one component:
 *
 *   kind="llm"   → defaults from `fetchModels().default` (global default
 *                  alias), lists every alias + every probed model from
 *                  every enabled provider.
 *   kind="image" → defaults to "gpt-image-2" (or the operator's stored
 *                  override), lists every provider that advertises
 *                  `image_capable` together with their `image_model`
 *                  config, plus probed models.
 *
 * Both surfaces accept a free-text input at the top so the operator can
 * type a model name that hasn't been registered as an alias / surfaced
 * by upstream — useful for new models the registry hasn't caught up to.
 */

import * as React from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Loader2, Search, X } from "lucide-react";

import { cn } from "@/lib/utils";
import {
  fetchModels,
  fetchProviders,
  getProviderModels,
  type ProviderView,
} from "@/lib/api";

export type ModelPickerKind = "llm" | "image";

export interface ChatModelPickerProps {
  open: boolean;
  onClose: () => void;
  kind: ModelPickerKind;
  current: string;
  onPick: (model: string) => void;
}

interface ModelOption {
  id: string;
  source: string; // "alias" | "probed:<provider>" | "default" | "fallback"
  hint?: string;
}

export function ChatModelPicker({
  open,
  onClose,
  kind,
  current,
  onPick,
}: ChatModelPickerProps) {
  const { t } = useTranslation();
  const [custom, setCustom] = React.useState("");
  const [filter, setFilter] = React.useState("");
  // Focus management: the dialog container (for the Tab trap), the search
  // input (initial focus target), and the element that was focused before
  // the dialog opened (restored on close).
  const dialogRef = React.useRef<HTMLDivElement>(null);
  const searchRef = React.useRef<HTMLInputElement>(null);
  const restoreFocusRef = React.useRef<Element | null>(null);
  // PERF-012: number of provider probes allowed to run so far. We probe
  // providers sequentially (one in-flight at a time) instead of fanning out
  // one concurrent HTTP probe per enabled provider the instant the popover
  // opens. Provider i's query is gated until provider i-1 has settled, which
  // advances this cursor. Starts at 1 so exactly one probe begins on open.
  const [probeCursor, setProbeCursor] = React.useState(0);

  React.useEffect(() => {
    if (open) {
      setCustom("");
      setFilter("");
      setProbeCursor(1);
    } else {
      setProbeCursor(0);
    }
  }, [open]);

  // Focus management: on open, remember the element that triggered the dialog
  // and move focus to the search input. On close, restore focus to the
  // trigger so keyboard users land back where they started.
  React.useEffect(() => {
    if (!open) return;
    restoreFocusRef.current =
      typeof document !== "undefined" ? document.activeElement : null;
    // Defer to after paint so the input is mounted and focusable.
    const id = window.requestAnimationFrame(() => {
      searchRef.current?.focus();
    });
    return () => {
      window.cancelAnimationFrame(id);
      const prev = restoreFocusRef.current;
      if (prev && prev instanceof HTMLElement) {
        prev.focus();
      }
      restoreFocusRef.current = null;
    };
  }, [open]);

  // Minimal hand-rolled focus trap + Escape-to-close. Keeps Tab / Shift+Tab
  // cycling among the focusable controls inside the dialog and never lets
  // focus escape to the page behind the modal.
  const onDialogKeyDown = React.useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key !== "Tab") return;
      const root = dialogRef.current;
      if (!root) return;
      const focusable = Array.from(
        root.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((el) => el.offsetParent !== null || el === document.activeElement);
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (e.shiftKey) {
        if (active === first || !root.contains(active)) {
          e.preventDefault();
          last.focus();
        }
      } else {
        if (active === last || !root.contains(active)) {
          e.preventDefault();
          first.focus();
        }
      }
    },
    [onClose],
  );

  // ── data ────────────────────────────────────────────────────────────
  const modelsQ = useQuery({
    queryKey: ["chat-picker", "models"],
    queryFn: fetchModels,
    staleTime: 60_000,
    enabled: open,
  });
  const providersQ = useQuery({
    queryKey: ["chat-picker", "providers"],
    queryFn: fetchProviders,
    staleTime: 60_000,
    enabled: open,
  });

  // For LLM: pull probed models for every enabled provider. For image:
  // only providers flagged as image_capable.
  const enabledProviders: ProviderView[] = React.useMemo(() => {
    const all = providersQ.data ?? [];
    return all.filter((p) =>
      kind === "image"
        ? p.enabled && Boolean(p.params?.image_capable)
        : p.enabled,
    );
  }, [providersQ.data, kind]);

  const probeQueries = useQueries({
    queries: enabledProviders.map((p, i) => ({
      queryKey: ["chat-picker", "provider-models", p.name],
      queryFn: () => getProviderModels(p.name),
      staleTime: 120_000,
      // Sequential gate: only providers whose index is below the cursor may
      // probe. Each settled probe advances the cursor (effect below), so at
      // most one probe is ever in flight rather than an N-at-once burst.
      enabled: open && i < probeCursor,
    })),
  });

  // Advance the probe cursor once the current in-flight provider probe has
  // settled (success or error). A query that is `enabled: false` reports
  // fetchStatus "idle" and is treated as not-yet-settled, so the cursor only
  // moves forward when the *enabled* leading probe finishes.
  const lastProbe =
    probeCursor > 0 ? probeQueries[probeCursor - 1] : undefined;
  const lastProbeSettled =
    lastProbe != null &&
    lastProbe.fetchStatus === "idle" &&
    (lastProbe.isSuccess || lastProbe.isError);
  React.useEffect(() => {
    if (!open) return;
    if (probeCursor >= enabledProviders.length) return;
    if (probeCursor > 0 && !lastProbeSettled) return;
    setProbeCursor((c) => Math.min(c + 1, enabledProviders.length));
  }, [open, probeCursor, enabledProviders.length, lastProbeSettled]);

  // ── option list ─────────────────────────────────────────────────────
  const options: ModelOption[] = React.useMemo(() => {
    const seen = new Set<string>();
    const out: ModelOption[] = [];
    const push = (id: string, source: string, hint?: string) => {
      const trimmed = (id ?? "").trim();
      if (!trimmed || seen.has(trimmed)) return;
      seen.add(trimmed);
      out.push({ id: trimmed, source, hint });
    };

    if (kind === "llm") {
      const def = modelsQ.data?.default ?? "";
      if (def) push(def, "default", t("chat.modelPicker.defaultBadge"));
      // Aliases from /admin/models
      const aliases = modelsQ.data?.aliases ?? {};
      for (const [name, model] of Object.entries(aliases)) {
        push(name, "alias", t("chat.modelPicker.aliasBadge"));
        // Also surface the underlying model id
        if (typeof model === "string") push(model, "alias-target");
      }
    } else {
      // image — start with the operator-supplied default + each provider's image_model
      push("gpt-image-2", "fallback", t("chat.modelPicker.defaultBadge"));
      for (const p of enabledProviders) {
        const im = p.params?.image_model;
        if (typeof im === "string" && im.trim())
          push(im, `provider:${p.name}`, p.name);
      }
    }

    // Probed upstream models
    probeQueries.forEach((q, i) => {
      const provider = enabledProviders[i];
      if (!provider) return;
      const list = q.data?.models ?? [];
      for (const m of list) {
        push(m.id, `probed:${provider.name}`, provider.name);
      }
    });

    return out;
  }, [kind, modelsQ.data, enabledProviders, probeQueries, t]);

  const filtered = React.useMemo(() => {
    const f = filter.trim().toLowerCase();
    if (!f) return options;
    return options.filter((o) =>
      o.id.toLowerCase().includes(f) ||
      (o.hint ?? "").toLowerCase().includes(f),
    );
  }, [options, filter]);

  // ── handlers ────────────────────────────────────────────────────────
  const submitCustom = React.useCallback(() => {
    const v = custom.trim();
    if (!v) return;
    onPick(v);
    onClose();
  }, [custom, onPick, onClose]);

  if (!open) return null;

  const isProbing = probeQueries.some((q) => q.isLoading);

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center p-4 sm:items-center"
      role="dialog"
      aria-modal="true"
      aria-label={
        kind === "llm"
          ? t("chat.modelPicker.titleLLM")
          : t("chat.modelPicker.titleImage")
      }
      data-testid="chat-model-picker"
      data-kind={kind}
      onKeyDown={onDialogKeyDown}
    >
      <div
        className="absolute inset-0 bg-black/40"
        onClick={onClose}
        aria-hidden="true"
      />
      <div
        ref={dialogRef}
        className={cn(
          "relative z-10 flex max-h-[80vh] w-full max-w-md flex-col overflow-hidden",
          "rounded-lg border border-sg-border bg-sg-inset shadow-xl",
        )}
      >
        <header className="flex items-center gap-2 border-b border-sg-border px-3 py-2">
          <span className="text-[13px] font-medium text-sg-ink">
            {kind === "llm"
              ? t("chat.modelPicker.titleLLM")
              : t("chat.modelPicker.titleImage")}
          </span>
          <span className="ml-auto rounded border border-sg-border bg-sg-inset px-1.5 py-0 font-mono text-[10px] text-sg-ink-3">
            {t("chat.modelPicker.currentBadge")}: {current || "—"}
          </span>
          <button
            type="button"
            onClick={onClose}
            className="rounded p-1 text-sg-ink-3 hover:bg-sg-inset hover:text-sg-ink"
            aria-label={t("common.close")}
          >
            <X className="h-3.5 w-3.5" aria-hidden="true" />
          </button>
        </header>

        <div className="flex flex-col gap-2 border-b border-sg-border px-3 py-2">
          <label className="text-[11px] text-sg-ink-3">
            {t("chat.modelPicker.customLabel")}
          </label>
          <div className="flex items-center gap-1.5">
            <input
              type="text"
              value={custom}
              onChange={(e) => setCustom(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  submitCustom();
                }
              }}
              placeholder={t("chat.modelPicker.customPlaceholder")}
              className="flex-1 rounded border border-sg-border bg-sg-inset px-2 py-1 text-[12px] text-sg-ink placeholder:text-sg-ink-3 focus:border-sg-accent focus:outline-none"
              data-testid="chat-model-picker-custom-input"
            />
            <button
              type="button"
              onClick={submitCustom}
              disabled={!custom.trim()}
              className="rounded border border-sg-accent/60 bg-sg-accent/20 px-2 py-1 text-[12px] text-sg-ink hover:bg-sg-accent/30 disabled:cursor-not-allowed disabled:opacity-40"
              data-testid="chat-model-picker-custom-submit"
            >
              {t("chat.modelPicker.useCustom")}
            </button>
          </div>
        </div>

        <div className="flex items-center gap-1.5 border-b border-sg-border px-3 py-1.5">
          <Search className="h-3.5 w-3.5 text-sg-ink-3" aria-hidden="true" />
          <input
            ref={searchRef}
            type="search"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder={t("chat.modelPicker.filterPlaceholder")}
            className="flex-1 bg-transparent text-[12px] text-sg-ink placeholder:text-sg-ink-3 focus:outline-none"
            data-testid="chat-model-picker-filter"
          />
          {isProbing ? (
            <Loader2 className="h-3 w-3 animate-spin text-sg-ink-3" aria-hidden="true" />
          ) : null}
        </div>

        <ul
          className="flex-1 overflow-y-auto"
          aria-label={t("chat.modelPicker.listAriaLabel")}
          data-testid="chat-model-picker-list"
        >
          {filtered.length === 0 ? (
            <li className="px-3 py-6 text-center text-[12px] text-sg-ink-3">
              {modelsQ.isLoading || providersQ.isLoading
                ? t("common.loading")
                : t("chat.modelPicker.emptyList")}
            </li>
          ) : (
            filtered.map((o) => (
              <li key={`${o.id}:${o.source}`}>
                <button
                  type="button"
                  onClick={() => {
                    onPick(o.id);
                    onClose();
                  }}
                  className={cn(
                    "flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12px]",
                    o.id === current
                      ? "bg-sg-accent/20 text-sg-ink"
                      : "text-sg-ink hover:bg-sg-inset",
                  )}
                  data-testid="chat-model-picker-option"
                  data-model-id={o.id}
                >
                  <span className="font-mono">{o.id}</span>
                  {o.hint ? (
                    <span className="ml-auto rounded border border-sg-border px-1 py-0 font-mono text-[10px] text-sg-ink-3">
                      {o.hint}
                    </span>
                  ) : null}
                </button>
              </li>
            ))
          )}
        </ul>
      </div>
    </div>
  );
}
