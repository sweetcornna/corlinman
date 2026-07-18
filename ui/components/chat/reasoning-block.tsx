"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { Brain, ChevronDown, ChevronRight } from "@/components/icons";

import { cn } from "@/lib/utils";
import { springs } from "@/lib/motion";

interface ReasoningBlockProps {
  text: string;
  streaming?: boolean;
}

export function ReasoningBlock({ text, streaming }: ReasoningBlockProps) {
  const { t } = useTranslation();
  const reducedMotion = useReducedMotion();
  const [expanded, setExpanded] = React.useState(false);
  return (
    <div
      className={cn(
        "my-2 border-l-2 border-sg-accent-2/40 pl-3",
        streaming && "shimmer rounded-sg-sm",
      )}
      data-testid="reasoning-block"
    >
      <button
        type="button"
        className="flex w-full items-center gap-2 py-1 text-left text-[11px] italic text-sg-ink-3 hover:text-sg-ink-2"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        {expanded ? (
          <ChevronDown className="h-3 w-3 not-italic" aria-hidden="true" />
        ) : (
          <ChevronRight className="h-3 w-3 not-italic" aria-hidden="true" />
        )}
        <Brain className="h-3 w-3 not-italic" aria-hidden="true" />
        <span>
          {streaming ? t("chat.reasoningStreaming") : t("chat.reasoningTitle")}
        </span>
        <span className="ml-auto font-mono not-italic text-sg-ink-5">
          {t("chat.reasoningCharCount", { n: text.length })}
        </span>
      </button>
      <AnimatePresence initial={false}>
        {expanded ? (
          <motion.div
            key="reasoning-body"
            initial={reducedMotion ? false : { height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={reducedMotion ? { opacity: 0 } : { height: 0, opacity: 0 }}
            transition={reducedMotion ? { duration: 0 } : springs.soft}
            className="overflow-hidden"
          >
            <div className="py-1 font-mono text-[11px] leading-relaxed whitespace-pre-wrap italic text-sg-ink-3">
              {text}
            </div>
          </motion.div>
        ) : null}
      </AnimatePresence>
    </div>
  );
}
