"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { motion, useReducedMotion } from "framer-motion";
import { Sparkles } from "@/components/icons";

import { useMotionVariants } from "@/lib/motion";
import { PresenceOrb } from "@/components/ui/presence-orb";

interface ChatEmptyStateProps {
  onPick?: (text: string) => void;
}

export function ChatEmptyState({ onPick }: ChatEmptyStateProps) {
  const { t } = useTranslation();
  const { liquidRise, liquidStagger } = useMotionVariants();
  const reducedMotion = useReducedMotion();
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
    <motion.div
      initial="hidden"
      animate="visible"
      variants={liquidStagger}
      className="mx-auto flex max-w-lg flex-col items-center gap-4 text-center"
      data-testid="chat-empty"
    >
      <motion.div variants={liquidRise}>
        {/* Hero eclipse pearl — the empty chat's one lively element
            (eclipse-turn is reduced-motion gated in globals.css). */}
        <PresenceOrb size="hero" active={!reducedMotion} />
      </motion.div>
      <motion.h2
        variants={liquidRise}
        className="text-sg-ink font-display text-2xl font-medium tracking-[0.01em]"
      >
        {t("chat.emptyTitle")}
      </motion.h2>
      <motion.p
        variants={liquidRise}
        className="max-w-md text-[13px] leading-relaxed text-sg-ink-3"
      >
        {t("chat.emptySubtitle")}
      </motion.p>
      <motion.ul
        variants={liquidStagger}
        className="mt-1 flex w-full flex-wrap justify-center gap-2"
        aria-label={t("chat.emptySuggestionsAriaLabel")}
      >
        {suggestions.map((s) => (
          <motion.li key={s} variants={liquidRise}>
            <button
              type="button"
              onClick={() => onPick?.(s)}
              className="inline-flex items-center gap-1.5 rounded-full border border-sg-border bg-sg-inset px-3.5 py-1.5 text-left text-[12px] text-sg-ink-3 hover:border-sg-border-strong hover:bg-sg-inset-hover hover:text-sg-ink"
            >
              <Sparkles
                className="h-3 w-3 shrink-0 text-sg-ink-4"
                aria-hidden="true"
              />
              <span className="max-w-[220px] truncate">{s}</span>
            </button>
          </motion.li>
        ))}
      </motion.ul>
    </motion.div>
  );
}
