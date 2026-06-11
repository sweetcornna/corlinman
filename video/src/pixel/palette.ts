// Pixel-film palette — Sapphire spectrum + cyan accents.
// Extracted from the corlinman mascot (variant C) and color sheet.

export const pal = {
  // backgrounds (deep navy gradient)
  void: "#050912",
  navy: "#0A1530",
  navyDeep: "#060914",
  grid: "rgba(90, 136, 247, 0.06)",
  gridStrong: "rgba(90, 136, 247, 0.18)",

  // sapphire mascot body
  shadow: "#0F3FA8",
  deep: "#1F4FB8",
  mid: "#3070E0",
  bright: "#5A88F7",
  light: "#82A8E8",
  highlight: "#A8D4FF",

  // cyan accents (eyes / mouth / pulse)
  cyan: "#0FE8F0",
  cyanGlow: "rgba(15, 232, 240, 0.55)",
  ivory: "#F5EDDA",

  // utility
  white: "#FFFFFF",
  scrim: "rgba(5, 9, 18, 0.6)",
} as const;

export type PalKey = keyof typeof pal;

// Semantic gradient stops (used by glow / radial fills)
export const gradient = {
  hero: [pal.highlight, pal.bright, pal.deep, pal.shadow] as const,
  bloom: [pal.cyan, pal.bright, "transparent"] as const,
};
