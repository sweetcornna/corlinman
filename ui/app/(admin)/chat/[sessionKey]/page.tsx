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
import { ChatArea } from "@/components/chat/chat-area";
import { ChatSidebar } from "@/components/chat/chat-sidebar";
import type { ChatConversation, ChatMessage } from "@/lib/chat/types";

const DEFAULT_MODEL = "gpt-4o";

function pickBranchedHistory(sessionKey: string): ChatMessage[] | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = sessionStorage.getItem(`chat:branch:${sessionKey}`);
    if (!raw) return null;
    sessionStorage.removeItem(`chat:branch:${sessionKey}`);
    return JSON.parse(raw) as ChatMessage[];
  } catch {
    return null;
  }
}

function genSessionKey(): string {
  const r = Math.random().toString(36).slice(2, 10);
  return `web:${Date.now().toString(36)}:${r}`;
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
        initialHistory={branchedHistory}
      />
    </>
  );
}
