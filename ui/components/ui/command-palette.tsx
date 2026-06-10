"use client";

import * as React from "react";
import { Command } from "cmdk";
import { cn } from "@/lib/utils";

/**
 * Configurable ⌘K command palette for the Tidepool shell.
 *
 * Uses `cmdk` for fuzzy-search + keyboard nav built-ins. Consumers supply
 * groups of items and an `onRun` callback. The component does **not**
 * own state — pass `open` / `onOpenChange` from a parent provider. This
 * lets Phase 2 mount a single provider at the admin layout root and wire
 * the ⌘K global keyboard handler there.
 *
 * Structure (matches the F prototype):
 *   ┌────────────────────────────────────────────────┐
 *   │ 🔍 typed-query            …placeholder    [esc]│
 *   ├────────────────────────────────────────────────┤
 *   │ ─── group label ─────                          │
 *   │ 🟠 item label            meta           [kbd]  │
 *   │ …                                              │
 *   └────────────────────────────────────────────────┘
 *   │ [↑↓] navigate  [↵] select  [esc] close         │
 *
 * The existing `components/cmdk-palette.tsx` is the current production
 * palette with hard-coded actions; this component is **the Tidepool
 * replacement** and takes items as props so Phase 5+ pages can inject
 * page-specific commands.
 */

export interface PaletteItem {
  id: string;
  label: React.ReactNode;
  icon?: React.ReactNode;
  /** Keyboard shortcut shown on the right (e.g. "↵", "G A", "⌘↵"). */
  shortcut?: string;
  /** Optional secondary meta text (e.g. "2 pending", "5m ago"). */
  meta?: React.ReactNode;
  /** Optional attention badge — typically for items with urgency. */
  badge?: string;
  /** Keywords that should match fuzzy search beyond the label text. */
  keywords?: string[];
  /** Disabled items render muted and aren't selectable. */
  disabled?: boolean;
  /** Called when the item fires. Palette auto-closes unless you call
   *  `event.preventDefault()`. */
  onRun?: (event: { preventDefault: () => void }) => void;
}

export interface PaletteGroup {
  id: string;
  label: string;
  items: PaletteItem[];
}

export interface CommandPaletteProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  groups: PaletteGroup[];
  /** Placeholder text in the input. */
  placeholder?: string;
  /** Optional hint label shown bottom-right of the footer. */
  brandLabel?: React.ReactNode;
}

export function CommandPalette({
  open,
  onOpenChange,
  groups,
  placeholder = "search actions, jump to pages, invoke commands",
  brandLabel,
}: CommandPaletteProps) {
  const [query, setQuery] = React.useState("");

  // Close on Escape — cmdk already does this via Command.Dialog, but we
  // implement the container ourselves so we replicate the keybind.
  React.useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onOpenChange(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onOpenChange]);

  // Reset query when the palette closes so the next open starts clean.
  React.useEffect(() => {
    if (!open) setQuery("");
  }, [open]);

  if (!open) return null;

  return (
    <div
      data-testid="palette-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget) onOpenChange(false);
      }}
      className={cn(
        "fixed inset-0 z-50 flex items-start justify-center pt-[14vh]",
        "bg-black/50",
        "backdrop-blur-sm backdrop-saturate-[1.2]",
        "animate-in fade-in duration-150",
      )}
    >
      <Command
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        shouldFilter
        className={cn(
          "w-[640px] max-w-[calc(100vw-3rem)] overflow-hidden rounded-sg-xl border",
          "bg-sg-overlay border-sg-border-strong",
          "backdrop-blur-sg-overlay backdrop-saturate-sg-overlay",
          "shadow-sg-4 ring-1 ring-sg-accent/20",
          // Liquid Glass optics — light-aware edge ring + chromatic inner
          // lensing on the palette card (blur-free, composes with the blur
          // recipe above).
          "lg-edge lg-refract",
          "animate-sg-palette-in",
        )}
      >
        {/* Input */}
        <div className="flex items-center gap-3 border-b border-sg-border bg-sg-inset p-4 text-[16px]">
          <SearchIcon className="h-[18px] w-[18px] shrink-0 text-sg-accent" />
          <Command.Input
            value={query}
            onValueChange={setQuery}
            placeholder={placeholder}
            className={cn(
              "flex-1 bg-transparent font-sans tracking-[-0.01em] text-sg-ink",
              "placeholder:text-sg-ink-4 focus:outline-none",
            )}
            aria-label="Search"
          />
          <span className="rounded-sg-sm border border-sg-border bg-sg-inset-strong px-2 py-[2px] font-mono text-[11px] text-sg-ink-3">
            esc
          </span>
        </div>

        {/* Groups */}
        <Command.List className="max-h-[380px] overflow-y-auto px-1.5 py-1.5">
          <Command.Empty className="px-4 py-6 text-center text-[13px] text-sg-ink-3">
            No results — try another query.
          </Command.Empty>
          {groups.map((group) => (
            <Command.Group
              key={group.id}
              heading={group.label}
              className={cn(
                "py-1.5",
                "[&_[cmdk-group-heading]]:px-3.5 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:pt-1.5",
                "[&_[cmdk-group-heading]]:font-mono [&_[cmdk-group-heading]]:text-[10px]",
                "[&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-[0.12em]",
                "[&_[cmdk-group-heading]]:text-sg-ink-4",
              )}
            >
              {group.items.map((item) => (
                <PaletteRow
                  key={item.id}
                  item={item}
                  onSelect={() => {
                    const ev = { _prevented: false, preventDefault() { this._prevented = true; } };
                    item.onRun?.(ev);
                    if (!ev._prevented) onOpenChange(false);
                  }}
                />
              ))}
            </Command.Group>
          ))}
        </Command.List>

        {/* Footer */}
        <div className="flex items-center gap-[18px] border-t border-sg-border bg-sg-inset px-4 py-2.5 text-[11px] text-sg-ink-4">
          <FooterHint kbd="↑↓" label="navigate" />
          <FooterHint kbd="↵" label="select" />
          <FooterHint kbd="⌘↵" label="execute" />
          <FooterHint kbd="esc" label="close" />
          <span className="ml-auto font-mono text-[10px]">
            {brandLabel ?? (
              <>
                corlinman · <em className="not-italic font-medium text-sg-accent">⌘K</em>
              </>
            )}
          </span>
        </div>
      </Command>
    </div>
  );
}

function PaletteRow({
  item,
  onSelect,
}: {
  item: PaletteItem;
  onSelect: () => void;
}) {
  const valueStr = item.keywords
    ? `${flatLabel(item.label)} ${item.keywords.join(" ")}`
    : flatLabel(item.label);
  return (
    <Command.Item
      value={valueStr}
      disabled={item.disabled}
      onSelect={onSelect}
      className={cn(
        "group mx-2 grid grid-cols-[20px_1fr_auto_auto] items-center gap-3 rounded-sg-md px-3.5 py-2.5",
        "text-[13.5px] text-sg-ink-2 cursor-pointer",
        // Springy press physics on select.
        "lg-gel",
        "aria-selected:bg-sg-accent-soft aria-selected:text-sg-ink",
        "aria-selected:shadow-[inset_2px_0_0_0_var(--sg-accent)]",
        "data-[disabled=true]:cursor-not-allowed data-[disabled=true]:opacity-50",
      )}
    >
      <span className="flex h-4 w-4 items-center justify-center text-sg-ink-3 group-aria-selected:text-sg-accent">
        {item.icon}
      </span>
      <span>{item.label}</span>
      {item.badge ? (
        <span className="rounded-full border border-sg-accent/30 bg-sg-accent-soft px-1.5 py-0 font-mono text-[10px] text-sg-accent">
          {item.badge}
        </span>
      ) : item.meta ? (
        <span className="font-mono text-[10.5px] text-sg-ink-4">
          {item.meta}
        </span>
      ) : (
        <span />
      )}
      <span
        className={cn(
          "rounded-sg-sm border px-1.5 py-px font-mono text-[10.5px] tracking-[0.05em]",
          "bg-sg-inset border-sg-border text-sg-ink-3",
          "group-aria-selected:border-sg-accent/35 group-aria-selected:text-sg-accent",
          "group-aria-selected:[background:color-mix(in_oklch,var(--sg-accent)_20%,var(--sg-inset-bg))]",
        )}
      >
        {item.shortcut ?? "↵"}
      </span>
    </Command.Item>
  );
}

function FooterHint({ kbd, label }: { kbd: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="rounded-sg-sm border border-sg-border bg-sg-inset-strong px-1.5 py-px font-mono text-[10px] text-sg-ink-3">
        {kbd}
      </span>
      {label}
    </span>
  );
}

function flatLabel(node: React.ReactNode): string {
  if (node == null || node === false) return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(flatLabel).join(" ");
  if (React.isValidElement<{ children?: React.ReactNode }>(node)) {
    return flatLabel(node.props.children);
  }
  return "";
}

function SearchIcon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      aria-hidden="true"
      {...props}
    >
      <circle cx="11" cy="11" r="7" />
      <path d="m21 21-4.3-4.3" />
    </svg>
  );
}

export default CommandPalette;
