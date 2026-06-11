import React from "react";
import { AbsoluteFill, Easing, interpolate } from "remotion";
import { Stage } from "../Stage";
import { WorldSign } from "../WorldSign";
import { pal } from "../palette";
import { useAspect, pick } from "../../tokens/aspect";
import { useCopy } from "../i18n";

const easeOut = Easing.bezier(0.16, 1, 0.3, 1);

// 5×5 cluster pattern that emerges in stages: 1 → 2×2 → 3×3-ish (5 total bright pixels).
// Coordinates are offsets in pixel-cells from the center pixel. Order matters: first
// entry is the seed; subsequent entries reveal in sequence to imply growth.
const CLUSTER_OFFSETS: Array<[number, number]> = [
  [0, 0],   // seed pixel
  [1, 0],   // 2×2 partner
  [0, 1],
  [1, 1],
  [-1, 0],  // expand toward 3×3 hint
];

// Frame thresholds at which each clustered pixel ignites.
const CLUSTER_THRESHOLDS = [0, 100, 110, 120, 132];

type Ring = { startFrame: number; maxRadius: number };

const SHOCKWAVES: Ring[] = [
  { startFrame: 60, maxRadius: 520 },
];

export const Scene01_Spark: React.FC<{ localFrame: number; aspect: "h" | "v" }> = ({
  localFrame,
  aspect: _aspect,
}) => {
  const aspect = useAspect();
  const t = useCopy();

  const pixelSize = pick(aspect, 28, 32);

  // Pre-ignition flicker keeps the void from feeling static.
  const ignite = interpolate(localFrame, [0, 8], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: easeOut,
  });

  // 30-frame breath cycle on the seed pixel: 0.8 → 1.4 → 0.8.
  const breathPhase = (localFrame / 30) * Math.PI * 2;
  const breath = 0.8 + (Math.sin(breathPhase - Math.PI / 2) + 1) * 0.3;

  // After the cluster starts forming, calm the breathing down so the assembly reads cleanly.
  const breathAttenuation = interpolate(localFrame, [95, 115], [1, 0.35], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const seedScale = 1 + (breath - 1) * breathAttenuation;

  return (
    <Stage showGrid gridCell={pick(aspect, 28, 24)} gridOpacity={0.14} vignette={0.7}>
      <AbsoluteFill
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <div
          style={{
            position: "relative",
            width: pixelSize * 12,
            height: pixelSize * 12,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          {SHOCKWAVES.map((ring, idx) => {
            const ringProgress = interpolate(
              localFrame,
              [ring.startFrame, ring.startFrame + 50],
              [0, 1],
              {
                extrapolateLeft: "clamp",
                extrapolateRight: "clamp",
                easing: easeOut,
              },
            );
            if (ringProgress <= 0) return null;
            const radius = ringProgress * ring.maxRadius;
            const ringOpacity = (1 - ringProgress) * 0.55;
            const ringSize = radius * 2;
            return (
              <div
                key={`ring-${idx}`}
                style={{
                  position: "absolute",
                  width: ringSize,
                  height: ringSize,
                  borderRadius: "50%",
                  border: `2px dotted ${pal.cyan}`,
                  opacity: ringOpacity,
                  boxShadow: `0 0 ${pixelSize * 1.6}px ${pal.cyanGlow}`,
                  pointerEvents: "none",
                }}
              />
            );
          })}

          {CLUSTER_OFFSETS.map(([dx, dy], idx) => {
            const threshold = CLUSTER_THRESHOLDS[idx];
            if (localFrame < threshold) return null;

            const introWindow = idx === 0 ? 8 : 10;
            const intro = interpolate(
              localFrame,
              [threshold, threshold + introWindow],
              [0, 1],
              {
                extrapolateLeft: "clamp",
                extrapolateRight: "clamp",
                easing: easeOut,
              },
            );

            const isSeed = idx === 0;
            const scale = isSeed
              ? seedScale * (0.4 + 0.6 * intro)
              : 0.6 + 0.4 * intro;
            const opacity = isSeed ? ignite : intro;

            const overshoot = isSeed
              ? 0
              : (1 - intro) * (1 - intro) * pixelSize * 0.6;
            const offX = dx * pixelSize;
            const offY = dy * pixelSize - overshoot;

            return (
              <div
                key={`px-${idx}`}
                style={{
                  position: "absolute",
                  width: pixelSize,
                  height: pixelSize,
                  backgroundColor: pal.cyan,
                  transform: `translate(${offX}px, ${offY}px) scale(${scale})`,
                  transformOrigin: "center",
                  opacity,
                  boxShadow: `
                    0 0 ${pixelSize * 0.8}px ${pal.cyanGlow},
                    0 0 ${pixelSize * 2.2}px rgba(15,232,240,${0.35 * opacity}),
                    0 0 ${pixelSize * 4.5}px rgba(15,232,240,${0.18 * opacity})
                  `,
                }}
              />
            );
          })}

          <div
            style={{
              position: "absolute",
              width: pixelSize * 10,
              height: pixelSize * 10,
              borderRadius: "50%",
              background: `radial-gradient(circle, ${pal.cyanGlow} 0%, transparent 65%)`,
              opacity: 0.25 * ignite * seedScale,
              pointerEvents: "none",
              filter: `blur(${pixelSize * 0.4}px)`,
            }}
          />
        </div>
      </AbsoluteFill>

      <WorldSign title={t.s1_title} sub={t.s1_sub} variant="banner" y={300} scale={pick(aspect, 1.0, 0.85)} from={36} />
    </Stage>
  );
};
