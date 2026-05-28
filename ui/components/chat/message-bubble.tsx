"use client";

/**
 * One message bubble. Hosts:
 *
 *   - role-styled container (user right, assistant/system left)
 *   - reasoning block (top)
 *   - markdown content
 *   - tool-call cards
 *   - sub-agent cards
 *   - approval prompts
 *   - hover toolbar (copy / regenerate / retry)
 *
 * Attachments are rendered inline at the top of the bubble.
 */

import * as React from "react";
import {
  Bot,
  Copy,
  Image as ImageIcon,
  Paperclip,
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
}: MessageBubbleProps) {
  const isUser = message.role === "user";
  const isAssistant = message.role === "assistant";
  const isSystem = message.role === "system";
  const [copied, setCopied] = React.useState(false);

  const handleCopy = React.useCallback(() => {
    if (!message.content) return;
    void navigator.clipboard?.writeText(message.content).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
      onCopy?.(message.content);
    });
  }, [message.content, onCopy]);

  return (
    <li
      className={cn(
        "group flex w-full",
        isUser ? "justify-end" : "justify-start",
      )}
      data-testid="chat-bubble"
      data-role={message.role}
      data-pending={message.pending ? "true" : undefined}
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
          <span className="font-medium">
            {isUser ? "You" : isAssistant ? "Assistant" : "System"}
          </span>
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
            isUser &&
              "border-tp-amber/40 bg-tp-amber/10 text-tp-ink",
            isAssistant &&
              "border-tp-glass-edge bg-tp-glass-inner text-tp-ink",
            isSystem &&
              "border-dashed border-tp-glass-edge bg-tp-glass-inner/40 text-tp-ink-2 italic",
            message.error && "border-tp-err/50",
          )}
        >
          {/* attachments */}
          {message.attachments && message.attachments.length > 0 ? (
            <ul className="mb-2 flex flex-wrap gap-1.5" aria-label="attachments">
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

          {/* reasoning (above main content) */}
          {message.reasoning ? (
            <ReasoningBlock
              text={message.reasoning}
              streaming={Boolean(message.pending)}
            />
          ) : null}

          {/* main content */}
          {isAssistant ? (
            <MarkdownMessage
              content={message.content || (message.pending ? "" : "")}
              streaming={Boolean(message.pending && !message.toolCalls?.length)}
            />
          ) : (
            <div className="whitespace-pre-wrap break-words">
              {message.content}
            </div>
          )}

          {/* tool calls */}
          {message.toolCalls?.map((tc) => (
            <ToolCallCard key={tc.callId} tool={tc} />
          ))}

          {/* sub-agents */}
          {message.subagents?.map((sa) => (
            <SubagentCard key={sa.childSessionKey} subagent={sa} />
          ))}

          {/* approvals */}
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

          {/* error */}
          {message.error ? (
            <div
              className="mt-2 rounded border border-tp-err/40 bg-tp-err/10 px-2 py-1 text-[11px] text-tp-err"
              role="alert"
            >
              {message.error}
            </div>
          ) : null}
        </div>

        {/* hover toolbar (visible on hover, opacity transition) */}
        {(isAssistant || isUser) && message.content ? (
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
              aria-label={copied ? "Copied" : "Copy message"}
            >
              <Copy className="h-3 w-3" aria-hidden="true" />
              {copied ? "Copied" : "Copy"}
            </button>
            {isAssistant && onRegenerate ? (
              <button
                type="button"
                onClick={onRegenerate}
                className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 hover:bg-tp-glass-inner"
                aria-label="Regenerate response"
              >
                <RefreshCcw className="h-3 w-3" aria-hidden="true" />
                Regenerate
              </button>
            ) : null}
          </div>
        ) : null}
      </div>
    </li>
  );
}
