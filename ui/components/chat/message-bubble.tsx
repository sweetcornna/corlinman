"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import {
  Bot,
  ChevronDown,
  ChevronRight,
  Copy,
  CornerUpLeft,
  GitFork,
  Image as ImageIcon,
  Menu,
  Paperclip,
  Pencil,
  RefreshCcw,
  User,
} from "lucide-react";

import { cn } from "@/lib/utils";
import type {
  ApprovalDecision,
  ApprovalScope,
  ChatMessage,
} from "@/lib/chat/types";
import { MarkdownMessage } from "@/components/chat/markdown-message";
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
  onEdit?: (messageId: string, newContent: string) => void;
  onBranch?: (messageId: string) => void;
  onReply?: (messageId: string) => void;
  onOpenArtifact?: (language: string, source: string) => void;
  versionIndex?: number;
  versionCount?: number;
  onPrevVersion?: () => void;
  onNextVersion?: () => void;
}

function formatTime(ms: number): string {
  const d = new Date(ms);
  return Number.isNaN(d.getTime())
    ? ""
    : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

export function MessageBubble({
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
}: MessageBubbleProps) {
  const { t } = useTranslation();
  const isUser = message.role === "user";
  const isAssistant = message.role === "assistant";
  const isSystem = message.role === "system";
  const [copied, setCopied] = React.useState(false);
  const [editing, setEditing] = React.useState(false);
  const [draft, setDraft] = React.useState(message.content);
  const [toolsCollapsed, setToolsCollapsed] = React.useState(false);

  // Bulk collapse switches on automatically when the assistant fires
  // many tool calls — keeps the bubble compact during long agent loops.
  // The user can re-expand any time via the hamburger.
  const toolCount = message.toolCalls?.length ?? 0;
  const subagentCount = message.subagents?.length ?? 0;
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
    onEdit?.(message.id, next);
    setEditing(false);
  }, [draft, message.id, message.content, onEdit]);

  const roleLabel = isUser
    ? t("chat.roleYou")
    : isAssistant
      ? t("chat.roleAssistant")
      : t("chat.roleSystem");

  return (
    <li
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
          "flex max-w-[88%] flex-col gap-1.5",
          isUser ? "items-end" : "items-start",
        )}
      >
        <div
          className={cn(
            "flex items-center gap-1.5 text-[11px] text-tp-ink-3",
            isUser ? "flex-row-reverse" : "flex-row",
          )}
        >
          {isUser ? (
            <User className="h-3 w-3" aria-hidden="true" />
          ) : (
            <Bot className="h-3 w-3" aria-hidden="true" />
          )}
          <span className="font-medium">{roleLabel}</span>
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

        <div
          className={cn(
            "relative rounded-lg border px-3 py-2 text-[13px] leading-relaxed",
            isUser && "border-tp-amber/40 bg-tp-amber/10 text-tp-ink",
            isAssistant && "border-tp-glass-edge bg-tp-glass-inner text-tp-ink",
            isSystem && "border-dashed border-tp-glass-edge bg-tp-glass-inner/40 text-tp-ink-2 italic",
            message.error && "border-tp-err/50",
          )}
        >
          {message.attachments && message.attachments.length > 0 ? (
            <ul className="mb-2 flex flex-wrap gap-1.5" aria-label={t("chat.attachmentsAriaLabel")}>
              {message.attachments.map((att) => (
                <li
                  key={att.id}
                  className="flex items-center gap-1 rounded border border-tp-glass-edge bg-tp-glass-inner/60 px-1.5 py-0.5 text-[11px] text-tp-ink-2"
                >
                  {att.kind === "image" ? (
                    <ImageIcon className="h-3 w-3" aria-hidden="true" />
                  ) : (
                    <Paperclip className="h-3 w-3" aria-hidden="true" />
                  )}
                  <span className="font-mono">{att.name}</span>
                </li>
              ))}
            </ul>
          ) : null}

          {message.reasoning ? (
            <ReasoningBlock text={message.reasoning} streaming={Boolean(message.pending)} />
          ) : null}

          {isAssistant ? (
            <MarkdownMessage
              content={message.content || (message.pending ? "" : "")}
              streaming={Boolean(message.pending && !message.toolCalls?.length)}
              onOpenArtifact={onOpenArtifact}
            />
          ) : editing && isUser ? (
            <div className="flex flex-col gap-1.5" data-testid="bubble-edit">
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
                className="w-full resize-none rounded border border-tp-amber bg-tp-glass-inner px-2 py-1 text-[13px] text-tp-ink focus:outline-none"
                data-testid="bubble-edit-input"
              />
              <div className="flex items-center justify-end gap-1.5 text-[11px]">
                <button
                  type="button"
                  onClick={() => setEditing(false)}
                  className="rounded px-2 py-0.5 text-tp-ink-3 hover:bg-tp-glass-inner hover:text-tp-ink"
                  data-testid="bubble-edit-cancel"
                >
                  {t("chat.editCancel")}
                </button>
                <button
                  type="button"
                  onClick={handleSaveEdit}
                  className="rounded border border-tp-amber/60 bg-tp-amber/20 px-2 py-0.5 text-tp-ink hover:bg-tp-amber/30"
                  data-testid="bubble-edit-save"
                >
                  {t("chat.editSaveRerun")}
                </button>
              </div>
            </div>
          ) : (
            <div className="whitespace-pre-wrap break-words">
              {message.content}
            </div>
          )}

          {(toolCount > 0 || subagentCount > 0) && (
            <div className="mt-2 flex items-center gap-1 text-[11px] text-tp-ink-3">
              <button
                type="button"
                onClick={() => setToolsCollapsed((v) => !v)}
                className="inline-flex items-center gap-1 rounded border border-tp-glass-edge px-1.5 py-0.5 hover:bg-tp-glass-inner hover:text-tp-ink"
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
                <span className="font-mono text-tp-ink-3">
                  ·{" "}
                  {t("chat.bubbleSubagentsCollapsedSummary", {
                    count: subagentCount,
                    n: subagentCount,
                  })}
                </span>
              ) : null}
            </div>
          )}
          {!toolsCollapsed && message.toolCalls?.map((tc) => (
            <ToolCallCard key={tc.callId} tool={tc} />
          ))}
          {!toolsCollapsed && message.subagents?.map((sa) => (
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

          {message.error ? (
            <div
              className="mt-2 rounded border border-tp-err/40 bg-tp-err/10 px-2 py-1 text-[11px] text-tp-err"
              role="alert"
            >
              {message.error}
            </div>
          ) : null}
        </div>

        {(isAssistant || isUser) && message.content && !editing ? (
          <div
            className={cn(
              "flex items-center gap-1 text-[11px] text-tp-ink-3",
              "opacity-0 transition-opacity group-hover:opacity-100",
              isUser ? "flex-row-reverse" : "flex-row",
            )}
          >
            <button
              type="button"
              onClick={handleCopy}
              className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 hover:bg-tp-glass-inner"
              aria-label={copied ? t("chat.copied") : t("chat.copy")}
            >
              <Copy className="h-3 w-3" aria-hidden="true" />
              {copied ? t("chat.copied") : t("chat.copy")}
            </button>
            {isUser && onEdit ? (
              <button
                type="button"
                onClick={handleStartEdit}
                className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 hover:bg-tp-glass-inner"
                aria-label={t("chat.editAriaLabel")}
                data-testid="bubble-edit-trigger"
              >
                <Pencil className="h-3 w-3" aria-hidden="true" />
                {t("chat.edit")}
              </button>
            ) : null}
            {isAssistant && onRegenerate ? (
              <button
                type="button"
                onClick={onRegenerate}
                className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 hover:bg-tp-glass-inner"
                aria-label={t("chat.regenerateAriaLabel")}
              >
                <RefreshCcw className="h-3 w-3" aria-hidden="true" />
                {t("chat.regenerate")}
              </button>
            ) : null}
            {onBranch ? (
              <button
                type="button"
                onClick={() => onBranch(message.id)}
                className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 hover:bg-tp-glass-inner"
                aria-label={t("chat.branchAriaLabel")}
                data-testid="bubble-branch"
              >
                <GitFork className="h-3 w-3" aria-hidden="true" />
                {t("chat.branch")}
              </button>
            ) : null}
            {onReply ? (
              <button
                type="button"
                onClick={() => onReply(message.id)}
                className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 hover:bg-tp-glass-inner"
                aria-label={t("chat.replyAriaLabel")}
                data-testid="bubble-reply"
              >
                <CornerUpLeft className="h-3 w-3" aria-hidden="true" />
                {t("chat.reply")}
              </button>
            ) : null}
            {versionCount && versionCount > 1 ? (
              <span
                className="ml-1 inline-flex items-center gap-1 rounded border border-tp-glass-edge px-1.5 py-0.5 font-mono"
                data-testid="bubble-version-switcher"
              >
                <button
                  type="button"
                  onClick={onPrevVersion}
                  className="text-tp-ink-3 hover:text-tp-ink disabled:opacity-30"
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
                  className="text-tp-ink-3 hover:text-tp-ink disabled:opacity-30"
                  disabled={(versionIndex ?? 0) === versionCount - 1}
                  aria-label={t("chat.versionNext")}
                >
                  ›
                </button>
              </span>
            ) : null}
          </div>
        ) : null}
      </div>
    </li>
  );
}
