"use client";

import * as React from "react";
import { motion, type HTMLMotionProps } from "framer-motion";
import { cn } from "@/lib/utils";
import { useMotionVariants } from "@/lib/motion";

export interface EmptyStateProps extends HTMLMotionProps<"div"> {
  icon?: React.ReactNode;
  title: string;
  description?: React.ReactNode;
  action?: React.ReactNode;
}

/**
 * Centered placeholder block on the Spatial Glass card recipe, used when a
 * list / table / panel has no content. The icon sits in a circular sunken
 * chip with a matte inset chip. Rises in on
 * mount via the Liquid Glass spring (`liquidRise`); the motion variants are
 * reduced-motion aware.
 */
export const EmptyState = React.forwardRef<HTMLDivElement, EmptyStateProps>(
  function EmptyState(
    { icon, title, description, action, className, ...rest },
    ref,
  ) {
    const { liquidRise } = useMotionVariants();
    return (
      <motion.div
        ref={ref}
        initial="hidden"
        animate="visible"
        variants={liquidRise}
        role="status"
        className={cn(
          "mx-auto flex w-full max-w-md flex-col items-center justify-center gap-3 rounded-sg-lg border border-sg-border bg-sg-card bg-sg-card-grad px-6 py-10 text-center shadow-sg-edge",
          className,
        )}
        {...rest}
      >
        {icon ? (
          <div
            aria-hidden="true"
            className="flex h-14 w-14 items-center justify-center rounded-full bg-sg-inset text-sg-ink-4 shadow-sg-well-soft [&_svg]:h-7 [&_svg]:w-7"
          >
            {icon}
          </div>
        ) : null}
        <div className="text-sm font-semibold text-sg-ink">{title}</div>
        {description ? (
          <div className="text-xs text-sg-ink-3">{description}</div>
        ) : null}
        {action ? <div className=" mt-2">{action}</div> : null}
      </motion.div>
    );
  },
);

export default EmptyState;
