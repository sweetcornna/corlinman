"use client";

/**
 * /chat root page. Renders the conversation sidebar; the right pane shows
 * an empty state until a session is selected (or the user starts a new one,
 * which routes to /chat/[sessionKey]).
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  deleteChatSession,
  listChatSessions,
  patchChatSession,
} from "@/lib/api/chat";
import { CorlinmanApiError } from "@/lib/api";
import { ChatSidebar } from "@/components/chat/chat-sidebar";
import { ChatEmptyState } from "@/components/chat/empty-state";
import type { ChatConversation } from "@/lib/chat/types";

function genSessionKey(): string {
  const r = Math.random().toString(36).slice(2, 10);
  return `web:${Date.now().toString(36)}:${r}`;
}

export default function ChatPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const [collapsed, setCollapsed] = React.useState(false);

  const { data: conversations } = useQuery<ChatConversation[]>({
    queryKey: ["chat", "sessions"],
    queryFn: listChatSessions,
    refetchInterval: 30_000,
  });

  const handleNew = React.useCallback(() => {
    const key = genSessionKey();
    router.push(`/chat/${encodeURIComponent(key)}`);
  }, [router]);

  const refreshList = React.useCallback(() => {
    void qc.invalidateQueries({ queryKey: ["chat", "sessions"] });
  }, [qc]);

  const handleRename = React.useCallback(
    async (sessionKey: string, title: string) => {
      try {
        await patchChatSession(sessionKey, { title: title || null });
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
    async (sessionKey: string) => {
      const conv = conversations?.find((c) => c.sessionKey === sessionKey);
      if (!conv) return;
      try {
        await patchChatSession(sessionKey, { pinned: !conv.pinned });
        refreshList();
      } catch (err) {
        toast.error(
          err instanceof CorlinmanApiError
            ? err.message
            : "Pin toggle failed",
        );
      }
    },
    [conversations, refreshList],
  );

  const handleToggleArchive = React.useCallback(
    async (sessionKey: string) => {
      const conv = conversations?.find((c) => c.sessionKey === sessionKey);
      if (!conv) return;
      try {
        await patchChatSession(sessionKey, { archived: !conv.archived });
        refreshList();
      } catch (err) {
        toast.error(
          err instanceof CorlinmanApiError
            ? err.message
            : "Archive toggle failed",
        );
      }
    },
    [conversations, refreshList],
  );

  const handleDelete = React.useCallback(
    (sessionKey: string) => {
      // Optimistic deletion with undo. We schedule the real delete after
      // the toast's grace window; if the user clicks Undo we cancel.
      let cancelled = false;
      const timer = window.setTimeout(async () => {
        if (cancelled) return;
        try {
          await deleteChatSession(sessionKey);
          refreshList();
        } catch (err) {
          toast.error(
            err instanceof CorlinmanApiError
              ? err.message
              : "Delete failed",
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
    [refreshList],
  );

  return (
    <>
      <ChatSidebar
        conversations={conversations ?? []}
        activeSessionKey={null}
        onNew={handleNew}
        onRename={handleRename}
        onTogglePin={handleTogglePin}
        onToggleArchive={handleToggleArchive}
        onDelete={handleDelete}
        collapsed={collapsed}
        onToggleCollapsed={() => setCollapsed((v) => !v)}
      />
      <section className="flex flex-1 items-center justify-center p-6">
        <ChatEmptyState onPick={(text) => {
          // Picking a suggestion on the root page starts a new conversation
          // with that prompt pre-loaded via the URL hash.
          const key = genSessionKey();
          router.push(`/chat/${encodeURIComponent(key)}#prompt=${encodeURIComponent(text)}`);
        }} />
      </section>
    </>
  );
}
