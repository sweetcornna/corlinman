import * as React from "react";
import { cn } from "@/lib/utils";

/** Minimal styled checkbox. Native `input[type=checkbox]` + Tailwind.
 *
 * We intentionally avoid `@radix-ui/react-checkbox` — the approvals page
 * is the only consumer right now and the batch-select UX doesn't need
 * indeterminate state animations. ~15 lines is worth less than a new dep.
 */
export interface CheckboxProps
  extends Omit<React.InputHTMLAttributes<HTMLInputElement>, "type"> {
  "aria-label": string;
}

export const Checkbox = React.forwardRef<HTMLInputElement, CheckboxProps>(
  ({ className, ...props }, ref) => (
    <input
      ref={ref}
      type="checkbox"
      // Tidepool (Phase 5a): amber accent, glass border, amber focus ring.
      className={cn(
        "h-[15px] w-[15px] cursor-pointer rounded border border-sg-border bg-sg-inset",
        "accent-[color:var(--sg-accent)]",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/45 focus-visible:ring-offset-0",
        "disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...props}
    />
  ),
);
Checkbox.displayName = "Checkbox";
