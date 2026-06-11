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

// Temporary highlight applied to the jumped-to message bubble. The class is
// added to `#chat-msg-<id>`, then removed after HIGHLIGHT_MS so the ring fades
// the hit into the surrounding thread. Bubbles live in a sibling component we
// don't own, so we reach them by id (the same id `onJump` scrolls to) and
// inject the rule via a scoped `<style>` rather than a Tailwind class.
const HIGHLIGHT_CLASS = "sg-search-hit";
const HIGHLIGHT_MS = 2000;

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
  // Element focused before the overlay opened — focus is restored here on
  // close so keyboard users land back where they started.
  const restoreFocusRef = React.useRef<HTMLElement | null>(null);
  // Tracks the element currently wearing the highlight + its removal timer so
  // a rapid next/prev jump clears the previous ring before adding a new one.
  const highlightedRef = React.useRef<HTMLElement | null>(null);
  const highlightTimerRef = React.useRef<number | null>(null);

  const openOverlay = React.useCallback(() => {
    const activeEl = document.activeElement;
    restoreFocusRef.current =
      activeEl instanceof HTMLElement ? activeEl : null;
    setOpen(true);
    window.requestAnimationFrame(() => inputRef.current?.focus());
  }, []);

  const closeOverlay = React.useCallback(() => {
    setOpen(false);
    // Restore focus to wherever it was before the overlay opened.
    const target = restoreFocusRef.current;
    restoreFocusRef.current = null;
    window.requestAnimationFrame(() => target?.focus?.());
  }, []);

  React.useEffect(() => {
    if (!bindHotkey) return;
    const handleKey = (e: KeyboardEvent) => {
      const isOpen = (e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "f";
      if (isOpen) {
        e.preventDefault();
        openOverlay();
      }
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [bindHotkey, openOverlay]);

  const matches = React.useMemo(() => {
    const q = query.toLowerCase().trim();
    if (!q) return [];
    return messages.filter((m) => m.content.toLowerCase().includes(q));
  }, [messages, query]);

  React.useEffect(() => {
    setActive(0);
  }, [query]);

  // Briefly ring the active match after jumping to it.
  const highlight = React.useCallback((messageId: string) => {
    if (typeof document === "undefined") return;
    if (highlightTimerRef.current !== null) {
      window.clearTimeout(highlightTimerRef.current);
      highlightTimerRef.current = null;
    }
    highlightedRef.current?.classList.remove(HIGHLIGHT_CLASS);
    const el = document.getElementById(`chat-msg-${messageId}`);
    if (!el) {
      highlightedRef.current = null;
      return;
    }
    el.classList.add(HIGHLIGHT_CLASS);
    highlightedRef.current = el;
    highlightTimerRef.current = window.setTimeout(() => {
      el.classList.remove(HIGHLIGHT_CLASS);
      if (highlightedRef.current === el) highlightedRef.current = null;
      highlightTimerRef.current = null;
    }, HIGHLIGHT_MS);
  }, []);

  React.useEffect(() => {
    if (matches.length === 0) return;
    const m = matches[Math.min(active, matches.length - 1)];
    if (m) {
      onJump(m.id);
      highlight(m.id);
    }
  }, [active, matches, onJump, highlight]);

  // Clean up a lingering highlight + timer on unmount.
  React.useEffect(
    () => () => {
      if (highlightTimerRef.current !== null) {
        window.clearTimeout(highlightTimerRef.current);
      }
      highlightedRef.current?.classList.remove(HIGHLIGHT_CLASS);
    },
    [],
  );

  if (!open) return null;

  return (
    <div
      className={cn(
        "pointer-events-auto absolute right-4 top-4 z-30 flex items-center gap-1",
        "sg-glass-overlay rounded-sg-lg px-2 py-1.5 text-[12px] shadow-sg-4",
        "animate-sg-palette-in",
      )}
      role="search"
      aria-label={t("chat.searchOverlayAriaLabel")}
      data-testid="conversation-search"
    >
      {/* Scoped highlight ring for the jumped-to bubble (added/removed via
        * classList on `#chat-msg-<id>`, see `highlight`). */}
      <style
        // eslint-disable-next-line react/no-danger
        dangerouslySetInnerHTML={{
          __html: `.${HIGHLIGHT_CLASS}{border-radius:var(--sg-radius-lg,12px);box-shadow:0 0 0 2px var(--sg-accent,#6aa3ff),0 0 0 6px color-mix(in srgb,var(--sg-accent,#6aa3ff) 28%,transparent);transition:box-shadow .18s ease-out;}@media (prefers-reduced-motion:reduce){.${HIGHLIGHT_CLASS}{transition:none;}}`,
        }}
      />
      <Search className="h-3.5 w-3.5 text-sg-ink-4" aria-hidden="true" />
      <input
        ref={inputRef}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            e.preventDefault();
            closeOverlay();
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
        className={cn(
          "w-48 rounded-sg-sm bg-transparent text-sg-ink placeholder:text-sg-ink-5",
          "focus:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/50",
        )}
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
        onClick={closeOverlay}
        className="rounded-sg-sm p-0.5 text-sg-ink-4 hover:bg-sg-inset hover:text-sg-ink"
        aria-label={t("chat.searchCloseAriaLabel")}
        data-testid="conversation-search-close"
      >
        <X className="h-3.5 w-3.5" aria-hidden="true" />
      </button>
    </div>
  );
}
