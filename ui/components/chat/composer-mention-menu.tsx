"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";

export interface MentionCandidate {
  id: string;
  name: string;
  description?: string;
  kind?: "agent" | "skill" | "persona";
}

interface ComposerMentionMenuProps {
  query: string;
  candidates: MentionCandidate[];
  onPick: (c: MentionCandidate) => void;
  onClose: () => void;
}

export function ComposerMentionMenu({
  query,
  candidates,
  onPick,
  onClose,
}: ComposerMentionMenuProps) {
  const { t } = useTranslation();
  const q = query.toLowerCase();
  const filtered = React.useMemo(
    () =>
      candidates.filter(
        (c) =>
          c.name.toLowerCase().includes(q) ||
          c.id.toLowerCase().includes(q) ||
          (c.description ?? "").toLowerCase().includes(q),
      ),
    [candidates, q],
  );

  const [active, setActive] = React.useState(0);
  React.useEffect(() => {
    setActive(0);
  }, [q]);

  React.useEffect(() => {
    const handleKey = (e: KeyboardEvent): void => {
      if (filtered.length === 0) {
        if (e.key === "Escape") onClose();
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActive((i) => (i + 1) % filtered.length);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setActive((i) => (i - 1 + filtered.length) % filtered.length);
      } else if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        onPick(filtered[active]);
      } else if (e.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [filtered, active, onPick, onClose]);

  if (filtered.length === 0) return null;

  return (
    <ul
      className={cn(
        "absolute bottom-full left-0 z-30 mb-2 w-80 overflow-hidden",
        "sg-glass-overlay rounded-sg-md shadow-sg-4 animate-sg-palette-in",
      )}
      role="listbox"
      aria-label={t("chat.mentionMenuAriaLabel")}
      data-testid="mention-menu"
    >
      {filtered.map((c, i) => (
        <li
          key={c.id}
          role="option"
          aria-selected={i === active}
          className={cn(
            "relative flex cursor-pointer items-center gap-2 px-3 py-1.5 text-[12px]",
            "transition-colors",
            i === active
              ? "bg-sg-accent-soft before:absolute before:inset-y-1 before:left-0 before:w-0.5 before:rounded-full before:bg-sg-accent"
              : "hover:bg-sg-inset-hover",
          )}
          onMouseEnter={() => setActive(i)}
          onClick={() => onPick(c)}
        >
          <span className="font-mono text-sg-ink">@{c.name}</span>
          {c.kind ? (
            <span className="rounded border border-sg-border px-1 py-0 text-[10px] text-sg-ink-3">
              {c.kind}
            </span>
          ) : null}
          <span className="ml-auto truncate text-sg-ink-2">
            {c.description ?? c.id}
          </span>
        </li>
      ))}
    </ul>
  );
}

export function detectMentionQuery(
  text: string,
  caret: number,
): { query: string; start: number; end: number } | null {
  if (caret > text.length) return null;
  let i = caret - 1;
  while (i >= 0) {
    const ch = text[i];
    if (ch === "@") {
      if (i === 0 || /\s/.test(text[i - 1])) {
        const after = text.slice(i + 1, caret);
        if (/\s/.test(after)) return null;
        return { query: after, start: i, end: caret };
      }
      return null;
    }
    if (/\s/.test(ch)) return null;
    i -= 1;
  }
  return null;
}
