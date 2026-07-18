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
    <div className="my-2" data-testid="reasoning-block">
      {/* The quietest element on screen: a dashed chip; the streaming
          affordance is the bubble's thread, not this chip. */}
      <button
        type="button"
        className={cn(
          "flex w-full items-center gap-2 rounded-st-pill border border-dashed border-sg-border-strong",
          "px-3 py-1 text-left text-[11px] text-sg-ink-4 hover:text-sg-ink-2",
        )}
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        {expanded ? (
          <ChevronDown className="h-3 w-3" aria-hidden="true" />
        ) : (
          <ChevronRight className="h-3 w-3" aria-hidden="true" />
        )}
        <Brain className="h-3 w-3" aria-hidden="true" />
        <span>
          {streaming ? t("chat.reasoningStreaming") : t("chat.reasoningTitle")}
        </span>
        <span className="ml-auto font-mono text-sg-ink-5">
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
            <div className="mt-1.5 border-l-2 border-sg-border-strong py-1 pl-3 text-[12.5px] leading-[1.7] whitespace-pre-wrap text-sg-ink-4">
              {text}
            </div>
          </motion.div>
        ) : null}
      </AnimatePresence>
    </div>
  );
}
