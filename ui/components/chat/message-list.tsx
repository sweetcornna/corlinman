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

// CSS-containment threshold. Below this we render every bubble eagerly —
// containment buys nothing on short threads and the `contain-intrinsic-size`
// estimate only adds risk to the prepend scroll-anchoring math. Above it the
// off-screen render/paint savings are worth the estimate.
const CONTAIN_THRESHOLD = 50;
// Per-bubble height estimate used for `contain-intrinsic-size` on off-screen
// (skipped) settled bubbles. It only needs to be in the right ballpark — the
// browser swaps in the real measured height once a bubble scrolls near the
// viewport. A typical short reply renders ~80–160px tall.
const CONTAIN_INTRINSIC_PX = 120;

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

  // Long-thread perf: settled (off-screen) bubbles get CSS containment via a
  // scoped `<style>` (see below) so the browser can skip their layout/paint.
  // We do NOT use a virtualization library: it would fight the prepend
  // scroll-anchoring below (which depends on real DOM scrollHeight) and the
  // streaming re-render isolation contract (render-perf test). Containment is
  // purely visual/paint-level — every bubble stays in the DOM, so search jump,
  // anchoring, and `aria-live` announcements keep working.
  const contain = all.length > CONTAIN_THRESHOLD;

  // W5 scroll anchoring: when an older page is PREPENDED (first id
  // changed but the old first message still exists further down), keep
  // the viewport visually anchored by compensating for the height the
  // new content added above it.
  //
  // Containment trade-off: with `content-visibility: auto`, off-screen bubbles
  // report their `contain-intrinsic-size` ESTIMATE rather than their real
  // height in `scrollHeight`. A prepend inserts a batch of bubbles ABOVE the
  // viewport — if those were contained on the very render we measure, the
  // `scrollHeight` delta would reflect estimates, not real heights, and the
  // anchor could drift. We avoid that by suppressing containment for exactly
  // the render that applies the anchor compensation (`suppressContain`); the
  // prepended bubbles are then measured at their real height, the delta is
  // exact, and containment re-enables on the next commit once the viewport is
  // settled.
  const prevFirstIdRef = React.useRef<string | null>(null);
  const prevScrollHeightRef = React.useRef(0);
  const [suppressContain, setSuppressContain] = React.useState(false);
  const suppressTimerRef = React.useRef<number | null>(null);
  React.useLayoutEffect(() => {
    const el = scrollRef.current;
    const firstId = messages[0]?.id ?? null;
    const prevFirst = prevFirstIdRef.current;
    const prepended =
      Boolean(el) &&
      Boolean(prevFirst) &&
      firstId !== prevFirst &&
      messages.some((m) => m.id === prevFirst);
    if (el && prepended) {
      el.scrollTop += el.scrollHeight - prevScrollHeightRef.current;
      // The prepend landed: anchor is set against REAL heights — drop the
      // suppression so containment re-engages on the next commit.
      if (suppressContain) setSuppressContain(false);
      if (suppressTimerRef.current !== null) {
        window.clearTimeout(suppressTimerRef.current);
        suppressTimerRef.current = null;
      }
    }
    prevFirstIdRef.current = firstId;
    if (el) prevScrollHeightRef.current = el.scrollHeight;
  }, [messages, suppressContain]);

  // When the caller is about to prepend an older page, drop containment until
  // the prepend lands so the anchoring math measures real heights (above).
  // A safety timer re-enables it if the load yields nothing (error / no-op),
  // so we never leave a long thread permanently uncontained.
  const handleLoadEarlier = React.useCallback(() => {
    if (contain) {
      setSuppressContain(true);
      if (suppressTimerRef.current !== null) {
        window.clearTimeout(suppressTimerRef.current);
      }
      suppressTimerRef.current = window.setTimeout(() => {
        setSuppressContain(false);
        suppressTimerRef.current = null;
      }, 4000);
    }
    onLoadEarlier?.();
  }, [contain, onLoadEarlier]);

  React.useEffect(
    () => () => {
      if (suppressTimerRef.current !== null) {
        window.clearTimeout(suppressTimerRef.current);
      }
    },
    [],
  );

  const containActive = contain && !suppressContain;

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
      {/* Scoped CSS containment for settled (non-last) bubbles. Applied only
        * past CONTAIN_THRESHOLD and only while not suppressed for a prepend.
        * Targets the bubble `<li>`s by their `data-message-id` (the
        * load-earlier pill `<li>` has none) and excludes `:last-child`, which
        * is always the latest / streaming bubble — so the newest message and
        * the pending stream are never skipped. Scoped via the
        * `data-contain="on"` attribute on the `<ol>` so the rule cannot leak
        * to other lists. */}
      {containActive ? (
        <style
          // eslint-disable-next-line react/no-danger
          dangerouslySetInnerHTML={{
            __html: `[data-contain="on"] > li[data-message-id]:not(:last-child){content-visibility:auto;contain-intrinsic-size:auto ${CONTAIN_INTRINSIC_PX}px;}`,
          }}
        />
      ) : null}
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="h-full overflow-y-auto py-5"
        data-testid="message-list"
        role="log"
        aria-label={t("chat.messageLogAriaLabel")}
        aria-live="polite"
        aria-relevant="additions text"
        aria-atomic="false"
      >
        <ol
          className="mx-auto flex w-full max-w-3xl flex-col gap-5 px-4"
          data-contain={containActive ? "on" : undefined}
        >
          {hasEarlier ? (
            <li className="flex justify-center">
              <button
                type="button"
                onClick={handleLoadEarlier}
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
