import React from "react";
import { AbsoluteFill, Easing, interpolate } from "remotion";
import { Stage } from "../Stage";
import { WorldSign } from "../WorldSign";
import { Mascot } from "../Mascot";
import { pal } from "../palette";
import { useAspect, pick } from "../../tokens/aspect";
import { useCopy } from "../i18n";

const easeOut = Easing.bezier(0.16, 1, 0.3, 1);
const dashEase = Easing.bezier(0.34, 1.56, 0.64, 1);

// Deterministic hash → 0..1 (so SSR + render render identical layouts).
const hash01 = (n: number, salt = 1): number => {
  const x = Math.sin(n * 127.1 + salt * 311.7) * 43758.5453;
  return x - Math.floor(x);
};

type SpeedLine = {
  yFrac: number;   // -0.5..0.5 relative to half-height (kept away from mascot core)
  length: number;  // px
  thickness: number; // px
  offsetX: number; // initial x offset from mascot center, to the right
  speed: number;   // px / frame multiplier
  brightness: number; // 0.4..1 line alpha multiplier
};

const SPEED_LINES: SpeedLine[] = Array.from({ length: 14 }, (_, i) => {
  const r1 = hash01(i, 1);
  const r2 = hash01(i, 2);
  const r3 = hash01(i, 3);
  const r4 = hash01(i, 4);
  const r5 = hash01(i, 5);
  return {
    // bias away from a dead-center band so lines flank the mascot
    yFrac: (r1 - 0.5) * 0.95,
    length: 60 + r2 * 240,
    thickness: 2 + Math.floor(r3 * 3),
    offsetX: 120 + r4 * 480,
    speed: 18 + r5 * 22,
    brightness: 0.4 + r1 * 0.6,
  };
});

export const Scene04_Dash: React.FC<{ localFrame: number; aspect: "h" | "v" }> = ({
  localFrame,
  aspect: _aspect,
}) => {
  const aspect = useAspect();
  const t = useCopy();

  const startX = pick(aspect, -1100, -700);

  // Dash to center over 0..80, hold at 0 thereafter.
  const mascotX = interpolate(localFrame, [0, 80, 150], [startX, 0, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: dashEase,
  });

  // Trail intensity: full while dashing, fade to 0 by frame 110.
  const trail = interpolate(localFrame, [0, 80, 110], [0.85, 0.85, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Speed-line global alpha: peaks while dashing, falls off as mascot stops.
  const lineAlpha = interpolate(localFrame, [0, 70, 110], [0.95, 0.95, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: easeOut,
  });

  // Slight motion blur on the mascot during the dash phase.
  const motionBlurPx = interpolate(localFrame, [0, 70, 95], [10, 10, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Subtle horizontal squash while accelerating, normal at rest.
  const squashX = interpolate(localFrame, [0, 75, 95], [1.06, 1.06, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Celebratory tiny bounce after the stop (frames 90..150).
  const bounce =
    localFrame < 90
      ? 0
      : Math.sin(((localFrame - 90) / 14) * Math.PI) *
        Math.max(0, 1 - (localFrame - 90) / 60) *
        -14;

  // Impact flash when the mascot snaps to a halt (~frame 80).
  const flash = interpolate(localFrame, [78, 84, 96], [0, 0.75, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const dashAxisHalfPx = pick(aspect, 460, 800); // vertical spread band for lines

  return (
    <Stage showGrid gridCell={pick(aspect, 28, 24)} gridOpacity={0.14} vignette={0.6}>
      {/* Speed-line layer behind the mascot. Lines streak rightward of the sprite. */}
      <AbsoluteFill
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          pointerEvents: "none",
        }}
      >
        <div
          style={{
            position: "relative",
            width: 1,
            height: 1,
          }}
        >
          {SPEED_LINES.map((ln, i) => {
            // Each line advances rightward over the dash, drifting past the mascot.
            const driftStart = ln.offsetX;
            const driftEnd = ln.offsetX + ln.speed * 80;
            const x = interpolate(localFrame, [0, 80], [driftStart, driftEnd], {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
            });
            const y = ln.yFrac * dashAxisHalfPx;
            // Per-line stagger: lines have a short flash-in window keyed off mascot speed.
            const perLineAlpha = interpolate(
              localFrame,
              [0, 8 + i * 2, 70, 100],
              [0, ln.brightness, ln.brightness, 0],
              {
                extrapolateLeft: "clamp",
                extrapolateRight: "clamp",
                easing: easeOut,
              },
            );
            const op = perLineAlpha * lineAlpha;
            if (op <= 0) return null;
            return (
              <div
                key={`sl-${i}`}
                style={{
                  position: "absolute",
                  left: x,
                  top: y - ln.thickness / 2,
                  width: ln.length,
                  height: ln.thickness,
                  background: `linear-gradient(to right, rgba(15,232,240,0) 0%, ${pal.cyan} 40%, ${pal.cyan} 70%, rgba(15,232,240,0) 100%)`,
                  opacity: op,
                  boxShadow: `0 0 ${ln.thickness * 3}px ${pal.cyanGlow}`,
                }}
              />
            );
          })}
        </div>
      </AbsoluteFill>

      {/* Mascot layer — itself centers via Mascot's internal absolute positioning. */}
      <AbsoluteFill
        style={{
          // Mascot positions relative to composition center already.
          transform: `translate(0px, ${bounce}px)`,
        }}
      >
        <div
          style={{
            position: "absolute",
            inset: 0,
            filter: motionBlurPx > 0.3 ? `blur(${motionBlurPx * 0.25}px)` : undefined,
            transform: `scaleX(${squashX})`,
            transformOrigin: "center",
          }}
        >
          {/* Mascot owned by PersistentMascot at film level (trail in MascotState) */}
        </div>
      </AbsoluteFill>

      {/* Impact flash — short cyan ring when the mascot stops. */}
      {flash > 0 && (
        <AbsoluteFill
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            pointerEvents: "none",
          }}
        >
          <div
            style={{
              width: pick(aspect, 520, 460),
              height: pick(aspect, 520, 460),
              borderRadius: "50%",
              border: `3px solid ${pal.cyan}`,
              boxShadow: `0 0 60px ${pal.cyanGlow}, inset 0 0 60px ${pal.cyanGlow}`,
              opacity: flash,
              transform: `scale(${0.6 + (1 - flash) * 0.6})`,
            }}
          />
        </AbsoluteFill>
      )}

      <WorldSign title={t.s4_title} sub={t.s4_sub} variant="neon" y={pick(aspect, -340, -540)} scale={pick(aspect, 0.85, 0.75)} from={36} />
    </Stage>
  );
};
