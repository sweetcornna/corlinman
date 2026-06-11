"use client";

import * as React from "react";
import { createPortal } from "react-dom";
import { Palette, RotateCcw } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import {
  DESIGNER_THEMES,
  applyTheme,
  getStoredThemeParams,
  type ThemeParams,
} from "@/lib/theme-studio";

/**
 * Theme Studio — designer theme presets + full custom (accent hue and
 * canvas hue), applied live through lib/theme-studio.ts. The canvas
 * chroma stays inside the tuned envelope, so every combination keeps
 * the premium Spatial Glass read.
 */
export function AccentPicker({ className }: { className?: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const [params, setParams] = React.useState<ThemeParams | null>(null);
  const rootRef = React.useRef<HTMLDivElement | null>(null);
  const panelRef = React.useRef<HTMLDivElement | null>(null);
  // Viewport-anchored placement (portal + fixed): immune to ancestor
  // overflow/stacking contexts, clamped so the panel can never start
  // above the viewport or run past its bottom.
  const [pos, setPos] = React.useState<{ top: number; right: number; maxHeight: number } | null>(
    null,
  );

  React.useEffect(() => {
    setParams(getStoredThemeParams());
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

  const pick = React.useCallback((next: ThemeParams | null) => {
    setParams(next);
    applyTheme(next);
  }, []);

  const setCustom = React.useCallback(
    (patch: Partial<ThemeParams>) => {
      const base: ThemeParams = params
        ? { ...params }
        : { accent: 230, canvas: 266, canvasChroma: 0.03 };
      delete base.preset; // any manual tweak leaves the preset
      const next = { ...base, ...patch };
      setParams(next);
      applyTheme(next);
    },
    [params],
  );

  return (
    <div ref={rootRef} className={cn("relative", className)}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-label={t("nav.themeStudio.label")}
        aria-expanded={open}
        data-testid="accent-picker-trigger"
        className="lg-gel inline-flex h-9 w-9 items-center justify-center rounded-sg-sm text-sg-ink-3 transition-colors hover:bg-sg-accent-soft hover:text-sg-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <Palette className="h-4 w-4" aria-hidden="true" />
      </button>

      {open && pos
        ? createPortal(
        <div
          ref={panelRef}
          role="dialog"
          aria-label={t("nav.themeStudio.label")}
          data-testid="accent-picker-panel"
          className="sg-glass-overlay lg-edge fixed z-[70] w-72 overflow-y-auto rounded-sg-lg p-4 shadow-sg-4 animate-sg-palette-in"
          style={{ top: pos.top, right: pos.right, maxHeight: pos.maxHeight }}
        >
          {/* Designer themes */}
          <p className="text-[11px] font-semibold uppercase tracking-wider text-sg-ink-4">
            {t("nav.themeStudio.themesTitle")}
          </p>
          <div className="mt-2.5 grid grid-cols-3 gap-2">
            {DESIGNER_THEMES.map((theme) => {
              const active = theme.params
                ? params?.preset === theme.params.preset
                : params === null;
              return (
                <button
                  key={theme.id}
                  type="button"
                  onClick={() => pick(theme.params ? { ...theme.params } : null)}
                  aria-pressed={active}
                  data-testid={`theme-card-${theme.id}`}
                  className={cn(
                    "lg-gel group flex flex-col items-center gap-1.5 rounded-sg-md border p-2 transition-colors",
                    active
                      ? "border-sg-accent/50 bg-sg-accent-soft shadow-sg-glow"
                      : "border-sg-border bg-sg-inset hover:bg-sg-inset-hover",
                  )}
                >
                  {/* Tri-chip preview: canvas / accent / companion */}
                  <span
                    aria-hidden="true"
                    className="flex h-6 w-full items-stretch overflow-hidden rounded-[7px] border border-sg-border"
                  >
                    <span className="flex-[2]" style={{ background: theme.preview[0] }} />
                    <span className="flex-1" style={{ background: theme.preview[1] }} />
                    <span className="flex-1" style={{ background: theme.preview[2] }} />
                  </span>
                  <span className="text-[10.5px] leading-none text-sg-ink-3 group-hover:text-sg-ink">
                    {t(`nav.themeStudio.themes.${theme.labelKey}`)}
                  </span>
                </button>
              );
            })}
          </div>

          {/* Custom — accent + canvas hue sliders */}
          <p className="mt-4 text-[11px] font-semibold uppercase tracking-wider text-sg-ink-4">
            {t("nav.themeStudio.customTitle")}
          </p>
          <div className="mt-2">
            <div className="flex items-center justify-between">
              <label htmlFor="accent-hue-slider" className="text-[11px] text-sg-ink-4">
                {t("nav.themeStudio.accentHue")}
              </label>
              <span className="font-mono text-[11px] text-sg-ink-5">
                {params ? `${Math.round(params.accent)}°` : "—"}
              </span>
            </div>
            <input
              id="accent-hue-slider"
              type="range"
              min={0}
              max={359}
              step={1}
              value={params?.accent ?? 230}
              onChange={(e) => setCustom({ accent: Number(e.target.value) })}
              data-testid="accent-hue-slider"
              className="mt-1.5 h-2 w-full cursor-pointer appearance-none rounded-full"
              style={{
                background:
                  "linear-gradient(90deg, oklch(0.78 0.13 0), oklch(0.78 0.13 60), oklch(0.78 0.13 120), oklch(0.78 0.13 180), oklch(0.78 0.13 240), oklch(0.78 0.13 300), oklch(0.78 0.13 359))",
              }}
            />
          </div>
          <div className="mt-3">
            <div className="flex items-center justify-between">
              <label htmlFor="canvas-hue-slider" className="text-[11px] text-sg-ink-4">
                {t("nav.themeStudio.canvasHue")}
              </label>
              <span className="font-mono text-[11px] text-sg-ink-5">
                {params?.canvas != null ? `${Math.round(params.canvas)}°` : "—"}
              </span>
            </div>
            <input
              id="canvas-hue-slider"
              type="range"
              min={0}
              max={359}
              step={1}
              value={params?.canvas ?? 266}
              onChange={(e) =>
                setCustom({ canvas: Number(e.target.value), canvasChroma: params?.canvasChroma ?? 0.03 })
              }
              data-testid="canvas-hue-slider"
              className="mt-1.5 h-2 w-full cursor-pointer appearance-none rounded-full"
              style={{
                background:
                  "linear-gradient(90deg, oklch(0.22 0.045 0), oklch(0.22 0.045 60), oklch(0.22 0.045 120), oklch(0.22 0.045 180), oklch(0.22 0.045 240), oklch(0.22 0.045 300), oklch(0.22 0.045 359))",
              }}
            />
          </div>

          <div className="mt-3">
            <div className="flex items-center justify-between">
              <label htmlFor="glass-opacity-slider" className="text-[11px] text-sg-ink-4">
                {t("nav.themeStudio.glassOpacity")}
              </label>
              <span className="font-mono text-[11px] text-sg-ink-5">
                {Math.round((params?.glassOpacity ?? 1) * 100)}%
              </span>
            </div>
            <input
              id="glass-opacity-slider"
              type="range"
              min={55}
              max={140}
              step={5}
              value={Math.round((params?.glassOpacity ?? 1) * 100)}
              onChange={(e) => setCustom({ glassOpacity: Number(e.target.value) / 100 })}
              data-testid="glass-opacity-slider"
              className="mt-1.5 h-2 w-full cursor-pointer appearance-none rounded-full"
              style={{
                background:
                  "linear-gradient(90deg, oklch(0.6 0.02 270 / 0.25), oklch(0.6 0.02 270 / 0.95))",
              }}
            />
          </div>

          {/* Reset */}
          <button
            type="button"
            onClick={() => pick(null)}
            disabled={params === null}
            data-testid="accent-reset"
            className="mt-4 inline-flex w-full items-center justify-center gap-1.5 rounded-sg-sm border border-sg-border bg-sg-inset px-2 py-1.5 text-xs text-sg-ink-3 transition-colors hover:bg-sg-inset-hover hover:text-sg-ink disabled:opacity-40"
          >
            <RotateCcw className="h-3 w-3" aria-hidden="true" />
            {t("nav.themeStudio.reset")}
          </button>
        </div>,
        document.body,
      )
        : null}
    </div>
  );
}

export default AccentPicker;
