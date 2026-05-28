"use client";

/**
 * The right pane of the chat surface. Header (title + model/persona/cost) +
 * message list + composer. Owns the live chat hook per `sessionKey` and
 * the artifact panel.
 */

import * as React from "react";
import { useRouter } from "next/navigation";

import { cn } from "@/lib/utils";
import { ArtifactPanel } from "@/components/chat/artifact-panel";
import { Composer } from "@/components/chat/composer";
import { ChatEmptyState } from "@/components/chat/empty-state";
import { MessageList } from "@/components/chat/message-list";
import { useChatStream } from "@/lib/chat/use-chat-stream";
import {
  deriveArtifactKind,
  deriveArtifactTitle,
  useArtifacts,
} from "@/lib/chat/artifacts";
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

function genSessionKey(): string {
  const r = Math.random().toString(36).slice(2, 10);
  return `web:${Date.now().toString(36)}:${r}`;
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
  const router = useRouter();
  const chat = useChatStream({
    sessionKey,
    model,
    agentId,
    personaId,
  });
  const arts = useArtifacts();

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

  const handleEdit = React.useCallback(
    (messageId: string, newContent: string) => {
      void chat.editAndRerun(messageId, newContent);
    },
    [chat],
  );

  const handleBranch = React.useCallback(
    (messageId: string) => {
      const slice = chat.sliceUntil(messageId);
      const newKey = genSessionKey();
      // Stash the branched history under sessionStorage so the new page
      // can pick it up on mount. Cheap, no-backend pre-load.
      try {
        sessionStorage.setItem(
          `chat:branch:${newKey}`,
          JSON.stringify(slice),
        );
      } catch {
        /* ignore */
      }
      router.push(`/chat/${encodeURIComponent(newKey)}#branched=1`);
    },
    [chat, router],
  );

  const handleOpenArtifact = React.useCallback(
    (language: string, source: string) => {
      const messageId =
        chat.pendingMessage?.id ??
        chat.messages[chat.messages.length - 1]?.id ??
        "unknown";
      const id = `${messageId}:${language || "txt"}:${source.length}`;
      arts.open({
        id,
        kind: deriveArtifactKind(language),
        title: deriveArtifactTitle(language, source),
        language,
        source,
        messageId,
      });
    },
    [arts, chat.messages, chat.pendingMessage],
  );

  const title =
    conversation?.title ??
    (chat.messages[0]?.role === "user"
      ? chat.messages[0].content.slice(0, 60)
      : "New conversation");

  const { inputTokens, outputTokens, costUsd } = chat.totals;
  const hasUsage = inputTokens + outputTokens > 0 || costUsd > 0;

  return (
    <div className="flex h-full flex-1 min-w-0">
      <section className="flex h-full flex-1 flex-col min-w-0" data-testid="chat-area">
        <header className="flex items-center justify-between border-b border-tp-glass-edge bg-tp-glass-inner/30 px-4 py-2">
          <div className="flex min-w-0 flex-col">
            <h1 className="truncate text-[13px] font-medium text-tp-ink">
              {title}
            </h1>
            <p className="font-mono text-[10px] text-tp-ink-3">{sessionKey}</p>
          </div>
          <div className="flex items-center gap-2 text-[11px] text-tp-ink-3">
            {hasUsage ? (
              <span
                className="inline-flex items-center gap-1 rounded border border-tp-glass-edge bg-tp-glass-inner px-1.5 py-0.5 font-mono"
                data-testid="chat-totals"
                title={`${inputTokens} in · ${outputTokens} out · $${costUsd.toFixed(4)}`}
              >
                {inputTokens + outputTokens} tok · ${costUsd.toFixed(4)}
              </span>
            ) : null}
            <span className="inline-flex items-center gap-1 rounded border border-tp-glass-edge bg-tp-glass-inner px-1.5 py-0.5">
              <span
                className={cn(
                  "h-1.5 w-1.5 rounded-full",
                  chat.isStreaming ? "animate-pulse bg-tp-amber" : "bg-tp-ok",
                )}
                aria-hidden="true"
              />
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
            onEdit={handleEdit}
            onBranch={handleBranch}
            onOpenArtifact={handleOpenArtifact}
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

      <ArtifactPanel
        artifacts={arts.artifacts}
        activeId={arts.activeId}
        open={arts.panelOpen}
        onClose={arts.close}
        onSelect={arts.select}
        onRemove={arts.remove}
      />
    </div>
  );
}
