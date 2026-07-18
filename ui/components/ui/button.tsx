"use client";

import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

// Eclipse pill buttons. The solid primary is one of the few whitelisted
// glow surfaces: tint fill + tint-ink text + static soft glow. Everything
// else is matte: ghost pills carry the 42% ghost border, quiet buttons a
// bare ink hover, destructive a muted err fill with no glow.
const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-full text-sm font-medium transition-[color,background-color,border-color,box-shadow,opacity] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-transparent disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default:
          "bg-sg-tint text-sg-tint-ink shadow-sg-glow hover:bg-sg-tint/90",
        destructive:
          "bg-sg-err text-white hover:bg-sg-err/90",
        outline:
          "border border-sg-border-ghost bg-transparent text-sg-ink-2 hover:bg-sg-ink/5 hover:text-sg-ink",
        secondary:
          "border border-sg-border bg-sg-card-strong text-sg-ink-2 shadow-sg-edge hover:bg-sg-inset-hover hover:text-sg-ink",
        ghost: "text-sg-ink-3 hover:bg-sg-ink/5 hover:text-sg-ink",
        link: "text-sg-tint underline-offset-4 hover:underline",
      },
      size: {
        default: "h-10 px-5 py-2",
        sm: "h-9 px-4 text-xs",
        lg: "h-[50px] px-8 text-base",
        icon: "h-10 w-10 rounded-full",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        {...props}
      />
    );
  },
);
Button.displayName = "Button";

export { Button, buttonVariants };
