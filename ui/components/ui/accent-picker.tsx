"use client";

import * as React from "react";
import { createPortal } from "react-dom";
import { Palette, RotateCcw } from "@/components/icons";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import {
  TINT_PRESETS,
  applyTint,
  getStoredTint,
  type TintChoice,
} from "@/lib/tint";

/**
 * Tint picker — "give the light a color". Six presets (moonlight default
 * + dawn/ice/rose/moss/iris) plus a custom hue wheel with L/C locked, so
 * personalization retunes the eclipse orb, thread, live dots and solid
 * primary buttons while the monochrome skeleton stays untouched.
 * (File keeps the accent-picker name/testids from the Spatial Glass era
 * to minimize churn in nav and tests.)
 */
export function AccentPicker({ className }: { className?: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const [tint, setTint] = React.useState<TintChoice>(null);
  const rootRef = React.useRef<HTMLDivElement | null>(null);
  const panelRef = React.useRef<HTMLDivElement | null>(null);
  // Viewport-anchored placement (portal + fixed): immune to ancestor
  // overflow/stacking contexts, clamped so the panel can never start
  // above the viewport or run past its bottom.
  const [pos, setPos] = React.useState<{ top: number; right: number; maxHeight: number } | null>(
    null,
  );

  React.useEffect(() => {
    setTint(getStoredTint());
  }, []);

  const place = React.useCallback(() => {
    const trigger = rootRef.current;
    if (!trigger) return;
    const r = trigger.getBoundingClientRect();
    const vh = window.innerHeight;
    const margin = 8;
    const top = Math.max(margin, Math.min(r.bottom + margin, vh - 160));
    setPos({
      top,
      right: Math.max(margin, window.innerWidth - r.right),
      maxHeight: vh - top - margin,
    });
  }, []);

  React.useEffect(() => {
    if (!open) return;
    place();
    const onDown = (e: PointerEvent) => {
      const target = e.target as Node;
      if (rootRef.current?.contains(target) || panelRef.current?.contains(target)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("pointerdown", onDown);
    window.addEventListener("keydown", onKey);
    window.addEventListener("resize", place);
    return () => {
      window.removeEventListener("pointerdown", onDown);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", place);
    };
  }, [open, place]);

  const pick = React.useCallback((next: TintChoice) => {
    setTint(next);
    applyTint(next);
  }, []);

  const customHue = tint && "hue" in tint ? tint.hue : null;
  const activePreset = tint && "preset" in tint ? tint.preset : null;

  return (
    <div ref={rootRef} className={cn("relative", className)}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-label={t("nav.tint.label")}
        aria-expanded={open}
        data-testid="accent-picker-trigger"
        className="inline-flex h-9 w-9 items-center justify-center rounded-sg-sm text-sg-ink-3 transition-colors hover:bg-sg-ink/5 hover:text-sg-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <Palette className="h-4 w-4" aria-hidden="true" />
      </button>

      {open && pos
        ? createPortal(
        <div
          ref={panelRef}
          role="dialog"
          aria-label={t("nav.tint.label")}
          data-testid="accent-picker-panel"
          className="sg-glass-overlay fixed z-[70] w-72 overflow-y-auto rounded-sg-lg p-4 animate-sg-palette-in"
          style={{ top: pos.top, right: pos.right, maxHeight: pos.maxHeight }}
        >
          {/* Presets — each swatch previews the "light" on pure black. */}
          <p className="text-[11px] font-medium uppercase tracking-wider text-sg-ink-4">
            {t("nav.tint.presetsTitle")}
          </p>
          <div className="mt-2.5 grid grid-cols-3 gap-2">
            {TINT_PRESETS.map((preset) => {
              const active = preset.preset
                ? activePreset === preset.preset
                : tint === null;
              return (
                <button
                  key={preset.id}
                  type="button"
                  onClick={() => pick(preset.preset ? { preset: preset.preset } : null)}
                  aria-pressed={active}
                  data-testid={`tint-card-${preset.id}`}
                  className={cn(
                    "group flex flex-col items-center gap-1.5 rounded-sg-md border p-2 transition-colors",
                    active
                      ? "nav-active"
                      : "border-sg-border bg-sg-inset hover:bg-sg-inset-hover",
                  )}
                >
                  {/* Light-on-black preview: tint dot glowing on space-0. */}
                  <span
                    aria-hidden="true"
                    className="flex h-6 w-full items-center justify-center overflow-hidden rounded-[7px] border border-sg-border bg-black"
                  >
                    <span
                      className="h-2.5 w-2.5 rounded-full"
                      style={{
                        background: preset.swatch,
                        boxShadow: `0 0 8px ${preset.swatch}`,
                      }}
                    />
                  </span>
                  <span className="text-[10.5px] leading-none text-sg-ink-3 group-hover:text-sg-ink">
                    {t(`nav.tint.presets.${preset.labelKey}`)}
                  </span>
                </button>
              );
            })}
          </div>

          {/* Custom — hue only; L/C stay locked to the moonlight envelope. */}
          <p className="mt-4 text-[11px] font-medium uppercase tracking-wider text-sg-ink-4">
            {t("nav.tint.customTitle")}
          </p>
          <div className="mt-2">
            <div className="flex items-center justify-between">
              <label htmlFor="tint-hue-slider" className="text-[11px] text-sg-ink-4">
                {t("nav.tint.hue")}
              </label>
              <span className="font-mono text-[11px] text-sg-ink-5">
                {customHue != null ? `${customHue}°` : "—"}
              </span>
            </div>
            <input
              id="tint-hue-slider"
              type="range"
              min={0}
              max={359}
              step={1}
              value={customHue ?? 230}
              onChange={(e) => pick({ hue: Number(e.target.value) })}
              data-testid="tint-hue-slider"
              className="mt-1.5 h-2 w-full cursor-pointer appearance-none rounded-full"
              style={{
                background:
                  "linear-gradient(90deg, oklch(0.85 0.09 0), oklch(0.85 0.09 60), oklch(0.85 0.09 120), oklch(0.85 0.09 180), oklch(0.85 0.09 240), oklch(0.85 0.09 300), oklch(0.85 0.09 359))",
              }}
            />
          </div>

          {/* Reset */}
          <button
            type="button"
            onClick={() => pick(null)}
            disabled={tint === null}
            data-testid="accent-reset"
            className="mt-4 inline-flex w-full items-center justify-center gap-1.5 rounded-sg-sm border border-sg-border bg-sg-inset px-2 py-1.5 text-xs text-sg-ink-3 transition-colors hover:bg-sg-inset-hover hover:text-sg-ink disabled:opacity-40"
          >
            <RotateCcw className="h-3 w-3" aria-hidden="true" />
            {t("nav.tint.reset")}
          </button>
        </div>,
        document.body,
      )
        : null}
    </div>
  );
}

export default AccentPicker;
