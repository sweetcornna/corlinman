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
      className="mx-auto flex max-w-md flex-col items-center gap-3 text-center"
      data-testid="chat-empty"
    >
      <div className="rounded-full border border-tp-glass-edge bg-tp-glass-inner p-3">
        <Bot className="h-6 w-6 text-tp-amber" aria-hidden="true" />
      </div>
      <h2 className="text-base font-semibold text-tp-ink">
        {t("chat.emptyTitle")}
      </h2>
      <p className="text-[12px] text-tp-ink-3">{t("chat.emptySubtitle")}</p>
      <ul
        className="mt-2 flex w-full flex-col gap-1.5"
        aria-label={t("chat.emptySuggestionsAriaLabel")}
      >
        {suggestions.map((s) => (
          <li key={s}>
            <button
              type="button"
              onClick={() => onPick?.(s)}
              className="flex w-full items-center gap-1.5 rounded-md border border-tp-glass-edge bg-tp-glass-inner/40 px-2.5 py-1.5 text-left text-[12px] text-tp-ink-2 hover:border-tp-amber/40 hover:text-tp-ink"
            >
              <Sparkles
                className="h-3 w-3 shrink-0 text-tp-ink-3"
                aria-hidden="true"
              />
              <span className="truncate">{s}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
