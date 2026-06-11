import { useVideoConfig } from "remotion";

// Aspect-aware layout helper. Each scene uses this to switch between the
// 1920×1080 horizontal layout and the 1080×1920 vertical layout, picking the
// shape that lets the content breathe in its viewport.
export type Aspect = "h" | "v";

export const useAspect = (): Aspect => {
  const { width, height } = useVideoConfig();
  return height > width ? "v" : "h";
};

// helper: pick<T>(a, h, v) — returns h when aspect===h, v when v
export const pick = <T,>(aspect: Aspect, h: T, v: T): T => (aspect === "h" ? h : v);

// SVG viewBox per aspect — matches composition pixel dims so coords are 1:1
export const viewBox = (aspect: Aspect): string =>
  aspect === "h" ? "0 0 1920 1080" : "0 0 1080 1920";
