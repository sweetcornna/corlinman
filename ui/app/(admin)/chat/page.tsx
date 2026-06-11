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
import { i18next } from "@/lib/i18n";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  deleteChatSession,
  listChatSessions,
  patchChatSession,
} from "@/lib/api/chat";
import {
  CorlinmanApiError,
  GATEWAY_BASE_URL,
  fetchModels,
  listAgentBindings,
  type AgentBinding,
  type AgentBindingsResponse,
} from "@/lib/api";
import { replaySession, type TranscriptMessage } from "@/lib/api/sessions";
import { ChatModelPicker, type ModelPickerKind } from "@/components/chat/chat-model-picker";
import { ChatArea } from "@/components/chat/chat-area";
import { ChatSidebar } from "@/components/chat/chat-sidebar";
import { ChatEmptyState } from "@/components/chat/empty-state";
import type {
  ChatAttachment,
  ChatConversation,
  ChatMessage,
  ToolCallState,
} from "@/lib/chat/types";

const FALLBACK_MODEL = "gpt-4o"; // used only when /admin/models returns no global default

function genSessionKey(): string {
  const r = Math.random().toString(36).slice(2, 10);
  return `corlinman:${Date.now().toString(36)}:${r}`;
}

function chatHref(sessionKey: string): string {
  return `/chat?session=${encodeURIComponent(sessionKey)}`;
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

/** Map journal TranscriptMessage[] → ChatMessage[] so existing sessions
 *  (telegram / qq / scheduled) resume cleanly in /chat. Assistant rows
 *  with `tool_calls` rehydrate into ToolCallState[] so the bubble can
 *  render the historical tool invocations + their results (paired by
 *  the replay endpoint) — without this, tool-only assistant turns
 *  render as empty bubbles. */
function transcriptToChatMessages(
  transcript: TranscriptMessage[],
): ChatMessage[] {
  return transcript.map((m, i) => {
    // Identity must be deterministic across reloads AND stable when an
    // older page is PREPENDED (W5 "load earlier"): index from the END
    // of the list, so existing messages keep their ids as the list
    // grows upward. (The old `hist_${i}_${Date.now()-fallback}` baked a
    // load-time timestamp in, so every refetch re-keyed the whole list
    // and React rebuilt the DOM, losing scroll position.)
    const rid = transcript.length - i;
    const created = Number.isFinite(Date.parse(m.ts))
      ? Date.parse(m.ts)
      : Date.now() - (transcript.length - i) * 1000;
    const rawTcs = m.tool_calls ?? [];
    const toolCalls: ToolCallState[] = rawTcs.map((tc, j) => ({
      callId: tc.id?.trim() ? tc.id : `hist_r${rid}_${j}`,
      toolName: tc.function?.name ?? i18next.t("chat.unknownToolName"),
      argsJson: tc.function?.arguments ?? "{}",
      status: tc.result !== undefined ? "settled" : "ok",
      resultPreview: tc.result,
    }));
    // W3 — journaled attachment metadata → renderable cards. Relative
    // /v1/files urls get the gateway prefix so dev (separate origins)
    // and prod (same origin, empty prefix) both resolve.
    const attachments: ChatAttachment[] = (m.attachments ?? []).map(
      (a, k) => ({
        id: `hist_r${rid}_att_${k}`,
        kind:
          a.kind === "image" || a.kind === "audio" || a.kind === "video"
            ? a.kind
            : "document",
        name: a.name || a.url?.split("/").pop() || "attachment",
        mime: a.mime,
        sizeBytes: 0,
        remoteUrl: a.url
          ? a.url.startsWith("/")
            ? `${GATEWAY_BASE_URL}${a.url}`
            : a.url
          : undefined,
      }),
    );
    return {
      id: `hist_r${rid}_${m.role}`,
      role: m.role,
      content: m.content,
      createdAt: created,
      toolCalls: toolCalls.length > 0 ? toolCalls : undefined,
      attachments: attachments.length > 0 ? attachments : undefined,
    };
  });
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
  React.useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      setLlmOverride(localStorage.getItem("corlinman:chat:llm-model"));
      setImageOverride(localStorage.getItem("corlinman:chat:image-model"));
      setActiveAgent(localStorage.getItem("corlinman:chat:agent-id"));
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

  const activeModel: string =
    (llmOverride && llmOverride.trim()) || globalDefault;
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
    return transcriptToChatMessages([...earlierMessages, ...t.transcript]);
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
      let cancelled = false;
      const timer = window.setTimeout(async () => {
        if (cancelled) return;
        try {
          await deleteChatSession(key);
          refreshList();
          if (key === sessionKey) router.push("/chat");
        } catch (err) {
          toast.error(
            err instanceof CorlinmanApiError ? err.message : t("common.saveFailed"),
          );
        }
      }, 4500);
      toast(t("chat.deletedToast"), {
        action: {
          label: t("chat.undo"),
          onClick: () => {
            cancelled = true;
            window.clearTimeout(timer);
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
          showActionTrace={showActionTrace}
          onOpenModelPicker={() => setPickerOpen("llm")}
          hasEarlier={effectiveHasEarlier}
          loadingEarlier={loadingEarlier}
          onLoadEarlier={() => {
            void handleLoadEarlier();
          }}
        />
      ) : (
        <section
          className="flex flex-1 items-center justify-center overflow-hidden rounded-xl border border-sg-border bg-sg-card p-6 shadow-sg-2"
          data-testid="chat-empty-pane"
        >
          <ChatEmptyState onPick={handlePickSuggestion} />
        </section>
      )}
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
