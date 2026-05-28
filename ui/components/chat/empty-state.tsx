"use client";

import * as React from "react";
import { Bot, Sparkles } from "lucide-react";

const SUGGESTIONS = [
  "Explain the architecture of this project",
  "Write a Python function that…",
  "Summarize the latest changes on this branch",
  "Search the codebase for…",
];

interface ChatEmptyStateProps {
  onPick?: (text: string) => void;
}

export function ChatEmptyState({ onPick }: ChatEmptyStateProps) {
  return (
    <div
      className="mx-auto flex max-w-md flex-col items-center gap-3 text-center"
      data-testid="chat-empty"
    >
      <div className="rounded-full border border-tp-glass-edge bg-tp-glass-inner p-3">
        <Bot className="h-6 w-6 text-tp-amber" aria-hidden="true" />
      </div>
      <h2 className="text-base font-semibold text-tp-ink">
        Start a new conversation
      </h2>
      <p className="text-[12px] text-tp-ink-3">
        Ask the agent anything. It can read code, run tools, spawn sub-agents,
        and ask for approval before high-risk actions.
      </p>
      <ul className="mt-2 flex w-full flex-col gap-1.5" aria-label="suggested prompts">
        {SUGGESTIONS.map((s) => (
          <li key={s}>
            <button
              type="button"
              onClick={() => onPick?.(s)}
              className="flex w-full items-center gap-1.5 rounded-md border border-tp-glass-edge bg-tp-glass-inner/40 px-2.5 py-1.5 text-left text-[12px] text-tp-ink-2 hover:border-tp-amber/40 hover:text-tp-ink"
            >
              <Sparkles className="h-3 w-3 shrink-0 text-tp-ink-3" aria-hidden="true" />
              <span className="truncate">{s}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
