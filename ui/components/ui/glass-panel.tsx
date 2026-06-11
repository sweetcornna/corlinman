"use client";

import * as React from "react";
import { cn } from "@/lib/utils";
import { useSpecular } from "@/lib/use-specular";

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
   * Liquid Glass optics: light-aware gradient edge, chromatic refraction
   * rim, hover sheen sweep, and a pointer-tracked specular highlight.
   * Reserved for hero/interactive surfaces — keep dense lists plain.
   */
  lively?: boolean;
}

// Faux-glass card recipe per variant. The `bg-sg-card-grad` gradient is the
// visible fill (its stops carry the translucency); `strong` swaps the base
// color token under it for a denser read. Elevation carries the depth scale.
// NOTE: a base `bg-sg-card` color cannot share an element with the gradient —
// the class merger collapses two `bg-*` utilities — so the gradient stands in
// as the single background, which is intentional.
const variantClasses: Record<GlassPanelVariant, string> = {
  subtle: cn(
    "bg-sg-card-grad border-sg-border",
    "shadow-sg-1",
  ),
  soft: cn(
    "bg-sg-card-grad border-sg-border",
    "shadow-sg-2",
  ),
  strong: cn(
    "bg-sg-card-strong border-sg-border",
    "shadow-sg-3",
  ),
  primary: cn(
    "bg-sg-card-grad border-sg-border",
    "shadow-sg-primary",
  ),
};

export const GlassPanel = React.forwardRef<HTMLDivElement, GlassPanelProps>(
  function GlassPanel(
    {
      variant = "soft",
      rounded = "rounded-sg-lg",
      as: Tag = "div",
      lively = false,
      className,
      children,
      ...rest
    },
    ref,
  ) {
    // Pointer-tracked specular light for lively panels. The hook writes CSS
    // vars straight onto the node, so there is zero per-move React work.
    const specularRef = useSpecular<HTMLDivElement>();
    const setRefs = React.useCallback(
      (node: HTMLDivElement | null) => {
        specularRef.current = node;
        if (typeof ref === "function") ref(node);
        else if (ref) (ref as React.MutableRefObject<HTMLDivElement | null>).current = node;
      },
      [ref, specularRef],
    );
    // Each panel carries a top inset highlight via a pseudo-like layer — we use
    // a real child element so shadow layering doesn't interfere with the outer
    // shadow from the variant. This is a 1px highlight at the top edge that
    // makes the glass feel lit rather than painted.
    const mergedClassName = cn(
      "relative border",
      rounded,
      variantClasses[variant],
      lively && "lg-edge lg-refract lg-sheen lg-specular",
      className,
    );
    const commonProps = {
      ref: lively ? setRefs : (ref as React.Ref<HTMLDivElement>),
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
