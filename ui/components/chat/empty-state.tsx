"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { motion, useReducedMotion } from "framer-motion";
import { Sparkles } from "@/components/icons";

import { useMotionVariants } from "@/lib/motion";
import { Mascot } from "@/components/ui/mascot";

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
        {/* The mascot carries its own CSS float loop (reduced-motion gated),
            so the spring entrance on this wrapper isn't clobbered. */}
        <Mascot size={96} still={Boolean(reducedMotion)} />
      </motion.div>
      <motion.h2
        variants={liquidRise}
        className="text-sg-ink text-2xl font-semibold tracking-tight"
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
              className=" inline-flex items-center gap-1.5 rounded-full border border-sg-border bg-sg-inset px-3.5 py-1.5 text-left text-[12px] text-sg-ink-3 hover:border-sg-accent/30 hover:bg-sg-accent-soft hover:text-sg-ink"
            >
              <Sparkles
                className="h-3 w-3 shrink-0 text-sg-accent"
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
