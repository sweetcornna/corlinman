"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { ArrowDown } from "lucide-react";

import { cn } from "@/lib/utils";
import type {
  ApprovalDecision,
  ApprovalScope,
  ChatMessage,
} from "@/lib/chat/types";
import { MessageBubble } from "@/components/chat/message-bubble";

interface MessageListProps {
  messages: ChatMessage[];
  pendingMessage: ChatMessage | null;
  onRegenerate?: () => void;
  onApprove?: (
    turnId: string,
    callId: string,
    decision: ApprovalDecision,
    scope: ApprovalScope,
  ) => void;
  onEdit?: (messageId: string, newContent: string) => void;
  onBranch?: (messageId: string) => void;
  onReply?: (messageId: string) => void;
  onOpenArtifact?: (language: string, source: string) => void;
  emptyState?: React.ReactNode;
}

const NEAR_BOTTOM_PX = 60;

export function MessageList({
  messages,
  pendingMessage,
  onRegenerate,
  onApprove,
  onEdit,
  onBranch,
  onReply,
  onOpenArtifact,
  emptyState,
}: MessageListProps) {
  const { t } = useTranslation();
  const scrollRef = React.useRef<HTMLDivElement | null>(null);
  const [pinned, setPinned] = React.useState(true);

  const all = React.useMemo(
    () => (pendingMessage ? [...messages, pendingMessage] : messages),
    [messages, pendingMessage],
  );

  React.useEffect(() => {
    if (!pinned) return;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [all, pinned]);

  const handleScroll = React.useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.clientHeight - el.scrollTop;
    setPinned(distance < NEAR_BOTTOM_PX);
  }, []);

  const jumpToBottom = React.useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    setPinned(true);
  }, []);

  if (all.length === 0 && emptyState) {
    return (
      <div
        className="flex h-full items-center justify-center px-6"
        data-testid="message-list-empty"
      >
        {emptyState}
      </div>
    );
  }

  return (
    <div className="h-full p-3 sm:p-4" data-testid="message-list-wrap">
      <div
        className={cn(
          "relative h-full overflow-hidden rounded-xl",
          "border border-tp-glass-edge bg-tp-glass shadow-tp-panel",
        )}
      >
        <div
          ref={scrollRef}
          onScroll={handleScroll}
          className="h-full overflow-y-auto px-4 py-4"
          data-testid="message-list"
          aria-live="polite"
        >
          <ol className="mx-auto flex max-w-3xl flex-col gap-4">
            {all.map((m) => (
              <MessageBubble
                key={m.id}
                message={m}
                onRegenerate={
                  m.role === "assistant" && !m.pending
                    ? onRegenerate
                    : undefined
                }
                onApprove={onApprove}
                onEdit={m.role === "user" ? onEdit : undefined}
                onBranch={onBranch}
                onReply={onReply}
                onOpenArtifact={onOpenArtifact}
              />
            ))}
          </ol>
        </div>

        {!pinned ? (
          <button
            type="button"
            onClick={jumpToBottom}
            className={cn(
              "absolute right-4 bottom-4 inline-flex items-center gap-1.5 rounded-full",
              "border border-tp-glass-edge bg-tp-glass-inner px-3 py-1.5",
              "text-[11px] text-tp-ink shadow-sm transition hover:bg-tp-glass-inner/80",
            )}
            aria-label={t("chat.jumpToLatestAriaLabel")}
            data-testid="jump-to-bottom"
          >
            <ArrowDown className="h-3.5 w-3.5" aria-hidden="true" />
            {t("chat.jumpToLatest")}
          </button>
        ) : null}
      </div>
    </div>
  );
}
