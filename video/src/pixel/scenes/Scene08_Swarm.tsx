import React from "react";
import { AbsoluteFill, Easing, interpolate } from "remotion";
import { Stage } from "../Stage";
import { WorldSign } from "../WorldSign";
import { Mascot } from "../Mascot";
import { pal } from "../palette";
import { useAspect, pick } from "../../tokens/aspect";
import { useCopy } from "../i18n";

const snap = Easing.bezier(0.34, 1.56, 0.64, 1);
const easeOut = Easing.bezier(0.16, 1, 0.3, 1);

type Instance = {
  key: string;
  x: number;
  y: number;
  scale: number;
  phase: number; // frame offset for blink/breathe
  bornFrame: number; // when this instance first appeared
  squashFrame?: number; // when this instance squashed before splitting
};

// Stages of replication: 1 -> 2 -> 4 -> 8 -> 16, occurring at these frames.
const STAGES = [
  { atFrame: 0, count: 1 },
  { atFrame: 30, count: 2 },
  { atFrame: 60, count: 4 },
  { atFrame: 90, count: 8 },
  { atFrame: 120, count: 16 },
];

const SPLIT_DURATION = 18; // frames to animate snap-out
const SQUASH_LEAD = 8; // frames before split: parent squashes

// Compute positions for a given count and aspect.
// H: row counts are 1, 2, 4, 8, 4x4
// V: row counts are 1, 2, 4, 8, 2x8
const layoutFor = (count: number, aspect: "h" | "v"): Array<{ x: number; y: number; scale: number }> => {
  if (count === 1) return [{ x: 0, y: 0, scale: 0.55 }];
  if (count === 2) {
    return [-1, 1].map((d) => ({ x: d * 220, y: 0, scale: 0.45 }));
  }
  if (count === 4) {
    return [-3, -1, 1, 3].map((d) => ({ x: d * 220, y: 0, scale: 0.38 }));
  }
  if (count === 8) {
    return [-7, -5, -3, -1, 1, 3, 5, 7].map((d) => ({
      x: d * pick(aspect, 130, 80),
      y: 0,
      scale: pick(aspect, 0.3, 0.22),
    }));
  }
  // 16
  if (aspect === "h") {
    // 4x4 grid, fits in 1600x900 safe area
    const cols = 4;
    const rows = 4;
    const colStep = 360;
    const rowStep = 220;
    const out: Array<{ x: number; y: number; scale: number }> = [];
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const x = (c - (cols - 1) / 2) * colStep;
        const y = (r - (rows - 1) / 2) * rowStep;
        out.push({ x, y, scale: 0.34 });
      }
    }
    return out;
  } else {
    // V: 2 columns x 8 rows, fits in 900x1700
    const cols = 2;
    const rows = 8;
    const colStep = 380;
    const rowStep = 200;
    const out: Array<{ x: number; y: number; scale: number }> = [];
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const x = (c - (cols - 1) / 2) * colStep;
        const y = (r - (rows - 1) / 2) * rowStep;
        out.push({ x, y, scale: 0.3 });
      }
    }
    return out;
  }
};

// Find current and previous stage given a frame.
const currentStage = (frame: number) => {
  let stageIdx = 0;
  for (let i = 0; i < STAGES.length; i++) {
    if (frame >= STAGES[i].atFrame) stageIdx = i;
  }
  return stageIdx;
};

export const Scene08_Swarm: React.FC<{ localFrame: number; aspect: "h" | "v" }> = ({
  localFrame,
  aspect: _aspect,
}) => {
  const aspect = useAspect();
  const t = useCopy();

  const stageIdx = currentStage(localFrame);
  const stage = STAGES[stageIdx];
  const nextStage = STAGES[stageIdx + 1];

  // Build current instances by mapping target positions to layout slots.
  const currentLayout = layoutFor(stage.count, aspect);

  // If we are in the squash-window before a split, parents prepare a squash-stretch.
  // If we are in the split-window after a split, children snap out from their parent.
  let splitProgress = 0;
  let squashAmount = 0;
  let inSplit = false;
  if (nextStage) {
    const splitFrame = nextStage.atFrame;
    if (localFrame >= splitFrame - SQUASH_LEAD && localFrame < splitFrame) {
      // Parent squash-stretch lead-in
      const t = (localFrame - (splitFrame - SQUASH_LEAD)) / SQUASH_LEAD;
      squashAmount = Math.sin(t * Math.PI) * 0.3; // peaks at +0.3, returns to 0
    }
    if (localFrame >= splitFrame && localFrame < splitFrame + SPLIT_DURATION) {
      inSplit = true;
      splitProgress = (localFrame - splitFrame) / SPLIT_DURATION;
    }
  }

  // When in split window, render children from next-stage layout, animating
  // from their parent's current position toward their target.
  let instances: Instance[];
  let flashIntensity = 0;

  if (inSplit && nextStage) {
    const nextLayout = layoutFor(nextStage.count, aspect);
    const parentsPerChild = Math.floor(nextStage.count / stage.count);
    instances = nextLayout.map((targ, i) => {
      const parentIdx = Math.floor(i / parentsPerChild);
      const parent = currentLayout[parentIdx];
      const t = interpolate(splitProgress, [0, 1], [0, 1], {
        extrapolateLeft: "clamp",
        extrapolateRight: "clamp",
        easing: snap,
      });
      return {
        key: `s${stageIdx}-c${i}`,
        x: parent.x + (targ.x - parent.x) * t,
        y: parent.y + (targ.y - parent.y) * t,
        scale: parent.scale + (targ.scale - parent.scale) * t,
        phase: (i * 13) % 110,
        bornFrame: nextStage.atFrame,
      };
    });
    flashIntensity = Math.sin(splitProgress * Math.PI);
  } else {
    instances = currentLayout.map((p, i) => ({
      key: `s${stageIdx}-${i}`,
      x: p.x,
      y: p.y,
      scale: p.scale,
      phase: (i * 13) % 110,
      bornFrame: stage.atFrame,
    }));
  }

  // Final-state collective breathing pulse (frames 130-150)
  const collective = interpolate(localFrame, [130, 150], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: easeOut,
  });

  return (
    <Stage showGrid gridCell={pick(aspect, 28, 24)} gridOpacity={0.14} vignette={0.6}>
      {/* Split-event flash overlay — brief white-cyan wash on parent positions */}
      {flashIntensity > 0 && (
        <AbsoluteFill
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            pointerEvents: "none",
          }}
        >
          {currentLayout.map((p, i) => (
            <div
              key={`flash-${i}`}
              style={{
                position: "absolute",
                left: `calc(50% + ${p.x}px)`,
                top: `calc(50% + ${p.y}px)`,
                width: 240 * p.scale,
                height: 240 * p.scale,
                marginLeft: -120 * p.scale,
                marginTop: -120 * p.scale,
                borderRadius: "50%",
                background: `radial-gradient(circle, ${pal.cyanGlow} 0%, transparent 70%)`,
                opacity: flashIntensity * 0.85,
                filter: "blur(4px)",
              }}
            />
          ))}
        </AbsoluteFill>
      )}

      <AbsoluteFill
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        {instances.map((inst) => {
          // Per-instance squash on parents leading into the split
          const xScale = 1 + (squashAmount && !inSplit ? squashAmount : 0);
          const yScale = 1 - (squashAmount && !inSplit ? squashAmount * 0.5 : 0);

          // Collective sync pulse at end (very subtle)
          const syncPulse = 1 + Math.sin(localFrame / 18) * 0.04 * collective;

          // Born-fade for new instances
          const born = interpolate(
            localFrame - inst.bornFrame,
            [0, 14],
            [0, 1],
            {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
              easing: easeOut,
            },
          );

          return (
            <div
              key={inst.key}
              style={{
                position: "absolute",
                transform: `scale(${xScale * syncPulse}, ${yScale * syncPulse})`,
                transformOrigin: "center",
                opacity: born,
              }}
            >
              <Mascot
                scale={inst.scale}
                x={inst.x}
                y={inst.y}
                glow={0.45}
                blink
                breathe
                localFrame={localFrame + inst.phase}
              />
            </div>
          );
        })}
      </AbsoluteFill>

      <WorldSign
        title={t.s8_title}
        sub={t.s8_sub}
        variant="banner"
        y={pick(aspect, -360, -600)}
        scale={pick(aspect, 1.0, 0.85)}
        from={36}
      />
    </Stage>
  );
};
