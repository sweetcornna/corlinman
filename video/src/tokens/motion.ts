import { Easing } from "remotion";

// Cinematic motion presets. Use these instead of inventing new curves per scene.

export const easings = {
  // Hero moves (large gestures, scene entries / exits)
  hero: Easing.bezier(0.65, 0, 0.35, 1),

  // Quiet entries (text, glow appearances)
  quiet: Easing.bezier(0.16, 1, 0.3, 1),

  // System-like (data packets, ticks)
  system: Easing.bezier(0.4, 0, 0.2, 1),

  // Breathing (long oscillation, very gentle in/out)
  breath: Easing.inOut(Easing.cubic),
} as const;

// Common spring config for spring()
export const springs = {
  gentle: { damping: 200, stiffness: 60, mass: 1 } as const,
  firm: { damping: 200, stiffness: 120, mass: 0.7 } as const,
} as const;
