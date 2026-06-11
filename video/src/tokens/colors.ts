// Sapphire Impasto palette
// Locked to corlinman UI globals.css. DO NOT introduce new hues here —
// if a scene needs a tint, derive it via opacity / mix-blend from these.

export const colors = {
  // backgrounds
  bgVoid: "#050912",
  navy: "#060914",
  ivory: "#F5EDDA",
  ivory2: "#EFE5CF",

  // sapphire spectrum
  sapphire: "#1F4FB8",
  sapphireBright: "#5A88F7",
  sapphireLight: "#82A8E8",

  // ink
  ink: "#E5E9F2",
  ink2: "#8A96B3",
  ink3: "#4D5775",
  inkDay: "#0F2044",

  // utility
  glow: "rgba(130, 168, 232, 0.4)",
  glowStrong: "rgba(130, 168, 232, 0.7)",
} as const;

export type ColorKey = keyof typeof colors;
