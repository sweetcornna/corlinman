"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

export interface SwitchProps {
  checked: boolean;
  onCheckedChange: (next: boolean) => void;
  disabled?: boolean;
  id?: string;
  "aria-label"?: string;
  "aria-labelledby"?: string;
  "data-testid"?: string;
  className?: string;
}

export function Switch({
  checked,
  onCheckedChange,
  disabled = false,
  className,
  ...rest
}: SwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => {
        if (!disabled) onCheckedChange(!checked);
      }}
      className={cn(
        "relative inline-flex h-8 w-14 shrink-0 items-center rounded-full border p-1",
        "backdrop-blur-glass backdrop-saturate-glass transition-colors duration-150",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/50 focus-visible:ring-offset-1 focus-visible:ring-offset-transparent",
        checked
          ? "border-tp-amber/50 bg-[color-mix(in_oklch,var(--tp-amber)_34%,transparent)]"
          : "border-tp-glass-edge bg-tp-glass-inner",
        disabled && "cursor-not-allowed opacity-60",
        className,
      )}
      {...rest}
    >
      <span
        aria-hidden
        className={cn(
          "pointer-events-none inline-block h-5 w-5 rounded-full border border-tp-glass-edge-strong bg-[color-mix(in_oklch,var(--tp-ink)_18%,transparent)] shadow-tp-panel backdrop-blur-glass",
          "transition-transform duration-150",
          checked ? "translate-x-7" : "translate-x-0",
        )}
      />
    </button>
  );
}

export default Switch;
