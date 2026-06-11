import { loadFont as loadInstrumentSerif } from "@remotion/google-fonts/InstrumentSerif";
import { loadFont as loadGeistMono } from "@remotion/google-fonts/GeistMono";
import { loadFont as loadNotoSansSC } from "@remotion/google-fonts/NotoSansSC";

const instrument = loadInstrumentSerif();
const mono = loadGeistMono();
const cjk = loadNotoSansSC();

// mono stack falls back to the CJK font so Chinese chars render correctly.
const monoStack = `${mono.fontFamily}, ${cjk.fontFamily}, sans-serif`;
const serifStack = `${instrument.fontFamily}, ${cjk.fontFamily}, serif`;

export const fonts = {
  serif: serifStack,
  mono: monoStack,
  cjk: cjk.fontFamily,
} as const;

// Common type ramps
export const type = {
  // hero wordmark / scene titles
  display: {
    fontFamily: fonts.serif,
    fontWeight: 400,
    letterSpacing: "-0.015em",
    lineHeight: 1,
  },
  displayItalic: {
    fontFamily: fonts.serif,
    fontStyle: "italic" as const,
    fontWeight: 400,
    letterSpacing: "-0.005em",
    lineHeight: 1,
  },
  // timestamps, scene labels, code snippets
  mono: {
    fontFamily: fonts.mono,
    fontWeight: 400,
    letterSpacing: "0.04em",
  },
  monoSmall: {
    fontFamily: fonts.mono,
    fontWeight: 400,
    fontSize: 14,
    letterSpacing: "0.14em",
    textTransform: "uppercase" as const,
  },
  // CJK display
  cjk: {
    fontFamily: fonts.cjk,
    fontWeight: 400,
    letterSpacing: "0.02em",
  },
} as const;
