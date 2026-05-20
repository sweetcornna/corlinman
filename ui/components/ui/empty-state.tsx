"use client";

import * as React from "react";
import { motion, type HTMLMotionProps, type Variants } from "framer-motion";
import { cn } from "@/lib/utils";

export interface EmptyStateProps extends HTMLMotionProps<"div"> {
  icon?: React.ReactNode;
  title: string;
  description?: React.ReactNode;
  action?: React.ReactNode;
}

const fadeUp: Variants = {
  hidden: { opacity: 0, y: 8 },
  visible: {
    opacity: 1,
    y: 0,
    transition: { duration: 0.28, ease: [0.23, 1, 0.32, 1] },
  },
};

/**
 * Centered placeholder block with dashed border, used when a list / table /
 * panel has no content. Fades up on mount via framer-motion; the underlying
 * framer runtime is itself reduced-motion aware.
 */
export const EmptyState = React.forwardRef<HTMLDivElement, EmptyStateProps>(
  function EmptyState(
    { icon, title, description, action, className, ...rest },
    ref,
  ) {
    return (
      <motion.div
        ref={ref}
        initial="hidden"
        animate="visible"
        variants={fadeUp}
        role="status"
        className={cn(
          "mx-auto flex w-full max-w-md flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-tp-glass-edge bg-tp-glass px-6 py-10 text-center shadow-tp-panel",
          className,
        )}
        {...rest}
      >
        {icon ? (
          <div
            aria-hidden="true"
            className="flex h-10 w-10 items-center justify-center text-muted-foreground [&_svg]:h-10 [&_svg]:w-10"
          >
            {icon}
          </div>
        ) : null}
        <div className="text-sm font-semibold text-foreground">{title}</div>
        {description ? (
          <div className="text-xs text-muted-foreground">{description}</div>
        ) : null}
        {action ? <div className="mt-2">{action}</div> : null}
      </motion.div>
    );
  },
);

export default EmptyState;
