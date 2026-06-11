// 12-keyframe pixel film timeline.
// Each scene is 150 frames @ 30fps = 5s. Total 1800f = 60s.
// Matches user's reference storyboard (image #6).

export const SCENE_DURATION = 150;
// Crossfade tuned so adjacent world signs don't overlap visually.
// Persistent mascot handles narrative continuity across the cut on its own.
export const CROSSFADE = 24;

export const KEYFRAMES = [
  { id: "01-spark", title: "GENESIS", metric: "00:00" },
  { id: "02-assemble", title: "ASSEMBLE", metric: "ONE BINARY" },
  { id: "03-hero", title: "AGENT", metric: "v0.1" },
  { id: "04-dash", title: "STREAM", metric: "SSE" },
  { id: "05-orbit", title: "PROVIDERS", metric: "6 / 6" },
  { id: "06-mesh", title: "PLUGINS", metric: "JSON-RPC" },
  { id: "07-circuit", title: "TOOLS", metric: "HOT-SWAP" },
  { id: "08-swarm", title: "SWARM", metric: "47 SKILLS" },
  { id: "09-cards", title: "APPROVE", metric: "HITL" },
  { id: "10-reflect", title: "TIDEPOOL", metric: "DAY / NIGHT" },
  { id: "11-frame", title: "OBSERVE", metric: "LOCK-ON" },
  { id: "12-wordmark", title: "CORLINMAN", metric: "self-host the agent" },
] as const;

export type KeyframeId = (typeof KEYFRAMES)[number]["id"];

// Helper to get a scene's local frame from the global frame.
// Returns -1 if outside the scene's window.
export const sceneLocal = (globalFrame: number, idx: number): number => {
  const start = idx * SCENE_DURATION;
  const end = start + SCENE_DURATION;
  if (globalFrame < start - CROSSFADE || globalFrame >= end + CROSSFADE) return -1;
  return globalFrame - start;
};

// Crossfade opacity for a scene given the local frame (-CROSSFADE .. DURATION+CROSSFADE)
export const sceneOpacity = (localFrame: number, isFirst = false, isLast = false): number => {
  // fade in
  if (localFrame < 0) {
    if (isFirst) return 0; // first scene snaps in (will be wrapped by film-level fade)
    return Math.max(0, (localFrame + CROSSFADE) / CROSSFADE);
  }
  // fade out
  if (localFrame >= SCENE_DURATION) {
    if (isLast) return 0;
    return Math.max(0, 1 - (localFrame - SCENE_DURATION) / CROSSFADE);
  }
  return 1;
};
