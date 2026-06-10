"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { Brain, ChevronDown, ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";

interface ReasoningBlockProps {
  text: string;
  streaming?: boolean;
}

export function ReasoningBlock({ text, streaming }: ReasoningBlockProps) {
  const { t } = useTranslation();
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
      {expanded ? (
        <div className="py-1 font-mono text-[11px] leading-relaxed whitespace-pre-wrap italic text-sg-ink-3">
          {text}
        </div>
      ) : null}
    </div>
  );
}
