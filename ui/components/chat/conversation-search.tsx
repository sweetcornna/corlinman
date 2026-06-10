"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, ChevronUp, Search, X } from "lucide-react";

import { cn } from "@/lib/utils";
import type { ChatMessage } from "@/lib/chat/types";

interface ConversationSearchProps {
  messages: ChatMessage[];
  onJump: (messageId: string) => void;
  bindHotkey?: boolean;
}

export function ConversationSearch({
  messages,
  onJump,
  bindHotkey,
}: ConversationSearchProps) {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState("");
  const [active, setActive] = React.useState(0);
  const inputRef = React.useRef<HTMLInputElement | null>(null);

  React.useEffect(() => {
    if (!bindHotkey) return;
    const handleKey = (e: KeyboardEvent) => {
      const isOpen = (e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "f";
      if (isOpen) {
        e.preventDefault();
        setOpen(true);
        window.requestAnimationFrame(() => inputRef.current?.focus());
      }
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [bindHotkey]);

  const matches = React.useMemo(() => {
    const q = query.toLowerCase().trim();
    if (!q) return [];
    return messages.filter((m) => m.content.toLowerCase().includes(q));
  }, [messages, query]);

  React.useEffect(() => {
    setActive(0);
  }, [query]);

  React.useEffect(() => {
    if (matches.length === 0) return;
    const m = matches[Math.min(active, matches.length - 1)];
    if (m) onJump(m.id);
  }, [active, matches, onJump]);

  if (!open) return null;

  return (
    <div
      className={cn(
        "pointer-events-auto absolute right-4 top-4 z-30 flex items-center gap-1",
        "sg-glass-overlay rounded-sg-lg px-2 py-1.5 text-[12px] shadow-sg-4",
        "animate-tp-palette-in",
      )}
      role="search"
      aria-label={t("chat.searchOverlayAriaLabel")}
      data-testid="conversation-search"
    >
      <Search className="h-3.5 w-3.5 text-sg-ink-4" aria-hidden="true" />
      <input
        ref={inputRef}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            e.preventDefault();
            setOpen(false);
          } else if (e.key === "Enter") {
            e.preventDefault();
            if (matches.length === 0) return;
            setActive((i) =>
              e.shiftKey
                ? (i - 1 + matches.length) % matches.length
                : (i + 1) % matches.length,
            );
          }
        }}
        placeholder={t("chat.searchPlaceholderConversation")}
        className="w-48 bg-transparent text-sg-ink placeholder:text-sg-ink-5 focus:outline-none"
        data-testid="conversation-search-input"
      />
      <span className="min-w-[40px] text-right font-mono text-[11px] text-sg-ink-4">
        {matches.length === 0
          ? query
            ? "0/0"
            : ""
          : `${Math.min(active + 1, matches.length)}/${matches.length}`}
      </span>
      <button
        type="button"
        onClick={() =>
          setActive((i) =>
            matches.length === 0
              ? 0
              : (i - 1 + matches.length) % matches.length,
          )
        }
        disabled={matches.length === 0}
        className="rounded-sg-sm p-0.5 text-sg-ink-4 hover:bg-sg-inset hover:text-sg-ink disabled:opacity-30"
        aria-label={t("chat.searchPrevAriaLabel")}
      >
        <ChevronUp className="h-3.5 w-3.5" aria-hidden="true" />
      </button>
      <button
        type="button"
        onClick={() =>
          setActive((i) =>
            matches.length === 0 ? 0 : (i + 1) % matches.length,
          )
        }
        disabled={matches.length === 0}
        className="rounded-sg-sm p-0.5 text-sg-ink-4 hover:bg-sg-inset hover:text-sg-ink disabled:opacity-30"
        aria-label={t("chat.searchNextAriaLabel")}
      >
        <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
      </button>
      <button
        type="button"
        onClick={() => setOpen(false)}
        className="rounded-sg-sm p-0.5 text-sg-ink-4 hover:bg-sg-inset hover:text-sg-ink"
        aria-label={t("chat.searchCloseAriaLabel")}
        data-testid="conversation-search-close"
      >
        <X className="h-3.5 w-3.5" aria-hidden="true" />
      </button>
    </div>
  );
}
