"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { motion, useReducedMotion } from "framer-motion";
import {
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  CornerUpLeft,
  GitFork,
  Menu,
  Pencil,
  RefreshCcw,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { springs, useMotionVariants } from "@/lib/motion";
import type {
  ApprovalDecision,
  ApprovalScope,
  ChatMessage,
} from "@/lib/chat/types";
import { MarkdownMessage } from "@/components/chat/markdown-message";
import { AttachmentGallery } from "@/components/chat/attachment-gallery";
import { ToolCallCard } from "@/components/chat/tool-call-card";
import { ReasoningBlock } from "@/components/chat/reasoning-block";
import { SubagentCard } from "@/components/chat/subagent-card";
import { ApprovalPrompt } from "@/components/chat/approval-prompt";

interface MessageBubbleProps {
  message: ChatMessage;
  onCopy?: (text: string) => void;
  onRegenerate?: () => void;
  onApprove?: (
    turnId: string,
    callId: string,
    decision: ApprovalDecision,
    scope: ApprovalScope,
  ) => void;
  onEdit?: (messageId: string, newContent: string) => void | Promise<void>;
  onBranch?: (messageId: string) => void;
  onReply?: (messageId: string) => void;
  onOpenArtifact?: (language: string, source: string) => void;
  versionIndex?: number;
  versionCount?: number;
  onPrevVersion?: () => void;
  onNextVersion?: () => void;
  showActionTrace?: boolean;
  /** Latest message in the thread — gets the entrance animation. */
  isLatest?: boolean;
}

function formatTime(ms: number): string {
  const d = new Date(ms);
  return Number.isNaN(d.getTime())
    ? ""
    : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

/**
 * Detects a leading reply-quote that the composer prepends as
 * `> <author>: <preview>\n\n<body>` and splits it out so the bubble can
 * render the quote as a styled reference bar instead of raw markdown.
 */
function splitReplyQuote(
  content: string,
): { quote: string; body: string } | null {
  if (!content.startsWith("> ")) return null;
  const sep = content.indexOf("\n\n");
  if (sep === -1) return null;
  // Only treat as a reply quote when the quote is a single quoted line.
  const quoteBlock = content.slice(0, sep);
  if (quoteBlock.includes("\n")) return null;
  const body = content.slice(sep + 2);
  if (!body.trim()) return null;
  return { quote: quoteBlock.slice(2).trim(), body };
}

// Memoised so a streaming `pendingMessage` delta (which re-renders the whole
// `MessageList`) does not re-render — and re-parse the markdown of — every
// settled historical bubble. Settled messages keep a stable object identity
// and all callback props are `useCallback`-stable at the consumer, so the
// default shallow prop comparison is sufficient (see R4-D5).
export const MessageBubble = React.memo(function MessageBubble({
  message,
  onCopy,
  onRegenerate,
  onApprove,
  onEdit,
  onBranch,
  onReply,
  onOpenArtifact,
  versionIndex,
  versionCount,
  onPrevVersion,
  onNextVersion,
  showActionTrace = true,
  isLatest = false,
}: MessageBubbleProps) {
  const { t } = useTranslation();
  const { liquidRise } = useMotionVariants();
  const reducedMotion = useReducedMotion();
  const isUser = message.role === "user";
  const isAssistant = message.role === "assistant";
  const isSystem = message.role === "system";
  const [copied, setCopied] = React.useState(false);
  const [editing, setEditing] = React.useState(false);
  const [savingEdit, setSavingEdit] = React.useState(false);
  const [draft, setDraft] = React.useState(message.content);
  const [toolsCollapsed, setToolsCollapsed] = React.useState(false);

  // Bulk collapse switches on automatically when the assistant fires
  // many tool calls — keeps the bubble compact during long agent loops.
  // The user can re-expand any time via the hamburger.
  const toolCount = message.toolCalls?.length ?? 0;
  const subagentCount = message.subagents?.length ?? 0;
  const shouldShowActionTrace = showActionTrace !== false;
  React.useEffect(() => {
    if (toolCount + subagentCount >= 8 && !message.pending) {
      setToolsCollapsed(true);
    }
  }, [toolCount, subagentCount, message.pending]);

  React.useEffect(() => {
    if (!editing) setDraft(message.content);
  }, [editing, message.content]);

  const handleCopy = React.useCallback(() => {
    if (!message.content) return;
    void navigator.clipboard?.writeText(message.content).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
      onCopy?.(message.content);
    });
  }, [message.content, onCopy]);

  const handleStartEdit = React.useCallback(() => {
    setDraft(message.content);
    setEditing(true);
  }, [message.content]);

  const handleSaveEdit = React.useCallback(() => {
    const next = draft.trim();
    if (!next || next === message.content) {
      setEditing(false);
      return;
    }
    if (!onEdit) {
      setEditing(false);
      return;
    }
    // `onEdit` may be sync (void) or async (Promise). Only leave edit mode
    // once it has actually succeeded — if the edit/re-run rejects we keep the
    // bubble in edit mode with the draft intact so the user doesn't lose work.
    let result: void | Promise<void>;
    try {
      result = onEdit(message.id, next);
    } catch {
      // Synchronous throw — stay in edit mode, preserve the draft.
      return;
    }
    if (result && typeof (result as Promise<void>).then === "function") {
      setSavingEdit(true);
      (result as Promise<void>).then(
        () => {
          setSavingEdit(false);
          setEditing(false);
        },
        () => {
          // Rejected: keep editing so the draft survives for another attempt.
          setSavingEdit(false);
        },
      );
      return;
    }
    setEditing(false);
  }, [draft, message.id, message.content, onEdit]);

  const roleLabel = isUser
    ? t("chat.roleYou")
    : isAssistant
      ? t("chat.roleAssistant")
      : t("chat.roleSystem");

  // User bubbles may carry a quoted reply the composer prepended. Split
  // it out so the quote renders as a reference bar above the body.
  const reply = isUser && !editing ? splitReplyQuote(message.content) : null;
  const displayContent = reply ? reply.body : message.content;

  const roleRow = (
    <div
      className={cn(
        "flex items-center gap-1.5 text-[11px] text-sg-ink-5",
        isUser ? "flex-row-reverse" : "flex-row",
      )}
    >
      <span
        className={cn(
          "h-3.5 w-3.5 shrink-0 rounded-full bg-gradient-to-br",
          isUser
            ? "from-sg-accent-3 to-sg-accent"
            : isAssistant
              ? "from-sg-accent to-sg-accent-2"
              : "from-sg-ink-5 to-sg-ink-4",
        )}
        aria-hidden="true"
      />
      <span className="font-medium text-sg-ink-4">{roleLabel}</span>
      <span aria-hidden="true">·</span>
      <time
        dateTime={new Date(message.createdAt).toISOString()}
        className="font-mono text-[10px]"
      >
        {formatTime(message.createdAt)}
      </time>
      {message.usage?.estimatedCostUsd ? (
        <>
          <span aria-hidden="true">·</span>
          <span className="font-mono text-[10px]">
            ${message.usage.estimatedCostUsd.toFixed(4)}
          </span>
        </>
      ) : null}
    </div>
  );

  // Shared action-button chrome: ≥24px touch target (min-h-6/min-w-6 +
  // centred icon), Tab-focusable (native <button>), and a focus-visible ring
  // so keyboard users can see where they are. Padding stays modest so the bar
  // keeps its compact look — the min-size guarantees the hit area, not padding.
  const actionBtn = cn(
    "inline-flex min-h-6 min-w-6 items-center justify-center gap-1 rounded-full px-1.5 py-0.5",
    "hover:bg-sg-inset-hover hover:text-sg-ink",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/50",
  );

  const actionBar =
    (isAssistant || isUser) && message.content && !editing ? (
      <motion.div
        whileHover={reducedMotion ? undefined : { y: -2 }}
        transition={springs.bouncy}
        className={cn(
          "inline-flex items-center gap-0.5 rounded-full sg-inset px-1 py-0.5",
          "text-[11px] text-sg-ink-4",
          // Reveal on pointer hover OR keyboard focus landing anywhere in the
          // bubble (group-focus-within) OR focus inside the bar itself, so the
          // controls are reachable for keyboard / screen-reader users — not
          // hover-only. `focus-within` keeps it visible while Tabbing through.
          "opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100 focus-within:opacity-100",
          isUser ? "flex-row-reverse" : "flex-row",
        )}
      >
        <button
          type="button"
          onClick={handleCopy}
          className={actionBtn}
          aria-label={copied ? t("chat.copied") : t("chat.copy")}
        >
          {copied ? (
            <Check className="h-3 w-3 text-sg-ok" aria-hidden="true" />
          ) : (
            <Copy className="h-3 w-3" aria-hidden="true" />
          )}
        </button>
        {isUser && onEdit ? (
          <button
            type="button"
            onClick={handleStartEdit}
            className={actionBtn}
            aria-label={t("chat.editAriaLabel")}
            data-testid="bubble-edit-trigger"
          >
            <Pencil className="h-3 w-3" aria-hidden="true" />
          </button>
        ) : null}
        {isAssistant && onRegenerate ? (
          <button
            type="button"
            onClick={onRegenerate}
            className={actionBtn}
            aria-label={t("chat.regenerateAriaLabel")}
          >
            <RefreshCcw className="h-3 w-3" aria-hidden="true" />
          </button>
        ) : null}
        {onBranch ? (
          <button
            type="button"
            onClick={() => onBranch(message.id)}
            className={actionBtn}
            aria-label={t("chat.branchAriaLabel")}
            data-testid="bubble-branch"
          >
            <GitFork className="h-3 w-3" aria-hidden="true" />
          </button>
        ) : null}
        {onReply ? (
          <button
            type="button"
            onClick={() => onReply(message.id)}
            className={actionBtn}
            aria-label={t("chat.replyAriaLabel")}
            data-testid="bubble-reply"
          >
            <CornerUpLeft className="h-3 w-3" aria-hidden="true" />
          </button>
        ) : null}
        {versionCount && versionCount > 1 ? (
          <span
            className="ml-0.5 inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 font-mono"
            data-testid="bubble-version-switcher"
          >
            <button
              type="button"
              onClick={onPrevVersion}
              className="text-sg-ink-4 hover:text-sg-ink disabled:opacity-30"
              disabled={versionIndex === 0}
              aria-label={t("chat.versionPrev")}
            >
              ‹
            </button>
            <span>
              {(versionIndex ?? 0) + 1}/{versionCount}
            </span>
            <button
              type="button"
              onClick={onNextVersion}
              className="text-sg-ink-4 hover:text-sg-ink disabled:opacity-30"
              disabled={(versionIndex ?? 0) === versionCount - 1}
              aria-label={t("chat.versionNext")}
            >
              ›
            </button>
          </span>
        ) : null}
      </motion.div>
    ) : null;

  const trace = (
    <>
      {shouldShowActionTrace && (toolCount > 0 || subagentCount > 0) && (
        <div className="mt-2 flex items-center gap-1 text-[11px] text-sg-ink-4">
          <button
            type="button"
            onClick={() => setToolsCollapsed((v) => !v)}
            className="inline-flex items-center gap-1 rounded-sg-sm border border-sg-border px-1.5 py-0.5 hover:bg-sg-inset hover:text-sg-ink"
            aria-label={
              toolsCollapsed
                ? t("chat.bubbleToggleToolsExpand")
                : t("chat.bubbleToggleToolsCollapse")
            }
            aria-expanded={!toolsCollapsed}
            data-testid="bubble-tools-toggle"
          >
            <Menu className="h-3 w-3" aria-hidden="true" />
            {toolsCollapsed ? (
              <ChevronRight className="h-3 w-3" aria-hidden="true" />
            ) : (
              <ChevronDown className="h-3 w-3" aria-hidden="true" />
            )}
            {toolsCollapsed
              ? toolCount > 0
                ? t("chat.bubbleToolsCollapsedSummary", {
                    count: toolCount,
                    n: toolCount,
                  })
                : t("chat.bubbleSubagentsCollapsedSummary", {
                    count: subagentCount,
                    n: subagentCount,
                  })
              : null}
          </button>
          {toolsCollapsed && toolCount > 0 && subagentCount > 0 ? (
            <span className="font-mono text-sg-ink-4">
              ·{" "}
              {t("chat.bubbleSubagentsCollapsedSummary", {
                count: subagentCount,
                n: subagentCount,
              })}
            </span>
          ) : null}
        </div>
      )}
      {shouldShowActionTrace && !toolsCollapsed && message.toolCalls?.map((tc) => (
        <ToolCallCard key={tc.callId} tool={tc} />
      ))}
      {shouldShowActionTrace && !toolsCollapsed && message.subagents?.map((sa) => (
        <SubagentCard key={sa.childSessionKey} subagent={sa} />
      ))}
      {message.approvals?.map((ap) => (
        <ApprovalPrompt
          key={ap.callId}
          prompt={ap}
          onDecide={(decision, scope) => {
            if (message.turnId && onApprove) {
              onApprove(message.turnId, ap.callId, decision, scope);
            }
          }}
        />
      ))}

      {message.cancelling && !message.error ? (
        /* Stop clicked, backend unwinding — visible confirmation that the
         * click took (previously: no feedback until TurnErrored landed). */
        <div
          className="mt-2 inline-flex items-center gap-1.5 rounded-sg-sm border border-sg-border bg-sg-inset px-2 py-1 text-[11px] text-sg-ink-3"
          role="status"
        >
          <span
            className="h-2.5 w-2.5 animate-spin rounded-full border border-sg-ink-4 border-t-transparent"
            aria-hidden="true"
          />
          {t("chat.stopping")}
        </div>
      ) : null}
      {message.error === "cancelled" ? (
        /* User-initiated stop is not an error — neutral chip, no red. */
        <div
          className="mt-2 inline-flex items-center rounded-sg-sm border border-sg-border bg-sg-inset px-2 py-1 text-[11px] text-sg-ink-3"
          role="status"
        >
          {t("chat.stoppedByUser")}
        </div>
      ) : message.error === "session_expired" ? (
        <div
          className="mt-2 flex flex-wrap items-center gap-2 rounded-sg-sm border border-sg-warn/40 bg-sg-warn-soft px-2 py-1 text-[11px] text-sg-ink-2"
          role="alert"
        >
          <span>{t("chat.sessionExpired")}</span>
          <a
            href={`/login?redirect=${encodeURIComponent("/chat")}`}
            className="font-medium text-sg-accent underline underline-offset-2"
          >
            {t("chat.reLogin")}
          </a>
        </div>
      ) : message.error ? (
        <div
          className="mt-2 flex flex-wrap items-center gap-2 rounded-sg-sm border border-sg-err/40 bg-sg-err-soft px-2 py-1 text-[11px] text-sg-err"
          role="alert"
        >
          <span className="min-w-0 break-words">{message.error}</span>
          {isAssistant && onRegenerate ? (
            <button
              type="button"
              onClick={onRegenerate}
              className="inline-flex shrink-0 items-center gap-1 rounded-sg-sm border border-sg-err/40 px-1.5 py-0.5 font-medium text-sg-err hover:bg-sg-err/10"
              data-testid="bubble-retry"
            >
              <RefreshCcw className="h-3 w-3" aria-hidden="true" />
              {t("chat.retryTurn")}
            </button>
          ) : null}
        </div>
      ) : null}
    </>
  );

  return (
    <motion.li
      // Only the newest bubble plays the liquid-rise spring entrance — the
      // settled history renders statically so the memoised list stays cheap
      // and the streaming render-perf contract (R4-D5) is preserved.
      initial={isLatest ? "hidden" : false}
      animate={isLatest ? "visible" : undefined}
      variants={isLatest ? liquidRise : undefined}
      className={cn(
        "group flex w-full",
        isUser ? "justify-end" : "justify-start",
      )}
      data-testid="chat-bubble"
      data-role={message.role}
      data-pending={message.pending ? "true" : undefined}
      data-message-id={message.id}
      id={`chat-msg-${message.id}`}
    >
      <div
        className={cn(
          "flex min-w-0 flex-col gap-1.5",
          isUser ? "max-w-[78%] items-end" : "w-full items-start",
        )}
      >
        {roleRow}

        {isAssistant ? (
          /* Assistant: clean transparent prose block, no card chrome. */
          <div className="w-full text-[13px] leading-relaxed text-sg-ink">
            {shouldShowActionTrace && message.reasoning ? (
              <ReasoningBlock
                text={message.reasoning}
                streaming={Boolean(message.pending)}
              />
            ) : null}
            <MarkdownMessage
              content={message.content || (message.pending ? "" : "")}
              streaming={Boolean(message.pending && !message.toolCalls?.length)}
              onOpenArtifact={onOpenArtifact}
            />
            {/* W4 — assistant-produced media (generated images etc.),
              * journaled with the turn and rehydrated on replay. */}
            {message.attachments && message.attachments.length > 0 ? (
              <AttachmentGallery attachments={message.attachments} />
            ) : null}
            {trace}
          </div>
        ) : isSystem ? (
          /* System/tool: muted dashed inset treatment. */
          <div className="rounded-sg-md border border-dashed border-sg-border bg-sg-inset px-3 py-2 text-[13px] leading-relaxed text-sg-ink-3 italic">
            <div className="whitespace-pre-wrap break-words">
              {message.content}
            </div>
            {trace}
          </div>
        ) : editing && isUser ? (
          /* User edit-in-place. */
          <div
            className="flex w-full flex-col gap-1.5 rounded-sg-lg rounded-br-sg-sm border border-sg-accent/30 bg-sg-accent-soft px-4 py-2.5"
            data-testid="bubble-edit"
          >
            <textarea
              autoFocus
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  handleSaveEdit();
                }
                if (e.key === "Escape") {
                  e.preventDefault();
                  setEditing(false);
                }
              }}
              rows={Math.min(8, Math.max(2, draft.split("\n").length))}
              className="w-full resize-none rounded-sg-sm border border-sg-accent/40 bg-sg-inset px-2 py-1 text-[13px] text-sg-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40"
              data-testid="bubble-edit-input"
            />
            <div className="flex items-center justify-end gap-1.5 text-[11px]">
              <button
                type="button"
                onClick={() => setEditing(false)}
                disabled={savingEdit}
                className="rounded-sg-sm px-2 py-0.5 text-sg-ink-4 hover:bg-sg-inset hover:text-sg-ink disabled:opacity-40"
                data-testid="bubble-edit-cancel"
              >
                {t("chat.editCancel")}
              </button>
              <button
                type="button"
                onClick={handleSaveEdit}
                disabled={savingEdit}
                className="rounded-sg-sm border border-sg-accent/40 bg-sg-accent px-2 py-0.5 text-white hover:bg-sg-accent/90 disabled:opacity-60"
                data-testid="bubble-edit-save"
              >
                {savingEdit ? t("chat.editSaving") : t("chat.editSaveRerun")}
              </button>
            </div>
          </div>
        ) : (
          /* User: right-aligned compact bubble. */
          <div
            className={cn(
              "flex max-w-full flex-col gap-1.5 rounded-sg-lg rounded-br-sg-sm border border-sg-accent/20 bg-sg-accent-soft px-4 py-2.5 text-[13px] leading-relaxed text-sg-ink",
              message.error && "border-sg-err/50",
            )}
          >
            {message.attachments && message.attachments.length > 0 ? (
              <AttachmentGallery attachments={message.attachments} />
            ) : null}
            {reply ? (
              <motion.div
                initial={reducedMotion ? false : { opacity: 0, x: -8 }}
                animate={{ opacity: 1, x: 0 }}
                transition={reducedMotion ? { duration: 0 } : springs.soft}
                className="border-l-2 border-sg-accent pl-2 text-[12px] text-sg-ink-4"
              >
                <span className="line-clamp-2 break-words">{reply.quote}</span>
              </motion.div>
            ) : null}
            <div className="whitespace-pre-wrap break-words">{displayContent}</div>
            {trace}
          </div>
        )}

        {actionBar}
      </div>
    </motion.li>
  );
});
