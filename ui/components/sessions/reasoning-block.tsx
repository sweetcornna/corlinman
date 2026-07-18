/**
 * Collapsible "Thinking" block. Streams render with a soft accent shimmer
 * while the underlying block isn't yet `done`; once finished the shimmer
 * stops and the block can be collapsed.
 *
 * Spatial Glass: faux-glass card, dimmed mono body in a sunken well.
 */
"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, ChevronRight, Sparkles } from "@/components/icons";

import { cn } from "@/lib/utils";

export interface ReasoningBlockProps {
  text: string;
  streaming: boolean;
  defaultOpen?: boolean;
}

export function ReasoningBlock({ text, streaming, defaultOpen }: ReasoningBlockProps) {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState<boolean>(defaultOpen ?? streaming);

  // While streaming we always keep the latest text visible
  React.useEffect(() => {
    if (streaming) setOpen(true);
  }, [streaming]);

  return (
    <div
      data-testid="reasoning-block"
      data-streaming={streaming ? "true" : "false"}
      className="rounded-sg-md border border-sg-border bg-sg-card-grad shadow-sg-1"
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={cn(
          "flex w-full items-center gap-2 rounded-sg-md px-3 py-2 text-left text-xs font-medium",
          "text-sg-ink-2 transition-colors hover:bg-sg-accent-soft",
        )}
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown className="size-3.5 shrink-0 text-sg-ink-4" aria-hidden />
        ) : (
          <ChevronRight className="size-3.5 shrink-0 text-sg-ink-4" aria-hidden />
        )}
        <Sparkles
          className={cn(
            "size-3.5 shrink-0 text-sg-ink-4",
            streaming && "animate-pulse text-sg-accent",
          )}
          aria-hidden
        />
        <span>
          {streaming ? t("sessions.reasoning.thinking") : t("sessions.reasoning.thought")}
        </span>
      </button>
      {open && (
        <div
          data-testid="reasoning-block-body"
          className={cn(
            "mx-3 mb-3 rounded-sg-sm bg-sg-inset px-3 py-2 text-[12.5px] leading-relaxed",
            "whitespace-pre-wrap font-mono text-sg-ink-3",
            streaming && "relative",
          )}
        >
          {text || (
            <span className="italic text-sg-ink-4">
              {t("sessions.reasoning.empty")}
            </span>
          )}
          {streaming && (
            <span
              className="ml-0.5 inline-block h-3 w-1 animate-pulse bg-sg-accent/70 align-middle"
              aria-hidden
            />
          )}
        </div>
      )}
    </div>
  );
}
