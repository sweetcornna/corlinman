"use client";

import { Search } from "@/components/icons";
import { useTranslation } from "react-i18next";
import { useCommandPalette } from "@/components/cmdk-palette";

/** The "Search... ⌘K" pill in the topnav — opens the command palette. */
export function SearchTrigger() {
  const { toggle } = useCommandPalette();
  const { t } = useTranslation();
  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={t("nav.openPalette")}
      className="group flex h-10 min-w-10 items-center justify-center gap-2 rounded-full border border-sg-border bg-sg-inset px-2.5 text-[12.5px] text-sg-ink-3 transition-colors hover:border-sg-border-strong hover:bg-sg-inset-hover hover:text-sg-ink-2 md:h-8 md:w-64 md:justify-between"
    >
      <span className="inline-flex items-center gap-2">
        <Search className="h-3.5 w-3.5" />
        <span className="hidden md:inline">{t("nav.searchPlaceholder")}</span>
      </span>
      <kbd className="hidden rounded-sg-sm border border-sg-border bg-sg-inset-strong px-1.5 py-0.5 font-mono text-[10px] text-sg-ink-4 md:inline-flex">
        ⌘K
      </kbd>
    </button>
  );
}
