"use client";

/**
 * /chat/[sessionKey] — concrete conversation view. Sidebar still on the
 * left (now with the active row highlighted), `<ChatArea>` on the right.
 */

import * as React from "react";
import { useParams, useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  deleteChatSession,
  listChatSessions,
  patchChatSession,
} from "@/lib/api/chat";
import { CorlinmanApiError } from "@/lib/api";
import { replaySession, type TranscriptMessage } from "@/lib/api/sessions";
import { ChatArea } from "@/components/chat/chat-area";
import { ChatSidebar } from "@/components/chat/chat-sidebar";
import type { ChatConversation, ChatMessage } from "@/lib/chat/types";

const DEFAULT_MODEL = "gpt-4o";

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

/** Convert a journal TranscriptMessage into a ChatMessage so existing
 *  sessions can be resumed in the /chat surface. The journal only
 *  surfaces flattened user/assistant/system text (tool calls are
 *  encoded inside assistant content); for resume we just need enough
 *  context to keep the conversation coherent. */
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

function genSessionKey(): string {
  const r = Math.random().toString(36).slice(2, 10);
  return `corlinman:${Date.now().toString(36)}:${r}`;
}

export default function ChatSessionPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const params = useParams<{ sessionKey: string }>();
  const sessionKey = decodeURIComponent(params.sessionKey ?? "");
  const [collapsed, setCollapsed] = React.useState(false);

  const { data: conversations } = useQuery<ChatConversation[]>({
    queryKey: ["chat", "sessions"],
    queryFn: listChatSessions,
    refetchInterval: 30_000,
  });

  const active = React.useMemo(
    () => conversations?.find((c) => c.sessionKey === sessionKey) ?? null,
    [conversations, sessionKey],
  );

  // Pick up branched history (set by the branch action on a previous page)
  // exactly once per sessionKey mount.
  const [branchedHistory, setBranchedHistory] = React.useState<
    ChatMessage[] | undefined
  >(undefined);
  React.useEffect(() => {
    const h = pickBranchedHistory(sessionKey);
    if (h && h.length > 0) setBranchedHistory(h);
  }, [sessionKey]);

  // Fetch on-server transcript when no branched history is staged. Allows
  // operators to click into any existing session (telegram/qq/web) and
  // keep talking. Stable on the sessionKey, so navigating between rows
  // refetches per row.
  const transcriptQuery = useQuery({
    queryKey: ["chat", "transcript", sessionKey],
    queryFn: async () => {
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
    router.push(`/chat/${encodeURIComponent(key)}`);
  }, [router]);

  const handleRename = React.useCallback(
    async (key: string, title: string) => {
      try {
        await patchChatSession(key, { title: title || null });
        refreshList();
      } catch (err) {
        toast.error(
          err instanceof CorlinmanApiError ? err.message : "Rename failed",
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
          if (key === sessionKey) {
            router.push("/chat");
          }
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
      <ChatArea
        sessionKey={sessionKey}
        model={DEFAULT_MODEL}
        conversation={active}
        initialHistory={initialHistory}
      />
    </>
  );
}
