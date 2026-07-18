"use client";

import * as React from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Info,
  OctagonAlert,
} from "@/components/icons";

import { cn } from "@/lib/utils";

type AlertVariant = "info" | "success" | "warning" | "danger";

const VARIANTS: Record<
  AlertVariant,
  { box: string; icon: string; Icon: React.ComponentType<{ className?: string }> }
> = {
  // Eclipse: matte charcoal band + a 2px status rule on the left edge —
  // the muted status color marks the semantic, the surface stays neutral.
  info: {
    box: "border-sg-border bg-sg-card border-l-2 border-l-sg-ink-4",
    icon: "text-sg-ink-3",
    Icon: Info,
  },
  success: {
    box: "border-sg-border bg-sg-card border-l-2 border-l-sg-ok",
    icon: "text-sg-ok",
    Icon: CheckCircle2,
  },
  warning: {
    box: "border-sg-border bg-sg-card border-l-2 border-l-sg-warn",
    icon: "text-sg-warn",
    Icon: AlertTriangle,
  },
  danger: {
    box: "border-sg-border bg-sg-card border-l-2 border-l-sg-err",
    icon: "text-sg-err",
    Icon: OctagonAlert,
  },
};

export interface AlertProps
  extends Omit<React.HTMLAttributes<HTMLDivElement>, "title"> {
  variant?: AlertVariant;
  title?: React.ReactNode;
  /** Override the variant icon; pass null to hide it. */
  icon?: React.ReactNode | null;
}

/**
 * Spatial Glass status band — the single primitive for inline notices.
 * Replaces the hand-rolled `dark:bg-red-950/30`-style boxes scattered
 * across pages. Children render as the description body.
 */
export function Alert({
  variant = "info",
  title,
  icon,
  className,
  children,
  ...rest
}: AlertProps) {
  const v = VARIANTS[variant];
  const iconNode =
    icon === null ? null : icon ?? <v.Icon className={cn("h-4 w-4", v.icon)} aria-hidden="true" />;

  return (
    <div
      role={variant === "danger" || variant === "warning" ? "alert" : "status"}
      data-variant={variant}
      className={cn(
        "flex items-start gap-2.5 rounded-sg-md border px-3.5 py-2.5 text-[13px] leading-relaxed",
        v.box,
        className,
      )}
      {...rest}
    >
      {iconNode ? <span className="mt-0.5 shrink-0">{iconNode}</span> : null}
      <div className="min-w-0 flex-1">
        {title ? <div className="font-semibold text-sg-ink">{title}</div> : null}
        {children ? <div className={cn("text-sg-ink-2", title && "mt-0.5")}>{children}</div> : null}
      </div>
    </div>
  );
}
