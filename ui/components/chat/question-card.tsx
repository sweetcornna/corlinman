"use client";

/**
 * QuestionCard — renders an `ask_user` clarification question in the thread.
 *
 * Web counterpart of Telegram's inline-keyboard buttons: the agent's canned
 * options become tappable pills. Picking one (or submitting a multi-select)
 * sends the label(s) as the user's next message — the reply then arrives as
 * a normal turn, exactly like typing it. On settled history turns the card
 * is inert: the conversation already moved on, so the options render dimmed.
 *
 * The question text itself is usually repeated in the assistant bubble (the
 * tool contract tells the model to finalize with it), so the bubble decides
 * whether to show the question line here (`showQuestion`).
 */

import * as React from "react";
import { useTranslation } from "react-i18next";
import { Check, Send } from "@/components/icons";

import { cn } from "@/lib/utils";
import type { AskUserQuestion } from "@/lib/chat/ask-user";

interface QuestionCardProps {
  question: AskUserQuestion;
  /** Clickable only while this is the thread's live tail. */
  interactive: boolean;
  /** Render the question line (bubble text didn't already carry it). */
  showQuestion?: boolean;
  onAnswer?: (text: string) => void;
}

export function QuestionCard({
  question,
  interactive,
  showQuestion = false,
  onAnswer,
}: QuestionCardProps) {
  const { t } = useTranslation();
  const [picked, setPicked] = React.useState<string[]>([]);
  // Latch after sending so a slow turn start can't double-submit.
  const [sent, setSent] = React.useState(false);
  const active = interactive && !sent && Boolean(onAnswer);

  const send = React.useCallback(
    (labels: string[]) => {
      if (!onAnswer || labels.length === 0) return;
      setSent(true);
      onAnswer(labels.join("、"));
    },
    [onAnswer],
  );

  const toggle = (label: string) => {
    if (!active) return;
    if (!question.multiple) {
      setPicked([label]);
      send([label]);
      return;
    }
    setPicked((prev) =>
      prev.includes(label)
        ? prev.filter((l) => l !== label)
        : [...prev, label],
    );
  };

  if (question.options.length === 0 && !showQuestion) return null;

  return (
    <div
      className="w-fit max-w-[min(600px,86%)] self-start"
      data-testid="question-card"
    >
      {showQuestion ? (
        <div className="mb-2 text-[14px] leading-relaxed text-sg-ink-2">
          {question.question}
        </div>
      ) : null}
      {question.options.length > 0 ? (
        <div className="flex flex-wrap items-center gap-2">
          {question.options.map((label) => {
            const isPicked = picked.includes(label);
            return (
              <button
                key={label}
                type="button"
                disabled={!active}
                onClick={() => toggle(label)}
                data-testid="question-option"
                aria-pressed={isPicked}
                className={cn(
                  "inline-flex min-h-[36px] items-center gap-1.5 rounded-full border px-3.5 py-1.5 text-[13px] transition-colors",
                  isPicked
                    ? "border-transparent bg-sg-tint font-medium text-sg-tint-ink"
                    : "border-sg-border-ghost bg-transparent text-sg-ink-2",
                  active && !isPicked && "hover:bg-sg-inset-hover",
                  !active && !isPicked && "opacity-50",
                )}
              >
                {isPicked ? (
                  <Check className="h-3.5 w-3.5" aria-hidden="true" />
                ) : null}
                {label}
              </button>
            );
          })}
          {question.multiple && active ? (
            <button
              type="button"
              disabled={picked.length === 0}
              onClick={() => send(picked)}
              data-testid="question-submit"
              className={cn(
                "inline-flex min-h-[36px] items-center gap-1.5 rounded-full bg-sg-tint px-3.5 py-1.5 text-[13px] font-medium text-sg-tint-ink",
                picked.length === 0
                  ? "opacity-40"
                  : "hover:bg-sg-tint/90",
              )}
            >
              <Send className="h-3.5 w-3.5" aria-hidden="true" />
              {t("chat.questionSubmit")}
            </button>
          ) : null}
        </div>
      ) : null}
      {question.multiple && active && question.options.length > 0 ? (
        <div className="mt-1.5 text-[11.5px] text-sg-ink-5">
          {t("chat.questionMultipleHint")}
        </div>
      ) : null}
    </div>
  );
}
