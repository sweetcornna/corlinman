"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { motion } from "framer-motion";
import { ImagePlus } from "lucide-react";

import { cn } from "@/lib/utils";
import { useMotionVariants } from "@/lib/motion";

const RECENT_KEY = "corlinman:chat:recent-emoji";
const RECENT_MAX = 16;

/** Sentinel category id whose cell opens the image file picker (表情包). */
const STICKER_CATEGORY = "sticker";

interface EmojiCategory {
  /** i18n key under `chat.emojiCat.*`. */
  id: string;
  /** Curated unicode emoji glyphs (~24 each). */
  emoji: string[];
}

const CATEGORIES: EmojiCategory[] = [
  {
    id: "smileys",
    emoji: [
      "😀", "😄", "😁", "😆", "😅", "😂", "🤣", "😊",
      "😇", "🙂", "😉", "😍", "🥰", "😘", "😋", "😜",
      "🤪", "🤗", "🤔", "😴", "😎", "🥳", "😭", "😤",
    ],
  },
  {
    id: "gestures",
    emoji: [
      "👍", "👎", "👌", "✌️", "🤞", "🤟", "🤘", "👏",
      "🙌", "👐", "🤝", "🙏", "✍️", "💪", "👈", "👉",
      "👆", "👇", "☝️", "✋", "🤚", "🖐️", "👋", "🤙",
    ],
  },
  {
    id: "mood",
    emoji: [
      "❤️", "🧡", "💛", "💚", "💙", "💜", "🖤", "🤍",
      "💔", "💕", "💞", "💓", "💗", "💖", "💘", "💝",
      "✨", "⭐", "🌟", "💫", "🔥", "💥", "💯", "🎉",
    ],
  },
  {
    id: "animals",
    emoji: [
      "🐶", "🐱", "🐭", "🐹", "🐰", "🦊", "🐻", "🐼",
      "🐨", "🐯", "🦁", "🐮", "🐷", "🐸", "🐵", "🐔",
      "🐧", "🐦", "🦄", "🐝", "🦋", "🐢", "🐬", "🐳",
    ],
  },
  {
    id: "food",
    emoji: [
      "🍎", "🍊", "🍋", "🍌", "🍉", "🍇", "🍓", "🍑",
      "🍍", "🥝", "🍅", "🥑", "🌽", "🍔", "🍟", "🍕",
      "🌭", "🍿", "🍣", "🍜", "🍰", "🍦", "🍩", "☕",
    ],
  },
  {
    id: "activity",
    emoji: [
      "⚽", "🏀", "🏈", "⚾", "🎾", "🏐", "🏉", "🎱",
      "🏓", "🏸", "🥅", "⛳", "🎯", "🎮", "🎲", "🎸",
      "🎹", "🎺", "🎻", "🎤", "🎬", "🏆", "🥇", "🎨",
    ],
  },
  {
    id: "objects",
    emoji: [
      "📱", "💻", "⌨️", "🖥️", "🖨️", "🕹️", "💡", "🔋",
      "📷", "🎥", "📺", "📻", "⏰", "⌚", "📚", "📝",
      "✏️", "📌", "📎", "🔑", "🔒", "💰", "💎", "🎁",
    ],
  },
  {
    id: "symbols",
    emoji: [
      "✅", "❌", "❓", "❗", "⚠️", "♻️", "🔝", "🔚",
      "🆗", "🆕", "🔥", "💤", "🚫", "✔️", "➕", "➖",
      "❤️‍🔥", "💢", "💬", "🔔", "📢", "🎵", "🌈", "⚡",
    ],
  },
];

function readRecent(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(RECENT_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((x): x is string => typeof x === "string").slice(0, RECENT_MAX);
  } catch {
    return [];
  }
}

function writeRecent(list: string[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, RECENT_MAX)));
  } catch {
    // ignore quota / privacy-mode errors
  }
}

interface EmojiPickerProps {
  /** Insert a unicode emoji at the textarea caret. */
  onPick: (emoji: string) => void;
  /** Open the image file input (表情包 / sticker entry). */
  onPickSticker: () => void;
  /** Close + return focus to the textarea. */
  onClose: () => void;
}

interface Cell {
  /** Unicode glyph, or null for the sticker entry cell. */
  emoji: string | null;
  /** Recorded into recent on pick (emoji cells only). */
  record: boolean;
}

export function EmojiPicker({ onPick, onPickSticker, onClose }: EmojiPickerProps) {
  const { t } = useTranslation();
  const { liquidSurface } = useMotionVariants();
  const [recent, setRecent] = React.useState<string[]>(() => readRecent());
  const [active, setActive] = React.useState(0);
  const gridRef = React.useRef<HTMLDivElement | null>(null);

  // Number of columns in the grid (used for arrow-key navigation).
  const COLS = 8;

  // Flatten every cell into one array for keyboard navigation. Order:
  // recent (if any) → each category → sticker entry.
  const cells = React.useMemo<Cell[]>(() => {
    const out: Cell[] = [];
    for (const e of recent) out.push({ emoji: e, record: true });
    for (const cat of CATEGORIES) {
      for (const e of cat.emoji) out.push({ emoji: e, record: true });
    }
    out.push({ emoji: null, record: false });
    return out;
  }, [recent]);

  const recordRecent = React.useCallback((emoji: string) => {
    setRecent((prev) => {
      const next = [emoji, ...prev.filter((e) => e !== emoji)].slice(0, RECENT_MAX);
      writeRecent(next);
      return next;
    });
  }, []);

  const pickCell = React.useCallback(
    (cell: Cell) => {
      if (cell.emoji === null) {
        onPickSticker();
        return;
      }
      onPick(cell.emoji);
      if (cell.record) recordRecent(cell.emoji);
    },
    [onPick, onPickSticker, recordRecent],
  );

  const handleKeyDown = React.useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      if (cells.length === 0) {
        if (e.key === "Escape") onClose();
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        setActive((i) => Math.min(i + 1, cells.length - 1));
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        setActive((i) => Math.max(i - 1, 0));
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        setActive((i) => Math.min(i + COLS, cells.length - 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setActive((i) => Math.max(i - COLS, 0));
      } else if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        pickCell(cells[active]);
      }
    },
    [cells, active, pickCell, onClose],
  );

  // Roving tabindex: keep the active cell scrolled into view AND move DOM
  // focus to it, so the browser's focus ring follows the selection and
  // screen readers announce the active option. We deliberately don't focus
  // on the very first mount frame here — the mount effect below handles the
  // initial focus once so opening the picker doesn't yank the page scroll.
  const mounted = React.useRef(false);
  React.useEffect(() => {
    const grid = gridRef.current;
    if (!grid) return;
    const el = grid.querySelector<HTMLElement>(`[data-emoji-index="${active}"]`);
    el?.scrollIntoView?.({ block: "nearest" });
    if (mounted.current) el?.focus?.();
  }, [active]);

  // Focus the first cell on mount so arrow keys work immediately, then mark
  // mounted so subsequent `active` changes move focus too.
  React.useEffect(() => {
    const grid = gridRef.current;
    const el = grid?.querySelector<HTMLElement>(`[data-emoji-index="${active}"]`);
    el?.focus?.();
    mounted.current = true;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  let cellIndex = 0;
  const renderCell = (cell: Cell, label: string, key: string) => {
    const idx = cellIndex++;
    const isActive = idx === active;
    return (
      <button
        key={key}
        type="button"
        role="option"
        aria-selected={isActive}
        data-testid="emoji-item"
        data-emoji-index={idx}
        // Roving tabindex — only the active cell is in the tab order; Tab
        // moves focus out of the grid, arrow keys move within it.
        tabIndex={isActive ? 0 : -1}
        aria-label={label}
        onClick={() => pickCell(cell)}
        onMouseEnter={() => setActive(idx)}
        className={cn(
          "lg-gel flex h-9 w-9 items-center justify-center rounded-md text-xl leading-none",
          "hover:bg-sg-accent-soft focus:outline-none",
          isActive && "bg-sg-accent-soft ring-1 ring-sg-accent/40",
          cell.emoji === null && "text-sg-accent",
        )}
      >
        {cell.emoji ?? <ImagePlus className="h-5 w-5" aria-hidden="true" />}
      </button>
    );
  };

  return (
    <motion.div
      role="listbox"
      aria-label={t("chat.emojiPickerAriaLabel")}
      data-testid="emoji-picker"
      ref={gridRef}
      onKeyDown={handleKeyDown}
      initial="hidden"
      animate="visible"
      variants={liquidSurface}
      style={{ transformOrigin: "bottom left" }}
      className={cn(
        "absolute bottom-full left-0 z-30 mb-2 w-[20.5rem] max-h-80 overflow-y-auto",
        "sg-glass-overlay rounded-sg-lg p-2 shadow-sg-4 outline-none",
      )}
    >
      {recent.length > 0 ? (
        <section className="mb-1">
          <h3 className="px-1.5 py-1 text-[11px] font-medium text-sg-ink-3">
            {t("chat.emojiRecent")}
          </h3>
          <div className="grid grid-cols-8 gap-0.5">
            {recent.map((e, i) =>
              renderCell({ emoji: e, record: true }, e, `recent-${e}-${i}`),
            )}
          </div>
        </section>
      ) : null}

      {CATEGORIES.map((cat) => (
        <section key={cat.id} className="mb-1">
          <h3 className="px-1.5 py-1 text-[11px] font-medium text-sg-ink-3">
            {t(`chat.emojiCat.${cat.id}`)}
          </h3>
          <div className="grid grid-cols-8 gap-0.5">
            {cat.emoji.map((e) =>
              renderCell({ emoji: e, record: true }, e, `${cat.id}-${e}`),
            )}
          </div>
        </section>
      ))}

      <section>
        <h3 className="px-1.5 py-1 text-[11px] font-medium text-sg-ink-3">
          {t("chat.emojiCat.sticker")}
        </h3>
        <div className="grid grid-cols-8 gap-0.5">
          {renderCell(
            { emoji: null, record: false },
            t("chat.emojiStickerHint"),
            STICKER_CATEGORY,
          )}
        </div>
        <p className="px-1.5 pt-0.5 text-[10px] leading-tight text-sg-ink-4">
          {t("chat.emojiStickerHint")}
        </p>
      </section>
    </motion.div>
  );
}
