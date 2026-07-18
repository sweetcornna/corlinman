"use client";

/**
 * `/admin/chat` — single page that renders the conversation sidebar +
 * either an empty state (no `?session=` query param) or the full
 * `<ChatArea>` once a session is selected.
 *
 * Uses a query string instead of a dynamic route segment because
 * `next.config.ts` ships `output: "export"`, which forbids arbitrary
 * dynamic paths without a `generateStaticParams()` enumeration — mirrors
 * the pattern used by `/admin/sessions/detail`.
 */

import * as React from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useTranslation } from "react-i18next";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  deleteChatSession,
  listChatSessions,
  patchChatSession,
  type ReasoningEffort,
} from "@/lib/api/chat";
import { isReasoningTier } from "@/lib/chat/reasoning-effort";
import {
  CorlinmanApiError,
  fetchModels,
  listAgentBindings,
  type AgentBinding,
  type AgentBindingsResponse,
} from "@/lib/api";
import { replaySession, type TranscriptMessage } from "@/lib/api/sessions";
import { transcriptToChatMessages } from "@/lib/chat/transcript";
import { ChatModelPicker, type ModelPickerKind } from "@/components/chat/chat-model-picker";
import { ChatArea } from "@/components/chat/chat-area";
import { ChatLiveAgents } from "@/components/chat/chat-live-agents";
import { ChatSidebar } from "@/components/chat/chat-sidebar";
import { ChatEmptyState } from "@/components/chat/empty-state";
import type { ChatConversation, ChatMessage } from "@/lib/chat/types";

const FALLBACK_MODEL = "gpt-4o"; // used only when /admin/models returns no global default
const DEFAULT_REASONING_EFFORT: ReasoningEffort = "medium";

function isReasoningEffort(value: string | null): value is ReasoningEffort {
  return value !== null && isReasoningTier(value);
}

function genSessionKey(): string {
  const r = Math.random().toString(36).slice(2, 10);
  return `corlinman:${Date.now().toString(36)}:${r}`;
}

function chatHref(sessionKey: string): string {
  return `/chat?session=${encodeURIComponent(sessionKey)}`;
}

type ModelAliasMetadata = {
  provider: string | null;
  target: string | null;
  /** Effort ladder for the resolved model — `null` = unknown family
   *  (fall back to client heuristics), `[]` = no effort knob. */
  reasoningTiers: string[] | null;
};

function modelAliasMetadataFromAliases(
  data: unknown,
  model: string,
): ModelAliasMetadata {
  const aliases = (data as { aliases?: unknown } | null | undefined)?.aliases;
  if (Array.isArray(aliases)) {
    const match = aliases.find((row) => {
      const alias = row as { name?: unknown };
      return typeof alias.name === "string" && alias.name === model;
    }) as
      | {
          provider?: unknown;
          model?: unknown;
          reasoning_tiers?: unknown;
          reasoning_default?: unknown;
        }
      | undefined;
    const rawTiers = match?.reasoning_tiers;
    return {
      provider:
        typeof match?.provider === "string" && match.provider.trim()
          ? match.provider
          : null,
      target:
        typeof match?.model === "string" && match.model.trim()
          ? match.model
          : null,
      reasoningTiers: Array.isArray(rawTiers)
        ? rawTiers.filter((t): t is string => typeof t === "string")
        : null,
    };
  }
  if (aliases && typeof aliases === "object") {
    const target = (aliases as Record<string, unknown>)[model];
    return {
      provider: null,
      target: typeof target === "string" && target.trim() ? target : null,
      reasoningTiers: null,
    };
  }
  return {
    provider: null,
    target: null,
    reasoningTiers: null,
  };
}

function pickBranchedHistory(sessionKey: string): ChatMessage[] | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = sessionStorage.getItem(`corlinman:chat:branch:${sessionKey}`);
    if (!raw) return null;
    sessionStorage.removeItem(`corlinman:chat:branch:${sessionKey}`);
    return JSON.parse(raw) as ChatMessage[];
  } catch {
    return null;
  }
}

export default function ChatPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const search = useSearchParams();
  const { t } = useTranslation();
  const sessionKey = search?.get("session") ?? null;
  const [collapsed, setCollapsed] = React.useState(false);

  const { data: conversations } = useQuery<ChatConversation[]>({
    queryKey: ["chat", "sessions"],
    queryFn: listChatSessions,
    refetchInterval: 30_000,
  });

  // Resolve the live global-default model (the same alias /admin/models
  // surfaces as ``models.default`` in the gateway config). Picked up live so
  // operators editing the default in /admin/models see it reflected on the
  // next composer turn without reloading the chat page.
  const { data: modelsData } = useQuery({
    queryKey: ["chat", "default-model"],
    queryFn: fetchModels,
    staleTime: 60_000,
  });
  const globalDefault: string =
    (modelsData?.default && modelsData.default.trim()) || FALLBACK_MODEL;

  // Per-operator model overrides — persisted to localStorage so the
  // selection survives reloads / tab switches. An empty/unset override
  // means "follow the upstream default" (LLM = global default, image =
  // gpt-image-2). The composer pills + the popover picker both write
  // through these setters.
  const [llmOverride, setLlmOverride] = React.useState<string | null>(null);
  const [imageOverride, setImageOverride] = React.useState<string | null>(null);
  const [activeAgent, setActiveAgent] = React.useState<string | null>(null);
  const [reasoningEffort, setReasoningEffort] =
    React.useState<ReasoningEffort>(DEFAULT_REASONING_EFFORT);
  React.useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      setLlmOverride(localStorage.getItem("corlinman:chat:llm-model"));
      setImageOverride(localStorage.getItem("corlinman:chat:image-model"));
      setActiveAgent(localStorage.getItem("corlinman:chat:agent-id"));
      const savedReasoning = localStorage.getItem(
        "corlinman:chat:reasoning-effort",
      );
      if (isReasoningEffort(savedReasoning)) {
        setReasoningEffort(savedReasoning);
      }
    } catch {
      /* ignore */
    }
  }, []);
  const persistOverride = React.useCallback(
    (key: "llm-model" | "image-model", value: string) => {
      try {
        localStorage.setItem(`corlinman:chat:${key}`, value);
      } catch {
        /* ignore */
      }
    },
    [],
  );
  const persistAgent = React.useCallback((agentId: string | null) => {
    setActiveAgent(agentId);
    try {
      if (agentId) {
        localStorage.setItem("corlinman:chat:agent-id", agentId);
      } else {
        localStorage.removeItem("corlinman:chat:agent-id");
      }
    } catch {
      /* ignore */
    }
  }, []);
  const persistReasoningEffort = React.useCallback((effort: ReasoningEffort) => {
    setReasoningEffort(effort);
    try {
      localStorage.setItem("corlinman:chat:reasoning-effort", effort);
    } catch {
      /* ignore */
    }
  }, []);

  const activeModel: string =
    (llmOverride && llmOverride.trim()) || globalDefault;
  const activeModelMetadata = modelAliasMetadataFromAliases(
    modelsData,
    activeModel,
  );
  const activeImageModel: string =
    (imageOverride && imageOverride.trim()) || "gpt-image-2";

  const bindingsQuery = useQuery<AgentBindingsResponse>({
    queryKey: ["admin", "agent-bindings"],
    queryFn: () => listAgentBindings(),
    staleTime: 30_000,
  });

  const bindingByName = React.useMemo(() => {
    const m = new Map<string, AgentBinding>();
    for (const b of bindingsQuery.data?.agents ?? []) {
      m.set(b.name, b);
    }
    return m;
  }, [bindingsQuery.data]);

  const showActionTrace = activeAgent
    ? (bindingByName.get(activeAgent)?.show_action_trace ?? true)
    : true;

  const [pickerOpen, setPickerOpen] = React.useState<ModelPickerKind | null>(
    null,
  );

  const active = React.useMemo(
    () =>
      sessionKey
        ? (conversations?.find((c) => c.sessionKey === sessionKey) ?? null)
        : null,
    [conversations, sessionKey],
  );

  // Branched history handoff (one-shot per sessionKey).
  const [branchedHistory, setBranchedHistory] = React.useState<
    ChatMessage[] | undefined
  >(undefined);
  React.useEffect(() => {
    if (!sessionKey) {
      setBranchedHistory(undefined);
      return;
    }
    const h = pickBranchedHistory(sessionKey);
    if (h && h.length > 0) setBranchedHistory(h);
  }, [sessionKey]);

  // Fetch server-side transcript when no branched history is staged.
  const transcriptQuery = useQuery({
    queryKey: ["chat", "transcript", sessionKey ?? ""],
    queryFn: async () => {
      if (!sessionKey) return null;
      const out = await replaySession(sessionKey, { mode: "transcript" });
      if (out.kind === "ok") return out.replay;
      return null;
    },
    enabled: Boolean(sessionKey) && branchedHistory === undefined,
    staleTime: 30_000,
  });

  // W5 — older pages loaded via the "load earlier" affordance, oldest
  // first. Reset whenever the session changes.
  const [earlierMessages, setEarlierMessages] = React.useState<
    TranscriptMessage[]
  >([]);
  const [earlierCursor, setEarlierCursor] = React.useState<string | null>(
    null,
  );
  const [loadingEarlier, setLoadingEarlier] = React.useState(false);
  React.useEffect(() => {
    setEarlierMessages([]);
    setEarlierCursor(null);
    setLoadingEarlier(false);
  }, [sessionKey]);

  // The active cursor / has-more come from the OLDEST page we hold —
  // the manual pages once any were loaded, else the base query's.
  const baseReplay = transcriptQuery.data ?? null;
  const hasEarlier = Boolean(
    earlierCursor !== null
      ? earlierCursor
      : baseReplay?.has_more && baseReplay?.oldest_turn_id,
  );

  const handleLoadEarlier = React.useCallback(async () => {
    if (!sessionKey || loadingEarlier) return;
    const cursor = earlierCursor ?? baseReplay?.oldest_turn_id;
    if (!cursor) return;
    setLoadingEarlier(true);
    try {
      const out = await replaySession(sessionKey, {
        mode: "transcript",
        beforeTurnId: cursor,
        limit: 200,
      });
      if (out.kind === "ok") {
        setEarlierMessages((prev) => [...out.replay.transcript, ...prev]);
        setEarlierCursor(
          out.replay.has_more ? (out.replay.oldest_turn_id ?? null) : null,
        );
        if (!out.replay.has_more) {
          // Sentinel: cursor consumed, nothing older — collapse the
          // affordance by leaving earlierCursor null AND covering the
          // base query's has_more via earlier pages being present.
          setEarlierCursor(null);
        }
      }
    } catch (err) {
      toast.error(err instanceof CorlinmanApiError ? err.message : String(err));
    } finally {
      setLoadingEarlier(false);
    }
  }, [sessionKey, loadingEarlier, earlierCursor, baseReplay]);

  // Once manual pages exist, "has earlier" is solely the manual cursor.
  const effectiveHasEarlier =
    earlierMessages.length > 0 ? earlierCursor !== null : hasEarlier;

  const initialHistory: ChatMessage[] | undefined = React.useMemo(() => {
    if (branchedHistory && branchedHistory.length > 0) return branchedHistory;
    const t = transcriptQuery.data;
    if (!t) return undefined;
    return transcriptToChatMessages(
      [...earlierMessages, ...t.transcript],
      sessionKey ?? "",
    );
  }, [branchedHistory, transcriptQuery.data, earlierMessages]);

  const refreshList = React.useCallback(() => {
    void qc.invalidateQueries({ queryKey: ["chat", "sessions"] });
  }, [qc]);

  const handleNew = React.useCallback(() => {
    const key = genSessionKey();
    router.push(chatHref(key));
  }, [router]);

  const handlePickSuggestion = React.useCallback(
    (text: string) => {
      const key = genSessionKey();
      router.push(`${chatHref(key)}&prompt=${encodeURIComponent(text)}`);
    },
    [router],
  );

  const handleRename = React.useCallback(
    async (key: string, title: string) => {
      try {
        await patchChatSession(key, { title: title || null });
        refreshList();
      } catch (err) {
        toast.error(
          err instanceof CorlinmanApiError
            ? t("common.saveFailed") + ": " + err.message
            : t("common.saveFailed"),
        );
      }
    },
    [refreshList, t],
  );

  const handleTogglePin = React.useCallback(
    async (key: string) => {
      const conv = conversations?.find((c) => c.sessionKey === key);
      if (!conv) return;
      try {
        await patchChatSession(key, { pinned: !conv.pinned });
        refreshList();
      } catch (err) {
        toast.error(
          err instanceof CorlinmanApiError ? err.message : t("common.saveFailed"),
        );
      }
    },
    [conversations, refreshList, t],
  );

  const handleToggleArchive = React.useCallback(
    async (key: string) => {
      const conv = conversations?.find((c) => c.sessionKey === key);
      if (!conv) return;
      try {
        await patchChatSession(key, { archived: !conv.archived });
        refreshList();
      } catch (err) {
        toast.error(
          err instanceof CorlinmanApiError ? err.message : t("common.saveFailed"),
        );
      }
    },
    [conversations, refreshList, t],
  );

  const handleDelete = React.useCallback(
    (key: string) => {
      // Per-key pending registry so rapid deletes don't race each other and
      // an undo only ever cancels *its own* timer. Without this, undoing one
      // conversation could refresh the list while another delete is still
      // in flight (or, with repeated keys, leak a timer) — surfacing as a
      // resurrected/half-deleted row. Stored on `window` so it survives
      // re-renders without widening this callback's responsibilities.
      type PendingDelete = { timer: number; cancelled: boolean };
      const w = window as typeof window & {
        __chatPendingDeletes?: Map<string, PendingDelete>;
      };
      const pending = (w.__chatPendingDeletes ??= new Map());

      // Collapse a duplicate delete of an already-pending key: cancel the
      // prior timer and start fresh so the 4.5s window restarts cleanly.
      const prior = pending.get(key);
      if (prior) {
        prior.cancelled = true;
        window.clearTimeout(prior.timer);
        pending.delete(key);
      }

      const entry: PendingDelete = { timer: 0, cancelled: false };
      entry.timer = window.setTimeout(async () => {
        // Claim ownership: only proceed if this entry is still the live one
        // for `key` and hasn't been undone.
        if (entry.cancelled || pending.get(key) !== entry) return;
        pending.delete(key);
        try {
          await deleteChatSession(key);
          if (entry.cancelled) return; // undone while the request was in flight
          refreshList();
          if (key === sessionKey) router.push("/chat");
        } catch (err) {
          toast.error(
            err instanceof CorlinmanApiError ? err.message : t("common.saveFailed"),
          );
        }
      }, 4500);
      pending.set(key, entry);

      toast(t("chat.deletedToast"), {
        action: {
          label: t("chat.undo"),
          onClick: () => {
            entry.cancelled = true;
            window.clearTimeout(entry.timer);
            // Only clear the registry slot if we still own it.
            if (pending.get(key) === entry) pending.delete(key);
            refreshList();
          },
        },
        duration: 4500,
      });
    },
    [refreshList, router, sessionKey, t],
  );

  return (
    <>
      <ChatSidebar
        conversations={conversations ?? []}
        activeSessionKey={sessionKey}
        onNew={handleNew}
        onRename={handleRename}
        onTogglePin={handleTogglePin}
        onToggleArchive={handleToggleArchive}
        onDelete={handleDelete}
        collapsed={collapsed}
        onToggleCollapsed={() => setCollapsed((v) => !v)}
      />
      {sessionKey ? (
        <ChatArea
          sessionKey={sessionKey}
          model={activeModel}
          conversation={active}
          initialHistory={initialHistory}
          agentId={activeAgent ?? undefined}
          onAgentChange={persistAgent}
          reasoningEffort={reasoningEffort}
          onReasoningEffortChange={persistReasoningEffort}
          modelProvider={activeModelMetadata.provider}
          modelTarget={activeModelMetadata.target}
          reasoningTiers={activeModelMetadata.reasoningTiers}
          showActionTrace={showActionTrace}
          onOpenModelPicker={() => setPickerOpen("llm")}
          hasEarlier={effectiveHasEarlier}
          loadingEarlier={loadingEarlier}
          onLoadEarlier={() => {
            void handleLoadEarlier();
          }}
        />
      ) : null}
      {sessionKey ? <ChatLiveAgents sessionKey={sessionKey} /> : null}
      {!sessionKey ? (
        <section
          className="flex flex-1 items-center justify-center overflow-hidden rounded-xl border border-sg-border bg-sg-card p-6 shadow-sg-2"
          data-testid="chat-empty-pane"
        >
          <ChatEmptyState onPick={handlePickSuggestion} />
        </section>
      ) : null}
      <ChatModelPicker
        open={pickerOpen !== null}
        onClose={() => setPickerOpen(null)}
        kind={pickerOpen ?? "llm"}
        current={pickerOpen === "image" ? activeImageModel : activeModel}
        onPick={(model) => {
          if (pickerOpen === "image") {
            setImageOverride(model);
            persistOverride("image-model", model);
          } else {
            setLlmOverride(model);
            persistOverride("llm-model", model);
          }
        }}
      />
    </>
  );
}
