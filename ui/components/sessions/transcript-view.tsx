"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { Bot, MessageSquareDashed, User } from "@/components/icons";

import { cn } from "@/lib/utils";
import { formatDateTime } from "@/lib/format";
import type { TranscriptMessage } from "@/lib/api/sessions";

/**
 * Chat-style renderer for a replay transcript.
 *
 * Style choices, per the brief:
 *   - Alternating left/right alignment by role: `user` right, `assistant`
 *     and `system` left.
 *   - Distinct chip color per role so the eye can follow the dialogue
 *     without reading the role label.
 *   - Per-message timestamp rendered with `new Date(ts).toLocaleString()`
 *     so it picks up the operator's locale.
 *
 * The component itself is presentational — fetching and the dialog frame
 * live in `replay-dialog.tsx`. Empty + system messages render explicitly
 * so a session with no replayable content shows an honest empty state.
 */

interface TranscriptViewProps {
  transcript: TranscriptMessage[];
}

function formatTs(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return formatDateTime(d);
}

export function TranscriptView({ transcript }: TranscriptViewProps) {
  const { t } = useTranslation();

  if (transcript.length === 0) {
    return (
      <div
        role="status"
        className="flex flex-col items-center justify-center gap-2 rounded-sg-md border border-dashed border-sg-border bg-sg-inset px-6 py-10 text-center"
        data-testid="transcript-empty"
      >
        <MessageSquareDashed
          aria-hidden="true"
          className="h-6 w-6 text-sg-ink-4"
        />
        <span className="text-xs text-sg-ink-3">
          {t("sessions.transcriptEmpty")}
        </span>
      </div>
    );
  }

  return (
    <ol
      className="flex flex-col gap-3"
      aria-label="transcript"
      data-testid="transcript-list"
    >
      {transcript.map((m, idx) => (
        <TranscriptRow key={idx} message={m} index={idx} />
      ))}
    </ol>
  );
}

function TranscriptRow({
  message,
  index,
}: {
  message: TranscriptMessage;
  index: number;
}) {
  const { t } = useTranslation();
  const isUser = message.role === "user";
  const isAssistant = message.role === "assistant";
  const isSystem = message.role === "system";

  const roleLabel = isUser
    ? t("sessions.transcriptRoleUser")
    : isAssistant
      ? t("sessions.transcriptRoleAssistant")
      : t("sessions.transcriptRoleSystem");

  return (
    <li
      className={cn(
        "flex w-full",
        isUser ? "justify-end" : "justify-start",
      )}
      data-testid={`transcript-row-${index}`}
      data-role={message.role}
    >
      <div
        className={cn(
          "flex max-w-[85%] flex-col gap-1",
          isUser ? "items-end" : "items-start",
        )}
      >
        <div
          className={cn(
            "flex items-center gap-1.5 text-[11px] text-sg-ink-4",
            isUser ? "flex-row-reverse" : "flex-row",
          )}
        >
          {isUser ? (
            <User className="h-3 w-3" aria-hidden="true" />
          ) : (
            <Bot className="h-3 w-3" aria-hidden="true" />
          )}
          <span className="font-medium" data-testid="transcript-role">
            {roleLabel}
          </span>
          <span aria-hidden="true">·</span>
          <time
            dateTime={message.ts}
            className="font-mono text-[10px]"
            data-testid="transcript-ts"
          >
            {formatTs(message.ts)}
          </time>
        </div>
        <div
          className={cn(
            "whitespace-pre-wrap break-words rounded-sg-md border px-3 py-2 text-[13px] leading-relaxed",
            // Distinct treatment per role — user messages echo the cyan
            // accent the rest of the admin uses for "self" actions; the
            // assistant uses a neutral inset well; system messages get a
            // dimmer dashed treatment so they read as out-of-band notes.
            isUser && "border-sg-accent/30 bg-sg-accent-soft text-sg-ink",
            isAssistant && "border-sg-border bg-sg-inset text-sg-ink",
            isSystem &&
              "border-dashed border-sg-border bg-sg-inset italic text-sg-ink-3",
          )}
        >
          {message.content}
        </div>
      </div>
    </li>
  );
}
