"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";

import { Info } from "@/components/icons";
import { cn } from "@/lib/utils";

/**
 * Tooltip — Eclipse Minimal v2 hover/focus bubble for long-form contracts
 * (regexes, endpoint details, format grammars) that must NOT sit in the
 * visible copy. Companion to `FieldHint`: the visible line stays one plain
 * sentence, the contract lives here.
 *
 * Deliberately dependency-free (no radix/floating-ui): an opaque, matte
 * bubble absolutely positioned against an inline wrapper. No portal — the
 * bubble stays inside the trigger's stacking context, which is fine for
 * the short single-paragraph payloads this is meant for.
 *
 * Design-language notes:
 *  - opaque surface (`bg-sg-opaque`), zero backdrop-filter;
 *  - font weight stays ≤500 (plain text only);
 *  - shows on hover AND keyboard focus, hides on Escape;
 *  - `role="tooltip"` + `aria-describedby` wiring for screen readers.
 */

export interface TooltipProps {
  /** Long-form detail shown inside the bubble. Plain text / inline nodes. */
  content: React.ReactNode;
  /** The trigger the bubble anchors to. */
  children: React.ReactNode;
  /** Which side of the trigger the bubble opens on. */
  side?: "top" | "bottom";
  className?: string;
  /** Extra classes for the bubble itself (e.g. width overrides). */
  contentClassName?: string;
  "data-testid"?: string;
}

export function Tooltip({
  content,
  children,
  side = "top",
  className,
  contentClassName,
  "data-testid": testId,
}: TooltipProps) {
  const [visible, setVisible] = React.useState(false);
  // useId is SSR/hydration-safe and unique per mounted instance.
  const id = `sg-tooltip-${React.useId()}`;

  const hide = React.useCallback(() => setVisible(false), []);
  const show = React.useCallback(() => setVisible(true), []);

  // Document-level listener so Escape also dismisses the hover-opened
  // bubble (focus may be nowhere near the wrapper on the hover path).
  React.useEffect(() => {
    if (!visible) return;
    const onDocKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") hide();
    };
    document.addEventListener("keydown", onDocKeyDown);
    return () => document.removeEventListener("keydown", onDocKeyDown);
  }, [visible, hide]);

  // aria-describedby must sit on the focusable trigger itself, not the
  // inline wrapper — screen readers only announce the description of
  // the focused element. Clone-inject when the child is a lone element;
  // arbitrary child trees keep the (weaker) wrapper-level fallback.
  const child = React.isValidElement(children)
    ? React.cloneElement(children as React.ReactElement<Record<string, unknown>>, {
        "aria-describedby": visible ? id : undefined,
      })
    : children;

  return (
    <span
      className={cn("relative inline-flex", className)}
      onMouseEnter={show}
      onMouseLeave={hide}
      onFocus={show}
      onBlur={hide}
      aria-describedby={
        React.isValidElement(children) ? undefined : visible ? id : undefined
      }
      data-testid={testId}
    >
      {child}
      {visible ? (
        <span
          role="tooltip"
          id={id}
          className={cn(
            "pointer-events-none absolute left-1/2 z-[80] w-max max-w-[18rem] -translate-x-1/2",
            side === "top" ? "bottom-full mb-1.5" : "top-full mt-1.5",
            "rounded-sg-md border border-sg-border-strong bg-sg-opaque px-2.5 py-1.5",
            "text-left text-[11px] font-normal leading-relaxed text-sg-ink-2 shadow-sg-2",
            "whitespace-normal break-words",
            contentClassName,
          )}
        >
          {content}
        </span>
      ) : null}
    </span>
  );
}

export interface InfoTipProps {
  /** Long-form detail shown in the bubble. */
  content: React.ReactNode;
  /** Accessible name for the trigger glyph. */
  label?: string;
  side?: "top" | "bottom";
  className?: string;
  contentClassName?: string;
  "data-testid"?: string;
}

/**
 * InfoTip — the canonical "ⓘ next to a hint" trigger: a focusable,
 * sprite-drawn info glyph carrying a `Tooltip`. Drop it beside a
 * `FieldHint` when the folded-away contract deserves richer text than the
 * native `title=` attribute (wrapping, selection-free hover on touchpads).
 */
export function InfoTip({
  content,
  label,
  side = "top",
  className,
  contentClassName,
  "data-testid": testId,
}: InfoTipProps) {
  const { t } = useTranslation();
  return (
    <Tooltip
      content={content}
      side={side}
      className={className}
      contentClassName={contentClassName}
      data-testid={testId}
    >
      <button
        type="button"
        aria-label={label ?? t("common.details")}
        className={cn(
          "inline-flex h-4 w-4 cursor-help items-center justify-center rounded-full",
          "align-middle text-sg-ink-4 transition-colors hover:text-sg-ink-2",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        )}
      >
        <Info className="h-3.5 w-3.5" aria-hidden />
      </button>
    </Tooltip>
  );
}

export default Tooltip;
