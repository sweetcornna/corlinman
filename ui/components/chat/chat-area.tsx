"use client";

/**
 * The right pane of the chat surface. Header (title + model/persona) +
 * message list + composer. Owns the live chat hook per `sessionKey`.
 */

import * as React from "react";

import { cn } from "@/lib/utils";
import { Composer } from "@/components/chat/composer";
import { ChatEmptyState } from "@/components/chat/empty-state";
import { MessageList } from "@/components/chat/message-list";
import { useChatStream } from "@/lib/chat/use-chat-stream";
import type { ChatMessage, ChatConversation } from "@/lib/chat/types";

interface ChatAreaProps {
  sessionKey: string;
  model: string;
  conversation?: ChatConversation | null;
  initialHistory?: ChatMessage[];
  agentId?: string;
  personaId?: string;
  personaLabel?: string;
  onOpenModelPicker?: () => void;
  onOpenPersonaPicker?: () => void;
}

export function ChatArea({
  sessionKey,
  model,
  conversation,
  initialHistory,
  agentId,
  personaId,
  personaLabel,
  onOpenModelPicker,
  onOpenPersonaPicker,
}: ChatAreaProps) {
  const chat = useChatStream({
    sessionKey,
    model,
    agentId,
    personaId,
  });

  // Hydrate the hook from server history once per sessionKey.
  const hydratedKeyRef = React.useRef<string | null>(null);
  React.useEffect(() => {
    if (hydratedKeyRef.current === sessionKey) return;
    if (initialHistory && initialHistory.length > 0) {
      chat.hydrate(initialHistory);
    } else {
      chat.hydrate([]);
    }
    hydratedKeyRef.current = sessionKey;
  }, [sessionKey, initialHistory, chat]);

  const handlePickSuggestion = React.useCallback(
    (text: string) => {
      void chat.sendMessage(text);
    },
    [chat],
  );

  const title =
    conversation?.title ??
    (chat.messages[0]?.role === "user"
      ? chat.messages[0].content.slice(0, 60)
      : "New conversation");

  return (
    <section className="flex h-full flex-1 flex-col" data-testid="chat-area">
      <header className="flex items-center justify-between border-b border-tp-glass-edge bg-tp-glass-inner/30 px-4 py-2">
        <div className="flex min-w-0 flex-col">
          <h1 className="truncate text-[13px] font-medium text-tp-ink">
            {title}
          </h1>
          <p className="font-mono text-[10px] text-tp-ink-3">
            {sessionKey}
          </p>
        </div>
        <div className="flex items-center gap-2 text-[11px] text-tp-ink-3">
          <span className="inline-flex items-center gap-1 rounded border border-tp-glass-edge bg-tp-glass-inner px-1.5 py-0.5">
            <span className={cn("h-1.5 w-1.5 rounded-full", chat.isStreaming ? "animate-pulse bg-tp-amber" : "bg-tp-ok")} aria-hidden="true" />
            {chat.isStreaming ? "Streaming" : "Idle"}
          </span>
        </div>
      </header>

      <div className="flex-1 overflow-hidden">
        <MessageList
          messages={chat.messages}
          pendingMessage={chat.pendingMessage}
          onRegenerate={chat.retryLast}
          onApprove={chat.approve}
          emptyState={<ChatEmptyState onPick={handlePickSuggestion} />}
        />
      </div>

      <Composer
        isStreaming={chat.isStreaming}
        modelLabel={model}
        personaLabel={personaLabel}
        onSend={(text, attachments) => {
          void chat.sendMessage(text, attachments);
        }}
        onStop={() => {
          void chat.stop();
        }}
        onOpenModelPicker={onOpenModelPicker}
        onOpenPersonaPicker={onOpenPersonaPicker}
        onSlashClear={() => chat.hydrate([])}
      />
    </section>
  );
}
