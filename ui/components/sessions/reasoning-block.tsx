/**
 * Collapsible "Thinking" block. Streams render with a soft amber shimmer
 * while the underlying block isn't yet `done`; once finished the shimmer
 * stops and the block can be collapsed.
 *
 * Tidepool palette: amber glass card, faded text body.
 */
"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, ChevronRight, Sparkles } from "lucide-react";

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
      className={cn(
        "rounded-xl border border-amber-200/40 bg-amber-50/40 backdrop-blur-sm",
        "dark:border-amber-300/10 dark:bg-amber-950/20",
        "shadow-sm",
      )}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={cn(
          "flex w-full items-center gap-2 px-3 py-2 text-left text-xs font-medium",
          "text-amber-900 dark:text-amber-200",
          "hover:bg-amber-100/40 dark:hover:bg-amber-900/20",
          "transition-colors rounded-xl",
        )}
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown className="size-3.5 shrink-0 opacity-70" aria-hidden />
        ) : (
          <ChevronRight className="size-3.5 shrink-0 opacity-70" aria-hidden />
        )}
        <Sparkles
          className={cn(
            "size-3.5 shrink-0",
            streaming && "animate-pulse text-amber-500 dark:text-amber-300",
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
            "px-4 pb-3 pt-1 text-xs leading-relaxed",
            "text-amber-950/80 dark:text-amber-100/70",
            "whitespace-pre-wrap font-mono",
            streaming && "relative",
          )}
        >
          {text || (
            <span className="italic opacity-60">{t("sessions.reasoning.empty")}</span>
          )}
          {streaming && (
            <span
              className="ml-0.5 inline-block h-3 w-1 animate-pulse bg-amber-500/70 align-middle"
              aria-hidden
            />
          )}
        </div>
      )}
    </div>
  );
}
