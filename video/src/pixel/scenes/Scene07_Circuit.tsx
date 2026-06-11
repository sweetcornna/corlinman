import React from "react";
import { AbsoluteFill, Easing, interpolate } from "remotion";
import { Stage } from "../Stage";
import { WorldSign } from "../WorldSign";
import { Mascot } from "../Mascot";
import { pal } from "../palette";
import { useAspect, pick, viewBox } from "../../tokens/aspect";
import { useCopy } from "../i18n";

const easeOut = Easing.bezier(0.16, 1, 0.3, 1);

// A single right-angle trace defined as a polyline (px coords in viewBox space).
// "start" is the mascot-edge anchor, "end" is the terminal pad.
type Trace = {
  id: string;
  points: Array<[number, number]>;
  fireFrames?: number[]; // frames at which this terminal "hot-swaps"
};

// Build a polyline of right-angle segments from start out to end, with one or two elbows.
const tracePath = (pts: Array<[number, number]>): string =>
  pts.map((p, i) => `${i === 0 ? "M" : "L"} ${p[0]} ${p[1]}`).join(" ");

// Approximate path length for dashing (right-angle segments only).
const tracePathLen = (pts: Array<[number, number]>): number => {
  let total = 0;
  for (let i = 1; i < pts.length; i++) {
    total += Math.abs(pts[i][0] - pts[i - 1][0]) + Math.abs(pts[i][1] - pts[i - 1][1]);
  }
  return total;
};

// Cumulative segment lengths so we can find a point at distance d along the trace.
const cumLengths = (pts: Array<[number, number]>): number[] => {
  const out = [0];
  for (let i = 1; i < pts.length; i++) {
    out.push(out[i - 1] + Math.abs(pts[i][0] - pts[i - 1][0]) + Math.abs(pts[i][1] - pts[i - 1][1]));
  }
  return out;
};

// Find point at distance d along the polyline.
const pointAt = (pts: Array<[number, number]>, d: number): [number, number] => {
  const cum = cumLengths(pts);
  const total = cum[cum.length - 1];
  const dist = Math.max(0, Math.min(total, d));
  for (let i = 1; i < pts.length; i++) {
    if (dist <= cum[i]) {
      const segLen = cum[i] - cum[i - 1] || 1;
      const t = (dist - cum[i - 1]) / segLen;
      const x = pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * t;
      const y = pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * t;
      return [x, y];
    }
  }
  return pts[pts.length - 1];
};

export const Scene07_Circuit: React.FC<{ localFrame: number; aspect: "h" | "v" }> = ({
  localFrame,
  aspect: _aspect,
}) => {
  const aspect = useAspect();
  const t = useCopy();
  const mascotScale = pick(aspect, 0.55, 0.65);

  // Mascot occupies roughly 480 * scale px wide. We use that to set anchor offsets.
  const mascotHalf = (480 * mascotScale) / 2;

  // Build traces in viewBox space. H: 1920x1080 with origin at top-left; we work
  // around center (960, 540). V: 1080x1920, center (540, 960).
  const cx = pick(aspect, 960, 540);
  const cy = pick(aspect, 540, 960);

  // Anchor points on the mascot's left/right (H) or top/bottom (V) edges.
  // Spread them vertically/horizontally so traces don't overlap.
  const anchorOffset = mascotHalf * 0.85;

  const traces: Trace[] = aspect === "h"
    ? [
        // 3 LEFT traces
        {
          id: "L1",
          points: [
            [cx - anchorOffset, cy - 180],
            [400, cy - 180],
            [400, 200],
            [120, 200],
          ],
          fireFrames: [60],
        },
        {
          id: "L2",
          points: [
            [cx - anchorOffset, cy],
            [180, cy],
          ],
        },
        {
          id: "L3",
          points: [
            [cx - anchorOffset, cy + 180],
            [400, cy + 180],
            [400, 880],
            [120, 880],
          ],
        },
        // 3 RIGHT traces
        {
          id: "R1",
          points: [
            [cx + anchorOffset, cy - 180],
            [1520, cy - 180],
            [1520, 200],
            [1800, 200],
          ],
          fireFrames: [110],
        },
        {
          id: "R2",
          points: [
            [cx + anchorOffset, cy],
            [1740, cy],
          ],
        },
        {
          id: "R3",
          points: [
            [cx + anchorOffset, cy + 180],
            [1520, cy + 180],
            [1520, 880],
            [1800, 880],
          ],
        },
      ]
    : [
        // 2 UP traces
        {
          id: "U1",
          points: [
            [cx - 120, cy - anchorOffset],
            [cx - 120, 420],
            [220, 420],
            [220, 180],
          ],
          fireFrames: [60],
        },
        {
          id: "U2",
          points: [
            [cx + 120, cy - anchorOffset],
            [cx + 120, 420],
            [860, 420],
            [860, 180],
          ],
        },
        // 2 DOWN traces
        {
          id: "D1",
          points: [
            [cx - 120, cy + anchorOffset],
            [cx - 120, 1500],
            [220, 1500],
            [220, 1740],
          ],
        },
        {
          id: "D2",
          points: [
            [cx + 120, cy + anchorOffset],
            [cx + 120, 1500],
            [860, 1500],
            [860, 1740],
          ],
          fireFrames: [110],
        },
      ];

  // Trace draw-in (frames 0..30, staggered per trace index).
  const traceDraw = (idx: number) => {
    const start = idx * 3;
    return interpolate(localFrame, [start, start + 28], [0, 1], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
      easing: easeOut,
    });
  };

  // Continuous packet travel — each trace has packets staggered along it.
  // A packet takes ~40 frames to traverse; we render 2 packets per trace,
  // offset by half a cycle, so the line feels continuously animated.
  const packetCycle = 40;

  const packetT = (idx: number, k: number): number => {
    const localPhase = ((localFrame + idx * 7 + k * (packetCycle / 2)) % packetCycle) / packetCycle;
    return localPhase;
  };

  const packetVisibleStart = 30; // packets begin after trace is drawn

  return (
    <Stage showGrid gridCell={pick(aspect, 28, 24)} gridOpacity={0.16} vignette={0.6}>
      <AbsoluteFill>
        <svg
          width="100%"
          height="100%"
          viewBox={viewBox(aspect)}
          preserveAspectRatio="xMidYMid meet"
          style={{ position: "absolute", inset: 0, pointerEvents: "none" }}
        >
          {traces.map((tr, idx) => {
            const draw = traceDraw(idx);
            const len = tracePathLen(tr.points);
            const dash = `${len * draw} ${len * (1 - draw) + 0.01}`;
            return (
              <g key={tr.id}>
                {/* Outer glow */}
                <path
                  d={tracePath(tr.points)}
                  stroke={pal.bright}
                  strokeWidth={10}
                  strokeOpacity={0.18}
                  fill="none"
                  strokeDasharray={dash}
                  strokeLinejoin="miter"
                  strokeLinecap="butt"
                />
                {/* Main trace - chunky pixel-art rectangle feel */}
                <path
                  d={tracePath(tr.points)}
                  stroke={pal.bright}
                  strokeWidth={4}
                  fill="none"
                  strokeDasharray={dash}
                  strokeLinejoin="miter"
                  strokeLinecap="butt"
                />
              </g>
            );
          })}

          {/* Animated packets */}
          {traces.map((tr, idx) => {
            const draw = traceDraw(idx);
            if (draw < 1) return null;
            if (localFrame < packetVisibleStart) return null;
            const len = tracePathLen(tr.points);
            return (
              <g key={`pkt-${tr.id}`}>
                {[0, 1].map((k) => {
                  const t = packetT(idx, k);
                  const [px, py] = pointAt(tr.points, t * len);
                  const op = Math.sin(t * Math.PI); // fade in/out across the run
                  return (
                    <g key={k} transform={`translate(${px} ${py}) rotate(45)`} opacity={op}>
                      <rect
                        x={-12}
                        y={-12}
                        width={24}
                        height={24}
                        fill={pal.cyan}
                        opacity={0.25}
                      />
                      <rect
                        x={-5}
                        y={-5}
                        width={10}
                        height={10}
                        fill={pal.cyan}
                      />
                    </g>
                  );
                })}
              </g>
            );
          })}

          {/* Terminal pads (cyan 24x24) at the end of each trace */}
          {traces.map((tr, idx) => {
            const draw = traceDraw(idx);
            const end = tr.points[tr.points.length - 1];
            const pulse = 0.7 + 0.3 * Math.sin((localFrame + idx * 11) / 12);

            // Fire bursts on schedule
            let burstScale = 1;
            let burstFlash = 0;
            let ripple = 0;
            for (const f of tr.fireFrames ?? []) {
              const progress = interpolate(localFrame, [f, f + 35], [0, 1], {
                extrapolateLeft: "clamp",
                extrapolateRight: "clamp",
                easing: easeOut,
              });
              if (progress > 0 && progress < 1) {
                const punch = Math.sin(progress * Math.PI);
                burstScale = Math.max(burstScale, 1 + punch * 0.6);
                burstFlash = Math.max(burstFlash, punch);
                ripple = Math.max(ripple, progress);
              }
            }

            return (
              <g key={`pad-${tr.id}`} opacity={draw}>
                {/* Ripple ring for hot-swap event */}
                {ripple > 0 && ripple < 1 && (
                  <circle
                    cx={end[0]}
                    cy={end[1]}
                    r={ripple * 90}
                    fill="none"
                    stroke={pal.cyan}
                    strokeWidth={3}
                    strokeOpacity={(1 - ripple) * 0.85}
                  />
                )}
                {/* Outer halo */}
                <rect
                  x={end[0] - 24 * burstScale}
                  y={end[1] - 24 * burstScale}
                  width={48 * burstScale}
                  height={48 * burstScale}
                  fill={pal.cyan}
                  opacity={0.15 * pulse}
                />
                {/* Pad */}
                <rect
                  x={end[0] - 12 * burstScale}
                  y={end[1] - 12 * burstScale}
                  width={24 * burstScale}
                  height={24 * burstScale}
                  fill={pal.cyan}
                  opacity={Math.min(1, 0.7 * pulse + burstFlash)}
                />
                {/* White flash core during burst */}
                {burstFlash > 0 && (
                  <rect
                    x={end[0] - 6}
                    y={end[1] - 6}
                    width={12}
                    height={12}
                    fill={pal.white}
                    opacity={burstFlash}
                  />
                )}
              </g>
            );
          })}
        </svg>
      </AbsoluteFill>

      {/* Mascot owned by PersistentMascot */}

      <WorldSign
        title={t.s7_title}
        sub={t.s7_sub}
        variant="neon"
        y={pick(aspect, -340, -560)}
        scale={pick(aspect, 1.0, 0.85)}
        from={30}
      />
    </Stage>
  );
};
