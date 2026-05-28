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
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  deleteChatSession,
  listChatSessions,
  patchChatSession,
} from "@/lib/api/chat";
import { CorlinmanApiError, fetchModels } from "@/lib/api";
import { replaySession, type TranscriptMessage } from "@/lib/api/sessions";
import { ChatArea } from "@/components/chat/chat-area";
import { ChatSidebar } from "@/components/chat/chat-sidebar";
import { ChatEmptyState } from "@/components/chat/empty-state";
import type { ChatConversation, ChatMessage } from "@/lib/chat/types";

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
 *  (telegram / qq / scheduled) resume cleanly in /chat. */
function transcriptToChatMessages(
  transcript: TranscriptMessage[],
): ChatMessage[] {
  return transcript.map((m, i) => {
    const created = Number.isFinite(Date.parse(m.ts))
      ? Date.parse(m.ts)
      : Date.now() - (transcript.length - i) * 1000;
    return {
      id: `hist_${i}_${created}`,
      role: m.role,
      content: m.content,
      createdAt: created,
    };
  });
}

export default function ChatPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const search = useSearchParams();
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
  const activeModel: string =
    (modelsData?.default && modelsData.default.trim()) || FALLBACK_MODEL;

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
      if (!sessionKey) return [] as TranscriptMessage[];
      const out = await replaySession(sessionKey, { mode: "transcript" });
      if (out.kind === "ok") return out.replay.transcript;
      return [] as TranscriptMessage[];
    },
    enabled: Boolean(sessionKey) && branchedHistory === undefined,
    staleTime: 30_000,
  });

  const initialHistory: ChatMessage[] | undefined = React.useMemo(() => {
    if (branchedHistory && branchedHistory.length > 0) return branchedHistory;
    const t = transcriptQuery.data;
    if (!t) return undefined;
    return transcriptToChatMessages(t);
  }, [branchedHistory, transcriptQuery.data]);

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
            ? `Rename failed: ${err.message}`
            : "Rename failed",
        );
      }
    },
    [refreshList],
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
          err instanceof CorlinmanApiError ? err.message : "Pin toggle failed",
        );
      }
    },
    [conversations, refreshList],
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
          err instanceof CorlinmanApiError ? err.message : "Archive failed",
        );
      }
    },
    [conversations, refreshList],
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
            err instanceof CorlinmanApiError ? err.message : "Delete failed",
          );
        }
      }, 4500);
      toast(`Conversation deleted`, {
        action: {
          label: "Undo",
          onClick: () => {
            cancelled = true;
            window.clearTimeout(timer);
            refreshList();
          },
        },
        duration: 4500,
      });
    },
    [refreshList, router, sessionKey],
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
        />
      ) : (
        <section className="flex flex-1 items-center justify-center p-6">
          <ChatEmptyState onPick={handlePickSuggestion} />
        </section>
      )}
    </>
  );
}
