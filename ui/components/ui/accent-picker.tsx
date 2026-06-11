"use client";

import * as React from "react";
import { Palette, RotateCcw } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import {
  ACCENT_PRESETS,
  applyAccent,
  getStoredAccent,
} from "@/lib/accent";

/**
 * Theme accent picker — preset swatches + a free hue slider, applied
 * live through lib/accent.ts (style-block override, both themes).
 * Lives in the topnav next to the theme toggle.
 */
export function AccentPicker({ className }: { className?: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const [hue, setHue] = React.useState<number | null>(null);
  const rootRef = React.useRef<HTMLDivElement | null>(null);

  React.useEffect(() => {
    setHue(getStoredAccent());
  }, []);

  React.useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("pointerdown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("pointerdown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const pick = React.useCallback((next: number | null) => {
    setHue(next);
    applyAccent(next);
  }, []);

  return (
    <div ref={rootRef} className={cn("relative", className)}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-label={t("nav.accent.label")}
        aria-expanded={open}
        data-testid="accent-picker-trigger"
        className="lg-gel inline-flex h-9 w-9 items-center justify-center rounded-sg-sm text-sg-ink-3 transition-colors hover:bg-sg-accent-soft hover:text-sg-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <Palette className="h-4 w-4" aria-hidden="true" />
      </button>

      {open ? (
        <div
          role="dialog"
          aria-label={t("nav.accent.label")}
          data-testid="accent-picker-panel"
          className="sg-glass-overlay lg-edge absolute right-0 top-11 z-50 w-64 rounded-sg-lg p-4 shadow-sg-4 animate-sg-palette-in"
        >
          <p className="text-[11px] font-semibold uppercase tracking-wider text-sg-ink-4">
            {t("nav.accent.title")}
          </p>

          {/* Preset swatches */}
          <div className="mt-3 flex items-center gap-2">
            {ACCENT_PRESETS.map((p) => {
              const active =
                p.hue === null ? hue === null : hue !== null && Math.abs(hue - p.hue) < 3;
              return (
                <button
                  key={p.id}
                  type="button"
                  onClick={() => pick(p.hue)}
                  aria-label={t(`nav.accent.presets.${p.labelKey}`)}
                  aria-pressed={active}
                  data-testid={`accent-swatch-${p.id}`}
                  className={cn(
                    "lg-gel h-7 w-7 rounded-full border transition-shadow",
                    active
                      ? "border-sg-ink shadow-[0_0_0_2px_var(--sg-accent-glow)]"
                      : "border-sg-border hover:shadow-sg-glow",
                  )}
                  style={{ background: p.swatch }}
                />
              );
            })}
          </div>

          {/* Free hue slider */}
          <div className="mt-4">
            <div className="flex items-center justify-between">
              <label
                htmlFor="accent-hue-slider"
                className="text-[11px] text-sg-ink-4"
              >
                {t("nav.accent.custom")}
              </label>
              <span className="font-mono text-[11px] text-sg-ink-5">
                {hue !== null ? `${Math.round(hue)}°` : "—"}
              </span>
            </div>
            <input
              id="accent-hue-slider"
              type="range"
              min={0}
              max={359}
              step={1}
              value={hue ?? 230}
              onChange={(e) => pick(Number(e.target.value))}
              data-testid="accent-hue-slider"
              className="mt-2 h-2 w-full cursor-pointer appearance-none rounded-full"
              style={{
                background:
                  "linear-gradient(90deg, oklch(0.78 0.13 0), oklch(0.78 0.13 60), oklch(0.78 0.13 120), oklch(0.78 0.13 180), oklch(0.78 0.13 240), oklch(0.78 0.13 300), oklch(0.78 0.13 359))",
              }}
            />
          </div>

          {/* Reset */}
          <button
            type="button"
            onClick={() => pick(null)}
            disabled={hue === null}
            data-testid="accent-reset"
            className="mt-4 inline-flex w-full items-center justify-center gap-1.5 rounded-sg-sm border border-sg-border bg-sg-inset px-2 py-1.5 text-xs text-sg-ink-3 transition-colors hover:bg-sg-inset-hover hover:text-sg-ink disabled:opacity-40"
          >
            <RotateCcw className="h-3 w-3" aria-hidden="true" />
            {t("nav.accent.reset")}
          </button>
        </div>
      ) : null}
    </div>
  );
}

export default AccentPicker;
