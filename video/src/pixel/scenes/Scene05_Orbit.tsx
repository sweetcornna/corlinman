import React from "react";
import { AbsoluteFill, Easing, interpolate } from "remotion";
import { Stage } from "../Stage";
import { WorldSign } from "../WorldSign";
import { Mascot } from "../Mascot";
import { pal } from "../palette";
import { fonts } from "../../tokens/typography";
import { useAspect, pick, viewBox } from "../../tokens/aspect";
import { useCopy } from "../i18n";

const easeOut = Easing.bezier(0.16, 1, 0.3, 1);

const INNER = ["ANTHROPIC", "OPENAI", "GOOGLE"] as const;
const OUTER = ["DEEPSEEK", "QWEN", "GLM"] as const;

type LabelProps = {
  name: string;
  cx: number;
  cy: number;
  opacity: number;
};

const OrbitLabel: React.FC<LabelProps> = ({ name, cx, cy, opacity }) => {
  return (
    <div
      style={{
        position: "absolute",
        left: `calc(50% + ${cx}px)`,
        top: `calc(50% + ${cy}px)`,
        transform: "translate(-50%, -50%)",
        display: "flex",
        alignItems: "center",
        gap: 10,
        padding: "8px 14px",
        background: "rgba(5,9,18,0.55)",
        border: `1px solid ${pal.gridStrong}`,
        boxShadow: `0 0 18px rgba(15,232,240,0.18)`,
        opacity,
        pointerEvents: "none",
        whiteSpace: "nowrap",
      }}
    >
      <span
        style={{
          width: 8,
          height: 8,
          background: pal.cyan,
          boxShadow: `0 0 8px ${pal.cyanGlow}`,
        }}
      />
      <span
        style={{
          fontFamily: fonts.mono,
          fontSize: 22,
          letterSpacing: "0.22em",
          textTransform: "uppercase",
          color: pal.light,
        }}
      >
        {name}
      </span>
    </div>
  );
};

export const Scene05_Orbit: React.FC<{ localFrame: number; aspect: "h" | "v" }> = ({
  localFrame,
  aspect: _aspect,
}) => {
  const aspect = useAspect();
  const t = useCopy();

  const innerR = pick(aspect, 280, 220);
  const outerR = pick(aspect, 480, 380);

  // Continuous rotation through the full 5-second scene.
  const innerAngle = (localFrame / 150) * 360; // CW
  const outerAngle = -(localFrame / 150) * 360; // CCW

  // Entry fade for orbits + labels.
  const enter = interpolate(localFrame, [0, 40], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: easeOut,
  });

  // Pulse rings emitted every 30 frames from mascot.
  const pulses = [0, 30, 60, 90, 120].map((start) => {
    const t = interpolate(localFrame, [start, start + 40], [0, 1], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    });
    return { start, t };
  });

  const vbox = viewBox(aspect);

  // Slow background ring rotation (degrees) — visually distinct from label orbit.
  const bgRotInner = (localFrame / 150) * 60;
  const bgRotOuter = -(localFrame / 150) * 45;

  return (
    <Stage showGrid gridCell={pick(aspect, 28, 24)} gridOpacity={0.12} vignette={0.6}>
      {/* SVG layer: dotted orbit circles + pulse rings, all centered at composition midpoint. */}
      <AbsoluteFill style={{ pointerEvents: "none" }}>
        <svg
          width="100%"
          height="100%"
          viewBox={vbox}
          preserveAspectRatio="xMidYMid meet"
        >
          {(() => {
            const cx = aspect === "h" ? 960 : 540;
            const cy = aspect === "h" ? 540 : 960;
            return (
              <g>
                <g transform={`rotate(${bgRotInner} ${cx} ${cy})`} opacity={enter}>
                  <circle
                    cx={cx}
                    cy={cy}
                    r={innerR}
                    fill="none"
                    stroke={pal.gridStrong}
                    strokeWidth={1.5}
                    strokeDasharray="4 12"
                  />
                </g>
                <g transform={`rotate(${bgRotOuter} ${cx} ${cy})`} opacity={enter}>
                  <circle
                    cx={cx}
                    cy={cy}
                    r={outerR}
                    fill="none"
                    stroke={pal.gridStrong}
                    strokeWidth={1.5}
                    strokeDasharray="4 12"
                  />
                </g>

                {pulses.map(({ start, t }, i) => {
                  if (t <= 0 || t >= 1) return null;
                  const r = 40 + t * (outerR + 80);
                  const op = (1 - t) * 0.45;
                  return (
                    <circle
                      key={`pulse-${i}-${start}`}
                      cx={cx}
                      cy={cy}
                      r={r}
                      fill="none"
                      stroke={pal.cyan}
                      strokeWidth={1.5}
                      opacity={op}
                    />
                  );
                })}
              </g>
            );
          })()}
        </svg>
      </AbsoluteFill>

      {/* Mascot owned by PersistentMascot at film level */}

      {/* Orbiting labels — positioned with css transforms relative to viewport center. */}
      <AbsoluteFill style={{ pointerEvents: "none" }}>
        {INNER.map((name, idx) => {
          const a = ((innerAngle + (idx * 360) / INNER.length) * Math.PI) / 180;
          const x = Math.cos(a) * innerR;
          const y = Math.sin(a) * innerR;
          // Staggered fade-in within the entry window.
          const stagger = interpolate(
            localFrame,
            [idx * 4, idx * 4 + 20],
            [0, 1],
            { extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: easeOut },
          );
          return (
            <OrbitLabel
              key={`in-${name}`}
              name={name}
              cx={x}
              cy={y}
              opacity={enter * stagger}
            />
          );
        })}
        {OUTER.map((name, idx) => {
          const a = ((outerAngle + (idx * 360) / OUTER.length + 60) * Math.PI) / 180;
          const x = Math.cos(a) * outerR;
          const y = Math.sin(a) * outerR;
          const stagger = interpolate(
            localFrame,
            [10 + idx * 4, 10 + idx * 4 + 20],
            [0, 1],
            { extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: easeOut },
          );
          return (
            <OrbitLabel
              key={`out-${name}`}
              name={name}
              cx={x}
              cy={y}
              opacity={enter * stagger}
            />
          );
        })}
      </AbsoluteFill>

      <WorldSign
        title={t.s5_title}
        sub={t.s5_sub}
        variant="engraved"
        y={pick(aspect, 340, 500)}
        scale={pick(aspect, 1.0, 0.85)}
        from={36}
      />
    </Stage>
  );
};
