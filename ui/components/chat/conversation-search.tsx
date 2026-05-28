"use client";

/**
 * In-conversation search overlay (Cmd/Ctrl+F).
 *
 * Lightweight client-side text search across the rendered messages.
 * Surfaces matches with prev/next navigation and the current-match
 * counter. Doesn't highlight in-place yet (that requires reaching into
 * MarkdownMessage); for MVP we scroll the matching bubble into view.
 */

import * as React from "react";
import { ChevronDown, ChevronUp, Search, X } from "lucide-react";

import { cn } from "@/lib/utils";
import type { ChatMessage } from "@/lib/chat/types";

interface ConversationSearchProps {
  messages: ChatMessage[];
  /** Called with the message id of the current match so the parent can
   *  scroll the bubble into view. */
  onJump: (messageId: string) => void;
  /** Bind to a window listener so Cmd/Ctrl+F opens the overlay. */
  bindHotkey?: boolean;
}

export function ConversationSearch({
  messages,
  onJump,
  bindHotkey,
}: ConversationSearchProps) {
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState("");
  const [active, setActive] = React.useState(0);
  const inputRef = React.useRef<HTMLInputElement | null>(null);

  // Cmd+F opens; Escape closes.
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
        "rounded-md border border-tp-glass-edge bg-tp-glass-inner px-1.5 py-1 text-[12px] shadow-md",
      )}
      role="search"
      aria-label="Search this conversation"
      data-testid="conversation-search"
    >
      <Search className="h-3.5 w-3.5 text-tp-ink-3" aria-hidden="true" />
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
        placeholder="Search in conversation"
        className="w-48 bg-transparent text-tp-ink placeholder:text-tp-ink-3 focus:outline-none"
        data-testid="conversation-search-input"
      />
      <span className="min-w-[40px] text-right font-mono text-[11px] text-tp-ink-3">
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
        className="rounded p-0.5 text-tp-ink-3 hover:bg-tp-glass-inner hover:text-tp-ink disabled:opacity-30"
        aria-label="Previous match"
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
        className="rounded p-0.5 text-tp-ink-3 hover:bg-tp-glass-inner hover:text-tp-ink disabled:opacity-30"
        aria-label="Next match"
      >
        <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
      </button>
      <button
        type="button"
        onClick={() => setOpen(false)}
        className="rounded p-0.5 text-tp-ink-3 hover:bg-tp-glass-inner hover:text-tp-ink"
        aria-label="Close search"
        data-testid="conversation-search-close"
      >
        <X className="h-3.5 w-3.5" aria-hidden="true" />
      </button>
    </div>
  );
}
