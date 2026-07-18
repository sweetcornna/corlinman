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
        "transition-colors duration-150",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/50 focus-visible:ring-offset-1 focus-visible:ring-offset-transparent",
        checked
          ? "border-transparent bg-sg-tint"
          : "border-sg-border bg-sg-inset shadow-sg-well-soft",
        disabled && "cursor-not-allowed opacity-60",
        className,
      )}
      {...rest}
    >
      <span
        aria-hidden
        className={cn(
          "pointer-events-none inline-block h-5 w-5 rounded-full shadow-sg-1",
          "transition-transform duration-150",
          checked ? "translate-x-7 bg-sg-tint-ink" : "translate-x-0 bg-sg-ink",
        )}
      />
    </button>
  );
}

export default Switch;
