"use client";

import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

// Liquid Glass buttons: full-pill `rounded-full`, semibold weight, generous
// horizontal padding. Solid accent fill on the default CTA with a soft accent
// glow on hover. `.` gives the press a springy overshoot release
// (non-linear gel physics) and the primary CTA carries a specular sheen
// sweep on hover.
const buttonVariants = cva(
  " inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-full text-sm font-semibold transition-[color,background-color,border-color,box-shadow,opacity] hover:-translate-y-px focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default:
          " bg-primary text-primary-foreground shadow-sg-1 hover:bg-primary/90 hover:shadow-sg-glow",
        destructive:
          "bg-sg-err text-primary-foreground shadow-sg-1 hover:bg-sg-err/90 hover:shadow-sg-2 hover:ring-2 hover:ring-sg-err/40",
        outline:
          "border border-sg-border-strong bg-sg-inset text-foreground hover:bg-sg-inset-hover hover:text-foreground",
        secondary:
          "bg-secondary text-secondary-foreground hover:bg-secondary/80",
        ghost: "hover:bg-sg-accent-soft hover:text-foreground",
        link: "text-primary underline-offset-4 hover:underline",
      },
      size: {
        default: "h-10 px-5 py-2",
        sm: "h-9 px-4 text-xs",
        lg: "h-12 px-8 text-base",
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
