"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { Bot, Sparkles } from "lucide-react";

interface ChatEmptyStateProps {
  onPick?: (text: string) => void;
}

export function ChatEmptyState({ onPick }: ChatEmptyStateProps) {
  const { t } = useTranslation();
  const suggestions = React.useMemo(
    () => [
      t("chat.emptySuggestion1"),
      t("chat.emptySuggestion2"),
      t("chat.emptySuggestion3"),
      t("chat.emptySuggestion4"),
    ],
    [t],
  );
  return (
    <div
      className="mx-auto flex max-w-lg flex-col items-center gap-4 text-center animate-sg-rise"
      data-testid="chat-empty"
    >
      <div className="flex h-14 w-14 items-center justify-center rounded-sg-xl border border-sg-border bg-sg-accent-soft shadow-sg-glow">
        <Bot className="h-7 w-7 text-sg-accent" aria-hidden="true" />
      </div>
      <h2 className="sg-grad-text text-2xl font-semibold tracking-tight">
        {t("chat.emptyTitle")}
      </h2>
      <p className="max-w-md text-[13px] leading-relaxed text-sg-ink-3">
        {t("chat.emptySubtitle")}
      </p>
      <ul
        className="mt-1 flex w-full flex-wrap justify-center gap-2"
        aria-label={t("chat.emptySuggestionsAriaLabel")}
      >
        {suggestions.map((s) => (
          <li key={s}>
            <button
              type="button"
              onClick={() => onPick?.(s)}
              className="inline-flex items-center gap-1.5 rounded-full border border-sg-border bg-sg-inset px-3.5 py-1.5 text-left text-[12px] text-sg-ink-3 transition hover:border-sg-accent/30 hover:bg-sg-accent-soft hover:text-sg-ink"
            >
              <Sparkles
                className="h-3 w-3 shrink-0 text-sg-accent"
                aria-hidden="true"
              />
              <span className="max-w-[220px] truncate">{s}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
