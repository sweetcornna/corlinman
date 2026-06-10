"use client";

import * as React from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { AnimatePresence, motion } from "framer-motion";
import { X } from "lucide-react";

import { cn } from "@/lib/utils";
import { springs } from "@/lib/motion";
import { useMotion } from "@/components/ui/motion-safe";

/**
 * Shared right-side (or left-side) sliding drawer primitive.
 *
 * Built on `@radix-ui/react-dialog` for focus-trap, Esc-to-close, scroll-lock
 * and `role=dialog` / `aria-modal` semantics. Enter/exit animation is driven
 * by `framer-motion`'s `AnimatePresence` with the radix Content/Overlay kept
 * mounted via `forceMount`, so exit animations actually play. Under
 * `prefers-reduced-motion`, the transition collapses to 0ms (snap).
 *
 * Used by:
 *   - `components/skills/skill-drawer.tsx`
 *   - `components/characters/character-drawer.tsx`
 *
 * NOT to be used by components that need a persistent side column (those are
 * layout siblings, not modal dialogs).
 */

export type DrawerWidth = "sm" | "md" | "lg" | "xl" | string;

export interface DrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Anchor edge. Defaults to `"right"`. */
  side?: "right" | "left";
  /** Preset (`sm`=360, `md`=440, `lg`=560, `xl`=720) or an explicit CSS value. */
  width?: DrawerWidth;
  title: string;
  description?: string;
  children: React.ReactNode;
  /** Sticky footer slot (actions). When provided, pinned to the bottom edge. */
  footer?: React.ReactNode;
  /** Classes merged onto the sliding panel. */
  className?: string;
  /** Lock scroll of the underlying page. Default `true`. */
  lockScroll?: boolean;
  /** Dismiss on outside click / Escape. Default `true`. */
  dismissable?: boolean;
}

const WIDTH_PRESETS: Record<"sm" | "md" | "lg" | "xl", string> = {
  sm: "max-w-[360px]",
  md: "max-w-[440px]",
  lg: "max-w-[560px]",
  xl: "max-w-[720px]",
};

function resolveWidth(width: DrawerWidth | undefined): {
  className: string;
  style?: React.CSSProperties;
} {
  const key = width ?? "md";
  if (key === "sm" || key === "md" || key === "lg" || key === "xl") {
    return { className: WIDTH_PRESETS[key] };
  }
  // Arbitrary string → inline style so consumers can pass `"640px"`, `"48rem"`,
  // etc. without fighting tailwind's JIT.
  return { className: "", style: { maxWidth: key } };
}

export function Drawer({
  open,
  onOpenChange,
  side = "right",
  width = "md",
  title,
  description,
  children,
  footer,
  className,
  lockScroll = true,
  dismissable = true,
}: DrawerProps): React.JSX.Element {
  const { reduced } = useMotion();

  const widthResolved = resolveWidth(width);

  const slideFrom = side === "right" ? "100%" : "-100%";
  // Animation params — reduced-motion collapses both translation and timing.
  const initial = reduced ? { x: 0 } : { x: slideFrom };
  const animate = { x: 0 };
  const exit = reduced ? { x: 0 } : { x: slideFrom };
  // Liquid Glass surface spring — weighty but alive, settles with a hint of
  // overshoot as the panel lands.
  const transition = reduced ? { duration: 0 } : springs.surface;

  // When `dismissable=false`, intercept radix's close-request events.
  const blockClose = (e: Event) => {
    if (!dismissable) e.preventDefault();
  };

  return (
    <DialogPrimitive.Root
      open={open}
      onOpenChange={onOpenChange}
      // `modal={lockScroll}` — radix's scroll-lock rides on the `modal` flag.
      modal={lockScroll}
    >
      <AnimatePresence>
        {open ? (
          <DialogPrimitive.Portal forceMount>
            <DialogPrimitive.Overlay asChild forceMount>
              <motion.div
                className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={reduced ? { duration: 0 } : { duration: 0.18 }}
              />
            </DialogPrimitive.Overlay>
            <DialogPrimitive.Content
              asChild
              forceMount
              onEscapeKeyDown={blockClose}
              onPointerDownOutside={blockClose}
              onInteractOutside={blockClose}
              // If no description is provided we must silence radix's
              // DescriptionWarning by passing `aria-describedby={undefined}`.
              {...(description ? {} : { "aria-describedby": undefined })}
            >
              <motion.div
                initial={initial}
                animate={animate}
                exit={exit}
                transition={transition}
                style={widthResolved.style}
                className={cn(
                  "fixed inset-y-0 z-50 flex h-full w-full flex-col",
                  "bg-sg-overlay shadow-sg-4 backdrop-blur-sg-overlay backdrop-saturate-sg-overlay focus:outline-none",
                  // Liquid Glass optics — light-aware edge ring + chromatic
                  // inner lensing so the sliding panel reads as bent light.
                  // Blur-free, composes with the blur recipe above.
                  "lg-edge lg-refract",
                  side === "right"
                    ? "right-0 rounded-l-sg-xl border-l border-sg-border-strong"
                    : "left-0 rounded-r-sg-xl border-r border-sg-border-strong",
                  widthResolved.className,
                  className,
                )}
                data-side={side}
              >
                <DrawerHeader title={title} description={description} />
                <div className="flex-1 overflow-y-auto">{children}</div>
                {footer ? (
                  <div className="sticky bottom-0 flex items-center justify-end gap-2 border-t border-sg-border bg-sg-overlay px-5 py-3">
                    {footer}
                  </div>
                ) : null}
                <DialogPrimitive.Close
                  className="absolute right-3 top-3 inline-flex h-8 w-8 items-center justify-center rounded-sg-sm text-sg-ink-3 transition-colors hover:bg-sg-inset-hover hover:text-sg-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  aria-label="Close"
                >
                  <X className="h-4 w-4" />
                </DialogPrimitive.Close>
              </motion.div>
            </DialogPrimitive.Content>
          </DialogPrimitive.Portal>
        ) : null}
      </AnimatePresence>
    </DialogPrimitive.Root>
  );
}

/**
 * Internal header shim so callers don't need to know about radix's
 * `Title` / `Description` components. Visually hidden if callers build their
 * own header chrome inside `children` — the radix primitives must render
 * *somewhere* in the tree so `aria-labelledby` / `aria-describedby` wire up,
 * even if the consumer is painting a richer visual header lower down.
 *
 * Today every callsite wants a simple title bar. If richer chrome is needed,
 * callers can pass `title=""` and render their own — the radix Title will
 * still be present (with empty text) to satisfy ARIA wiring.
 */
function DrawerHeader({
  title,
  description,
}: {
  title: string;
  description?: string;
}) {
  return (
    <header className="border-b border-sg-border px-5 pb-4 pt-5 pr-14">
      <DialogPrimitive.Title className="text-base font-semibold tracking-tight text-sg-ink">
        {title}
      </DialogPrimitive.Title>
      {description ? (
        <DialogPrimitive.Description className="mt-1 text-xs text-sg-ink-3">
          {description}
        </DialogPrimitive.Description>
      ) : null}
    </header>
  );
}

export default Drawer;
