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
  showActionTrace?: boolean;
  /** W5 — older history exists server-side; renders the load pill. */
  hasEarlier?: boolean;
  loadingEarlier?: boolean;
  onLoadEarlier?: () => void;
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
  showActionTrace = true,
  hasEarlier,
  loadingEarlier,
  onLoadEarlier,
}: MessageListProps) {
  const { t } = useTranslation();
  const scrollRef = React.useRef<HTMLDivElement | null>(null);
  const [pinned, setPinned] = React.useState(true);

  const all = React.useMemo(
    () => (pendingMessage ? [...messages, pendingMessage] : messages),
    [messages, pendingMessage],
  );

  // W5 scroll anchoring: when an older page is PREPENDED (first id
  // changed but the old first message still exists further down), keep
  // the viewport visually anchored by compensating for the height the
  // new content added above it.
  const prevFirstIdRef = React.useRef<string | null>(null);
  const prevScrollHeightRef = React.useRef(0);
  React.useLayoutEffect(() => {
    const el = scrollRef.current;
    const firstId = messages[0]?.id ?? null;
    const prevFirst = prevFirstIdRef.current;
    if (
      el &&
      prevFirst &&
      firstId !== prevFirst &&
      messages.some((m) => m.id === prevFirst)
    ) {
      el.scrollTop += el.scrollHeight - prevScrollHeightRef.current;
    }
    prevFirstIdRef.current = firstId;
    if (el) prevScrollHeightRef.current = el.scrollHeight;
  }, [messages]);

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
    <div className="relative h-full">
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="h-full overflow-y-auto py-5"
        data-testid="message-list"
        aria-live="polite"
      >
        <ol className="mx-auto flex w-full max-w-3xl flex-col gap-5 px-4">
          {hasEarlier ? (
            <li className="flex justify-center">
              <button
                type="button"
                onClick={onLoadEarlier}
                disabled={loadingEarlier}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-full border border-sg-border bg-sg-inset px-3 py-1",
                  "text-[11px] text-sg-ink-3 transition hover:bg-sg-inset-hover hover:text-sg-ink",
                  "disabled:cursor-default disabled:opacity-60",
                )}
                data-testid="load-earlier"
              >
                {loadingEarlier ? (
                  <span
                    className="h-2.5 w-2.5 animate-spin rounded-full border border-sg-ink-4 border-t-transparent"
                    aria-hidden="true"
                  />
                ) : null}
                {loadingEarlier
                  ? t("chat.loadingEarlier")
                  : t("chat.loadEarlier")}
              </button>
            </li>
          ) : null}
          {all.map((m, i) => (
            <MessageBubble
              key={m.id}
              message={m}
              isLatest={i === all.length - 1}
              onRegenerate={
                m.role === "assistant" && !m.pending ? onRegenerate : undefined
              }
              onApprove={onApprove}
              onEdit={m.role === "user" ? onEdit : undefined}
              onBranch={onBranch}
              onReply={onReply}
              onOpenArtifact={onOpenArtifact}
              showActionTrace={showActionTrace}
            />
          ))}
        </ol>
      </div>

      {!pinned ? (
        <button
          type="button"
          onClick={jumpToBottom}
          className={cn(
            "absolute bottom-4 left-1/2 inline-flex h-9 w-9 -translate-x-1/2 items-center justify-center rounded-full",
            "border border-sg-accent/30 bg-sg-card text-sg-accent shadow-sg-glow",
            "transition hover:bg-sg-accent-soft hover:text-sg-ink",
          )}
          aria-label={t("chat.jumpToLatestAriaLabel")}
          data-testid="jump-to-bottom"
        >
          <ArrowDown className="h-4 w-4" aria-hidden="true" />
        </button>
      ) : null}
    </div>
  );
}
