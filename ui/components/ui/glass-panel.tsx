"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Spatial Glass surface primitive — the core depth API of the design system.
 *
 * All variants share the faux-glass card recipe (gradient fill + hairline
 * border, NO backdrop-filter so scrolling lists never re-blur); the elevation
 * scale carries the depth read:
 *   - `subtle` — card recipe at the lowest elevation (`shadow-sg-1`).
 *   - `soft` (default) — card recipe at panel elevation (`shadow-sg-2`).
 *     Used for lists, plain panels.
 *   - `strong` — stronger card fill, hero elevation (`shadow-sg-3`).
 *     Hero-class surfaces (dashboard hero, palette modal card).
 *   - `primary` — soft fill plus the accent ring/glow that marks a stat chip
 *     as the "most active" metric (`shadow-sg-primary`).
 *
 * All variants:
 *   - fill: `bg-sg-card` (+ `bg-sg-card-grad`) / `bg-sg-card-strong`
 *   - border: hairline `border-sg-border`
 *   - inset highlight: `bg-sg-highlight` on the top edge
 *
 * Day/night automatic via token substitution — no prop needed.
 */

export type GlassPanelVariant = "soft" | "strong" | "subtle" | "primary";

export type GlassPanelTag = "div" | "section" | "aside" | "article" | "main" | "header" | "footer";

type DivProps = React.HTMLAttributes<HTMLDivElement>;

export interface GlassPanelProps extends DivProps {
  variant?: GlassPanelVariant;
  /** Override rounded corner (default `rounded-sg-lg` = 20px). */
  rounded?: string;
  /** Render as a different HTML tag. Constrained to block-level semantic tags. */
  as?: GlassPanelTag;
  /**
   * @deprecated Liquid Glass optics were removed with the Eclipse
   * redesign — matte surfaces have no specular layer. Accepted and
   * ignored so legacy call sites keep compiling until the sweep.
   */
  lively?: boolean;
}

// Faux-glass card recipe per variant. The `bg-sg-card-grad` gradient is the
// visible fill (its stops carry the translucency); `strong` swaps the base
// color token under it for a denser read. Elevation carries the depth scale.
// NOTE: a base `bg-sg-card` color cannot share an element with the gradient —
// the class merger collapses two `bg-*` utilities — so the gradient stands in
// as the single background, which is intentional.
// Eclipse light grammar: resting surfaces carry only the moon edge — drop
// shadows are reserved for floating layers (dialogs/drawers). The
// bg-sg-card-grad token carries BOTH the matte fill and the sheen in one
// background-image stack (a separate bg-color class alongside it would be
// collapsed away by tailwind-merge).
const variantClasses: Record<GlassPanelVariant, string> = {
  subtle: cn(
    "bg-sg-card-grad border-sg-border",
    "shadow-sg-edge",
  ),
  soft: cn(
    "bg-sg-card-grad border-sg-border",
    "shadow-sg-edge",
  ),
  strong: cn(
    "bg-sg-card-strong border-sg-border",
    "shadow-sg-edge-strong",
  ),
  // "Most active" surface — the selected treatment: moon edge + a faint
  // inset tint glow (whitelisted; single source = --sg-shadow-selected).
  primary: cn(
    "bg-sg-card-grad border-sg-border-strong",
    "shadow-sg-selected",
  ),
};

export const GlassPanel = React.forwardRef<HTMLDivElement, GlassPanelProps>(
  function GlassPanel(
    {
      variant = "soft",
      rounded = "rounded-sg-lg",
      as: Tag = "div",
      lively: _lively,
      className,
      children,
      ...rest
    },
    ref,
  ) {
    void _lively;
    // Each panel carries a top inset highlight via a real child element so
    // shadow layering doesn't interfere with the outer shadow from the
    // variant: a 1px moon-edge line at the top that makes the matte surface
    // feel lit rather than painted.
    const mergedClassName = cn(
      "relative border",
      rounded,
      variantClasses[variant],
      className,
    );
    const commonProps = {
      ref: ref as React.Ref<HTMLDivElement>,
      className: mergedClassName,
      "data-glass-variant": variant,
      ...(rest as DivProps),
    };
    const highlight = (
      <span
        aria-hidden="true"
        className={cn(
          "pointer-events-none absolute inset-x-0 top-0 h-px",
          rounded,
          "bg-sg-highlight opacity-80",
        )}
      />
    );
    // Specialised per-tag render to avoid JSX's complex polymorphic union type.
    switch (Tag) {
      case "section":
        return <section {...commonProps}>{highlight}{children}</section>;
      case "aside":
        return <aside {...commonProps}>{highlight}{children}</aside>;
      case "article":
        return <article {...commonProps}>{highlight}{children}</article>;
      case "main":
        return <main {...commonProps}>{highlight}{children}</main>;
      case "header":
        return <header {...commonProps}>{highlight}{children}</header>;
      case "footer":
        return <footer {...commonProps}>{highlight}{children}</footer>;
      default:
        return <div {...commonProps}>{highlight}{children}</div>;
    }
  },
);

export default GlassPanel;
