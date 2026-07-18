"use client";

import * as React from "react";
import { useSpring, useTransform, useMotionValueEvent } from "framer-motion";
import { cn } from "@/lib/utils";
import { uiLocale } from "@/lib/format";

type NumberFormat = "number" | "currency" | "percent";

export interface AnimatedNumberProps
  extends Omit<React.HTMLAttributes<HTMLSpanElement>, "children"> {
  value: number;
  format?: NumberFormat;
  /** Extra Intl.NumberFormat options merged over the format preset. */
  formatOptions?: Intl.NumberFormatOptions;
  locale?: string;
  /**
   * Informational settle hint in ms. Not used directly — spring stiffness /
   * damping drive timing. Kept for API parity and future use.
   */
  duration?: number;
}

function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = React.useState(false);
  React.useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mql = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(mql.matches);
    update();
    if (mql.addEventListener) {
      mql.addEventListener("change", update);
      return () => mql.removeEventListener("change", update);
    }
    mql.addListener(update);
    return () => mql.removeListener(update);
  }, []);
  return reduced;
}

function presetFor(format: NumberFormat): Intl.NumberFormatOptions {
  switch (format) {
    case "currency":
      return { style: "currency", currency: "USD", maximumFractionDigits: 2 };
    case "percent":
      return { style: "percent", maximumFractionDigits: 1 };
    case "number":
    default:
      return { maximumFractionDigits: 0 };
  }
}

/**
 * Spring-animated numeric counter. Under `prefers-reduced-motion` it renders
 * the target value directly, no tweening. Always announces via aria-live.
 */
export const AnimatedNumber = React.forwardRef<
  HTMLSpanElement,
  AnimatedNumberProps
>(function AnimatedNumber(
  {
    value,
    format = "number",
    formatOptions,
    locale = uiLocale(),
    duration,
    className,
    ...rest
  },
  ref,
) {
  void duration; // reserved for future tuning
  const reduced = usePrefersReducedMotion();
  const spring = useSpring(value, { stiffness: 80, damping: 20 });
  const display = useTransform(spring, (latest) => latest);

  const formatter = React.useMemo(() => {
    const opts = { ...presetFor(format), ...(formatOptions ?? {}) };
    try {
      return new Intl.NumberFormat(locale, opts);
    } catch {
      return new Intl.NumberFormat(locale);
    }
  }, [format, formatOptions, locale]);

  const [text, setText] = React.useState(() => formatter.format(value));

  React.useEffect(() => {
    if (reduced) {
      spring.jump(value);
      setText(formatter.format(value));
    } else {
      spring.set(value);
    }
  }, [value, reduced, spring, formatter]);

  useMotionValueEvent(display, "change", (latest) => {
    if (reduced) return;
    setText(formatter.format(latest));
  });

  return (
    <span
      ref={ref}
      className={cn("tabular-nums", className)}
      aria-live="polite"
      aria-atomic="true"
      {...rest}
    >
      {text}
    </span>
  );
});

export default AnimatedNumber;
