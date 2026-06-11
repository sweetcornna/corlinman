// Single source of truth for all scene timings.
// All values in frames at FPS (default 30). Total = 1800 frames = 60s.

export const FPS = 30;
export const TOTAL_FRAMES = 60 * FPS; // 1800

// Scene durations in seconds
const sec = {
  genesis: 6,
  awakening: 10,
  orchestration: 14,
  evolution: 14,
  swarm: 10,
  logoLock: 6,
} as const;

// Convert to frames + cumulative start offsets
const f = (s: number) => Math.round(s * FPS);

export const scenes = {
  genesis: {
    name: "Genesis",
    from: 0,
    durationInFrames: f(sec.genesis),
    copy: { en: "// signal detected", cn: "在沉默中" },
  },
  awakening: {
    name: "Awakening",
    from: f(sec.genesis),
    durationInFrames: f(sec.awakening),
    copy: { en: "// first instance materializes", cn: "一个智能体醒来" },
  },
  orchestration: {
    name: "Orchestration",
    from: f(sec.genesis + sec.awakening),
    durationInFrames: f(sec.orchestration),
    copy: { en: "hermes // orchestration tick", cn: "它们协同工作" },
  },
  evolution: {
    name: "Evolution",
    from: f(sec.genesis + sec.awakening + sec.orchestration),
    durationInFrames: f(sec.evolution),
    copy: { en: "darwin // selection round", cn: "它们自我进化" },
  },
  swarm: {
    name: "Swarm",
    from: f(sec.genesis + sec.awakening + sec.orchestration + sec.evolution),
    durationInFrames: f(sec.swarm),
    copy: { en: "swarm // 124 agents · 47 capabilities", cn: "一个会自己生长的系统" },
  },
  logoLock: {
    name: "Logo Lock",
    from: f(sec.genesis + sec.awakening + sec.orchestration + sec.evolution + sec.swarm),
    durationInFrames: f(sec.logoLock),
    copy: { en: "agents that build agents", cn: "" },
  },
} as const;

export type SceneKey = keyof typeof scenes;
