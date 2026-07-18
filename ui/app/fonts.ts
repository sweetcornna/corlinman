import localFont from "next/font/local";

// Eclipse Minimal v2 type stack, self-hosted so Docker builds never need
// network font access. Latin subsets only — CJK falls back to system fonts
// (HarmonyOS Sans SC / PingFang SC / Noto Sans CJK SC) per the token stacks
// in globals.css.

// Body — MiSans Latin, four files mapped onto weight ranges so any weight
// resolves to the nearest real cut (ranges mirror the design project).
export const misans = localFont({
  src: [
    { path: "./fonts/MiSansLatin-Regular.woff2", weight: "100 419", style: "normal" },
    { path: "./fonts/MiSansLatin-Medium.woff2", weight: "420 479", style: "normal" },
    { path: "./fonts/MiSansLatin-Semibold.woff2", weight: "480 559", style: "normal" },
    { path: "./fonts/MiSansLatin-Bold.woff2", weight: "560 900", style: "normal" },
  ],
  variable: "--font-misans",
  display: "swap",
});

// Display — M PLUS 1 at 500 only (the moonshot hero grotesk). Weight
// discipline is 400/500 app-wide, so a single cut suffices; MiSans Medium
// is the visual fallback in the --st-font-display stack.
export const mplus1 = localFont({
  src: "./fonts/MPLUS1-Latin-Medium.woff2",
  weight: "500",
  variable: "--font-mplus",
  display: "swap",
});

// Mono — JetBrains Mono variable (100-800).
export const jetbrainsMono = localFont({
  src: "./fonts/JetBrainsMono-VF.woff2",
  weight: "100 800",
  variable: "--font-jetbrains-mono",
  display: "swap",
});
