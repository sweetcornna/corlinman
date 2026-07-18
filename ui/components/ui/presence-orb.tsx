"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Eclipse pearl — the design language's signature element. A black disc
 * eclipsing a tint-colored corona (conic gradient), with a three-tier
 * bloom. The corona and bloom follow the tint pipeline, so every preset
 * recolors the pearl; the disc stays pure black.
 *
 * Sizes: sm 22px (inline status), md 34px (avatars/rows), hero 72px
 * (login/onboard/empty states). `active` spins the corona (eclipse-turn,
 * 7s); reduced-motion freezes it via the globals.css media block.
 */
export interface PresenceOrbProps extends React.HTMLAttributes<HTMLSpanElement> {
  size?: "sm" | "md" | "hero";
  /** Spinning corona + full bloom. Idle orbs dim to bloom-1. */
  active?: boolean;
}

export function PresenceOrb({
  size = "sm",
  active = false,
  className,
  ...rest
}: PresenceOrbProps) {
  return (
    <span
      aria-hidden
      data-active={active || undefined}
      className={cn(
        "presence-orb",
        !active && "idle",
        size === "md" && "lg",
        size === "hero" && "hero",
        className,
      )}
      {...rest}
    />
  );
}

export default PresenceOrb;
