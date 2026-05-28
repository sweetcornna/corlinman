"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";

export interface SlashCommand {
  id: string;
  label: string;
  description?: string;
  argHint?: string;
  run: () => string | void;
}

interface ComposerSlashMenuProps {
  query: string;
  commands: SlashCommand[];
  onPick: (cmd: SlashCommand) => void;
  onClose: () => void;
}

export function ComposerSlashMenu({
  query,
  commands,
  onPick,
  onClose,
}: ComposerSlashMenuProps) {
  const { t } = useTranslation();
  const q = query.toLowerCase();
  const filtered = React.useMemo(
    () =>
      commands.filter(
        (c) =>
          c.id.toLowerCase().includes(q) ||
          c.label.toLowerCase().includes(q),
      ),
    [commands, q],
  );

  const [active, setActive] = React.useState(0);
  React.useEffect(() => {
    setActive(0);
  }, [q]);

  const handleKeyDown = React.useCallback(
    (e: KeyboardEvent) => {
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
      } else if (e.key === "Enter") {
        e.preventDefault();
        onPick(filtered[active]);
      } else if (e.key === "Escape") {
        onClose();
      }
    },
    [filtered, active, onPick, onClose],
  );

  React.useEffect(() => {
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [handleKeyDown]);

  if (filtered.length === 0) return null;

  return (
    <ul
      className={cn(
        "absolute bottom-full left-0 z-20 mb-1 w-80 overflow-hidden rounded-md",
        "border border-tp-glass-edge bg-tp-glass-inner shadow-md",
      )}
      role="listbox"
      aria-label={t("chat.slashMenuAriaLabel")}
      data-testid="slash-menu"
    >
      {filtered.map((cmd, i) => (
        <li
          key={cmd.id}
          role="option"
          aria-selected={i === active}
          className={cn(
            "flex cursor-pointer items-center gap-2 px-2.5 py-1.5 text-[12px]",
            i === active && "bg-tp-amber/20",
          )}
          onMouseEnter={() => setActive(i)}
          onClick={() => onPick(cmd)}
        >
          <span className="font-mono text-tp-ink">/{cmd.id}</span>
          {cmd.argHint ? (
            <span className="font-mono text-tp-ink-3">{cmd.argHint}</span>
          ) : null}
          <span className="ml-auto truncate text-tp-ink-2">
            {cmd.description ?? cmd.label}
          </span>
        </li>
      ))}
    </ul>
  );
}
