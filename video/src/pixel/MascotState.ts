import { Easing, interpolate } from "remotion";

export type MascotProps = {
  scale: number;
  x: number;
  y: number;
  opacity: number;
  glow: number;
  trail: number;
  reflect: number;
  rotate: number;
};

// Continuous keyframe path for the persistent mascot.
// One mascot lives the whole 60s. Its scale, position, glow, trail all
// transition smoothly between zones — never disappears, never teleports.
//
// Frame numbers are GLOBAL frames (0..1800).
type Key = {
  frame: number;
  state: Partial<MascotProps>;
};

const KEYS_H: Key[] = [
  // Genesis (0..150): mascot not born yet (a spark exists instead)
  { frame: 0,    state: { opacity: 0, scale: 0.5, x: 0, y: 0, glow: 0, trail: 0, reflect: 0, rotate: 0 } },
  { frame: 240,  state: { opacity: 0, scale: 0.5, x: 0, y: 0, glow: 0, trail: 0, reflect: 0, rotate: 0 } },

  // Assembly completes around global frame 265 (Scene02 localFrame ~115)
  { frame: 265,  state: { opacity: 1, scale: 0.9, x: 0, y: 0, glow: 0.55, trail: 0, reflect: 0, rotate: 0 } },

  // Hero (300..450): center, big glow
  { frame: 320,  state: { opacity: 1, scale: 0.9, x: 0, y: 0, glow: 0.9, trail: 0, reflect: 0, rotate: 0 } },
  { frame: 430,  state: { opacity: 1, scale: 0.9, x: 0, y: 0, glow: 0.9, trail: 0, reflect: 0, rotate: 0 } },

  // Dash (450..600): trail rises, mascot stays center; the WORLD races past
  { frame: 480,  state: { opacity: 1, scale: 0.75, x: 0, y: 0, glow: 0.6, trail: 0.85, reflect: 0, rotate: 0 } },
  { frame: 560,  state: { opacity: 1, scale: 0.65, x: 0, y: 0, glow: 0.55, trail: 0.85, reflect: 0, rotate: 0 } },
  { frame: 600,  state: { opacity: 1, scale: 0.6,  x: 0, y: 0, glow: 0.5,  trail: 0.0,  reflect: 0, rotate: 0 } },

  // Orbit (600..750): center, smaller
  { frame: 700,  state: { opacity: 1, scale: 0.55, x: 0, y: 0, glow: 0.5, trail: 0, reflect: 0, rotate: 0 } },

  // Mesh (750..900): slight rise
  { frame: 820,  state: { opacity: 1, scale: 0.5,  x: 0, y: -20, glow: 0.5, trail: 0, reflect: 0, rotate: 0 } },

  // Circuit (900..1050): back to center
  { frame: 970,  state: { opacity: 1, scale: 0.55, x: 0, y: 0, glow: 0.55, trail: 0, reflect: 0, rotate: 0 } },

  // Swarm (1050..1200): persistent mascot fades partially to let the swarm visual breathe
  { frame: 1080, state: { opacity: 0.0, scale: 0.55, x: 0, y: 0, glow: 0.5, trail: 0, reflect: 0, rotate: 0 } },
  { frame: 1180, state: { opacity: 0.0, scale: 0.55, x: 0, y: 0, glow: 0.5, trail: 0, reflect: 0, rotate: 0 } },

  // Cards (1200..1350): slide to left
  { frame: 1240, state: { opacity: 1, scale: 0.4,  x: -650, y: 0, glow: 0.5, trail: 0, reflect: 0, rotate: 0 } },
  { frame: 1340, state: { opacity: 1, scale: 0.4,  x: -650, y: 0, glow: 0.5, trail: 0, reflect: 0, rotate: 0 } },

  // Reflect (1350..1500): slide back to center, hero pose + reflection
  { frame: 1410, state: { opacity: 1, scale: 0.85, x: 0, y: -80, glow: 0.8, trail: 0, reflect: 1, rotate: 0 } },
  { frame: 1490, state: { opacity: 1, scale: 0.85, x: 0, y: -80, glow: 0.8, trail: 0, reflect: 1, rotate: 0 } },

  // Frame (1500..1650): viewfinder lock
  { frame: 1560, state: { opacity: 1, scale: 0.7,  x: 0, y: 0, glow: 0.75, trail: 0, reflect: 0, rotate: 0 } },
  { frame: 1640, state: { opacity: 1, scale: 0.7,  x: 0, y: 0, glow: 0.75, trail: 0, reflect: 0, rotate: 0 } },

  // Wordmark (1650..1800): slide left
  { frame: 1700, state: { opacity: 1, scale: 0.55, x: -500, y: 0, glow: 0.85, trail: 0, reflect: 0, rotate: 0 } },
  { frame: 1800, state: { opacity: 1, scale: 0.55, x: -500, y: 0, glow: 0.85, trail: 0, reflect: 0, rotate: 0 } },
];

const KEYS_V: Key[] = [
  // Same timing as H but with V-friendly positions (mascot moves up/down instead of L/R)
  { frame: 0,    state: { opacity: 0, scale: 0.5, x: 0, y: 0, glow: 0, trail: 0, reflect: 0, rotate: 0 } },
  { frame: 240,  state: { opacity: 0, scale: 0.5, x: 0, y: 0, glow: 0, trail: 0, reflect: 0, rotate: 0 } },
  { frame: 265,  state: { opacity: 1, scale: 1.0, x: 0, y: 0, glow: 0.55, trail: 0, reflect: 0, rotate: 0 } },
  { frame: 320,  state: { opacity: 1, scale: 1.0, x: 0, y: 0, glow: 0.9, trail: 0, reflect: 0, rotate: 0 } },
  { frame: 430,  state: { opacity: 1, scale: 1.0, x: 0, y: 0, glow: 0.9, trail: 0, reflect: 0, rotate: 0 } },
  { frame: 480,  state: { opacity: 1, scale: 0.85, x: 0, y: 0, glow: 0.6, trail: 0.85, reflect: 0, rotate: 0 } },
  { frame: 560,  state: { opacity: 1, scale: 0.75, x: 0, y: 0, glow: 0.55, trail: 0.85, reflect: 0, rotate: 0 } },
  { frame: 600,  state: { opacity: 1, scale: 0.7, x: 0, y: 0, glow: 0.5, trail: 0, reflect: 0, rotate: 0 } },
  { frame: 700,  state: { opacity: 1, scale: 0.7, x: 0, y: 0, glow: 0.5, trail: 0, reflect: 0, rotate: 0 } },
  { frame: 820,  state: { opacity: 1, scale: 0.6, x: 0, y: -60, glow: 0.5, trail: 0, reflect: 0, rotate: 0 } },
  { frame: 970,  state: { opacity: 1, scale: 0.65, x: 0, y: 0, glow: 0.55, trail: 0, reflect: 0, rotate: 0 } },
  { frame: 1080, state: { opacity: 0.0, scale: 0.65, x: 0, y: 0, glow: 0.5, trail: 0, reflect: 0, rotate: 0 } },
  { frame: 1180, state: { opacity: 0.0, scale: 0.65, x: 0, y: 0, glow: 0.5, trail: 0, reflect: 0, rotate: 0 } },
  { frame: 1240, state: { opacity: 1, scale: 0.5, x: 0, y: -550, glow: 0.5, trail: 0, reflect: 0, rotate: 0 } },
  { frame: 1340, state: { opacity: 1, scale: 0.5, x: 0, y: -550, glow: 0.5, trail: 0, reflect: 0, rotate: 0 } },
  { frame: 1410, state: { opacity: 1, scale: 1.0, x: 0, y: -150, glow: 0.8, trail: 0, reflect: 1, rotate: 0 } },
  { frame: 1490, state: { opacity: 1, scale: 1.0, x: 0, y: -150, glow: 0.8, trail: 0, reflect: 1, rotate: 0 } },
  { frame: 1560, state: { opacity: 1, scale: 0.85, x: 0, y: 0, glow: 0.75, trail: 0, reflect: 0, rotate: 0 } },
  { frame: 1640, state: { opacity: 1, scale: 0.85, x: 0, y: 0, glow: 0.75, trail: 0, reflect: 0, rotate: 0 } },
  { frame: 1700, state: { opacity: 1, scale: 0.7, x: 0, y: -400, glow: 0.85, trail: 0, reflect: 0, rotate: 0 } },
  { frame: 1800, state: { opacity: 1, scale: 0.7, x: 0, y: -400, glow: 0.85, trail: 0, reflect: 0, rotate: 0 } },
];

const PROP_KEYS: Array<keyof MascotProps> = [
  "opacity", "scale", "x", "y", "glow", "trail", "reflect", "rotate",
];

const easeBoth = Easing.bezier(0.4, 0, 0.2, 1);

export function mascotStateAt(globalFrame: number, aspect: "h" | "v"): MascotProps {
  const keys = aspect === "h" ? KEYS_H : KEYS_V;
  const out: MascotProps = {
    opacity: 0, scale: 0.5, x: 0, y: 0, glow: 0, trail: 0, reflect: 0, rotate: 0,
  };
  for (const prop of PROP_KEYS) {
    // Build the sequence of (frame, value) for this prop, using only keys
    // that define it. Carry-forward last value where unset.
    let lastVal = 0;
    const xs: number[] = [];
    const ys: number[] = [];
    for (const k of keys) {
      const v = k.state[prop];
      if (v !== undefined) lastVal = v as number;
      xs.push(k.frame);
      ys.push(lastVal);
    }
    (out as any)[prop] = interpolate(globalFrame, xs, ys, {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
      easing: easeBoth,
    });
  }
  return out;
}
