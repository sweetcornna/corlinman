import * as React from "react";
import { cn } from "@/lib/utils";

const Input = React.forwardRef<
  HTMLInputElement,
  React.InputHTMLAttributes<HTMLInputElement>
>(({ className, type, ...props }, ref) => (
  <input
    type={type}
    ref={ref}
    className={cn(
      "sg-inset flex h-10 w-full rounded-sg-sm border-sg-border bg-sg-inset px-3 py-1 text-sm text-sg-ink transition-colors file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground placeholder:text-sg-ink-4 focus-visible:border-sg-accent/40 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50",
      className,
    )}
    {...props}
  />
));
Input.displayName = "Input";

export { Input };
