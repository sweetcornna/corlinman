import React from "react";
import { AbsoluteFill, Easing, interpolate } from "remotion";
import { Stage } from "../Stage";
import { WorldSign } from "../WorldSign";
import { Mascot } from "../Mascot";
import { pal } from "../palette";
import { useAspect, pick, viewBox } from "../../tokens/aspect";
import { useCopy } from "../i18n";

const easeOut = Easing.bezier(0.16, 1, 0.3, 1);

// Perspective floor — receding dotted grid converging on the mascot's foot line.
// Drawn as SVG paths so the lines stay crisp at any composition size.
const PerspectiveFloor: React.FC<{ aspect: "h" | "v"; horizonY: number; opacity: number }> = ({
  aspect,
  horizonY,
  opacity,
}) => {
  const vbW = aspect === "h" ? 1920 : 1080;
  const vbH = aspect === "h" ? 1080 : 1920;
  const vanishX = vbW / 2;
  const vanishY = horizonY;

  // Verticals — 14 lines spreading outward from the vanishing point to the bottom edge.
  const verticalCount = 14;
  const bottomSpread = vbW * 1.6; // lines exit well beyond the frame edges
  const verticals: Array<{ x1: number; y1: number; x2: number; y2: number }> = [];
  for (let i = 0; i <= verticalCount; i++) {
    const t = i / verticalCount; // 0..1
    const bottomX = vanishX - bottomSpread / 2 + bottomSpread * t;
    verticals.push({ x1: vanishX, y1: vanishY, x2: bottomX, y2: vbH });
  }

  // Horizontals — 10 lines getting tighter as they recede.
  const horizCount = 10;
  const horizontals: Array<{ y: number; xSpan: number }> = [];
  for (let i = 1; i <= horizCount; i++) {
    // Exponential easing so lines bunch up near the horizon.
    const t = Math.pow(i / horizCount, 1.8);
    const y = vanishY + (vbH - vanishY) * t;
    // span: width at this depth, proportional to t.
    const xSpan = bottomSpread * (0.18 + 0.82 * t);
    horizontals.push({ y, xSpan });
  }

  return (
    <svg
      viewBox={viewBox(aspect)}
      style={{
        position: "absolute",
        inset: 0,
        width: "100%",
        height: "100%",
        opacity,
        pointerEvents: "none",
      }}
    >
      <defs>
        <linearGradient id="floorFade" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={pal.cyan} stopOpacity="0" />
          <stop offset="35%" stopColor={pal.cyan} stopOpacity="0.18" />
          <stop offset="100%" stopColor={pal.bright} stopOpacity="0.55" />
        </linearGradient>
      </defs>
      <g
        stroke="url(#floorFade)"
        strokeWidth={1.5}
        strokeDasharray="2 6"
        fill="none"
        strokeLinecap="round"
      >
        {verticals.map((v, i) => (
          <line key={`v${i}`} x1={v.x1} y1={v.y1} x2={v.x2} y2={v.y2} />
        ))}
      </g>
      <g
        stroke={pal.bright}
        strokeWidth={1.5}
        strokeDasharray="3 8"
        fill="none"
        strokeLinecap="round"
        opacity={0.45}
      >
        {horizontals.map((h, i) => (
          <line
            key={`h${i}`}
            x1={vanishX - h.xSpan / 2}
            y1={h.y}
            x2={vanishX + h.xSpan / 2}
            y2={h.y}
          />
        ))}
      </g>
    </svg>
  );
};

export const Scene10_Reflect: React.FC<{ localFrame: number; aspect: "h" | "v" }> = ({
  localFrame,
  aspect: _aspect,
}) => {
  const aspect = useAspect();
  const t = useCopy();
  const vbW = aspect === "h" ? 1920 : 1080;
  const vbH = aspect === "h" ? 1080 : 1920;

  // Mascot Y offset from composition center (negative = above).
  const mascotYOffset = pick(aspect, -80, -150);
  // Horizon Y in composition coords — slightly below the mascot's foot line.
  const horizonY = vbH / 2 + mascotYOffset + pick(aspect, 240, 280);

  // Day/Night swap — first phase has LEFT lighter, RIGHT dark. Mid-scene the
  // gradient direction inverts over ~30 frames around frame 75.
  // swapT goes 0 (left=day) → 1 (left=night) between frames 60..90.
  const swapT = interpolate(localFrame, [60, 90], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: easeOut,
  });

  // Left half luminance multiplier — high in day phase.
  const leftDay = 1 - swapT; // 1 at start → 0 after swap
  const rightDay = swapT;    // 0 at start → 1 after swap

  // Glow modulation: cooler (lower) during dark phases, warmer (higher) in lighter.
  // Average the two halves so we get a smooth midpoint dip.
  const lightingAvg = (leftDay + rightDay) / 2 + 0.5; // hovers ~1.0
  const glow = 0.7 + 0.18 * Math.abs(leftDay - rightDay); // bumps near the swap

  // Foot-line cyan band opacity — fades in over first 25 frames, persists.
  const groundOp = interpolate(localFrame, [0, 25], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: easeOut,
  });

  // Floor grid opacity — eases in slowly so it doesn't compete with the mascot drop-in.
  const floorOp = interpolate(localFrame, [10, 50], [0, 0.9], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: easeOut,
  });

  // Gentle vertical ease-in for the mascot — sliding down into pose from ~30px above.
  const mascotSettle = interpolate(localFrame, [0, 30], [-30, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: easeOut,
  });

  // Ground-line band: a soft cyan horizontal slab at the mascot's foot level.
  const groundBandY = horizonY; // in viewBox coords
  const groundBandHeight = 6;

  return (
    <Stage showGrid gridCell={pick(aspect, 26, 22)} gridOpacity={0.12} vignette={0.55}>
      {/* Day / Night vertical split background.
          Left half: pal.deep at leftDay*0.3 opacity over the void.
          Right half: pal.deep at rightDay*0.3 opacity over the void.
          A soft gradient in the middle 200px region keeps the split readable
          without a hard seam. */}
      <AbsoluteFill style={{ pointerEvents: "none" }}>
        <div
          style={{
            position: "absolute",
            inset: 0,
            background: `linear-gradient(to right,
              rgba(31,79,184,${leftDay * 0.3}) 0%,
              rgba(31,79,184,${leftDay * 0.3}) calc(50% - 100px),
              rgba(31,79,184,${(leftDay + rightDay) * 0.08}) 50%,
              rgba(31,79,184,${rightDay * 0.3}) calc(50% + 100px),
              rgba(31,79,184,${rightDay * 0.3}) 100%)`,
            mixBlendMode: "screen",
          }}
        />
        {/* A subtle warm/cool tint layer keyed to the active side, so the swap reads. */}
        <div
          style={{
            position: "absolute",
            inset: 0,
            background: `linear-gradient(to right,
              rgba(168,212,255,${leftDay * 0.06}) 0%,
              transparent 50%,
              rgba(168,212,255,${rightDay * 0.06}) 100%)`,
            mixBlendMode: "screen",
          }}
        />
      </AbsoluteFill>

      {/* Receding perspective floor */}
      <PerspectiveFloor aspect={aspect} horizonY={horizonY} opacity={floorOp} />

      {/* Soft cyan horizontal ground band sitting at the foot line. */}
      <svg
        viewBox={viewBox(aspect)}
        style={{
          position: "absolute",
          inset: 0,
          width: "100%",
          height: "100%",
          opacity: groundOp,
          pointerEvents: "none",
        }}
      >
        <defs>
          <linearGradient id="groundBand" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor={pal.cyan} stopOpacity="0" />
            <stop offset="20%" stopColor={pal.cyan} stopOpacity="0.55" />
            <stop offset="50%" stopColor={pal.cyan} stopOpacity="0.85" />
            <stop offset="80%" stopColor={pal.cyan} stopOpacity="0.55" />
            <stop offset="100%" stopColor={pal.cyan} stopOpacity="0" />
          </linearGradient>
        </defs>
        <rect
          x={0}
          y={groundBandY - groundBandHeight / 2}
          width={vbW}
          height={groundBandHeight}
          fill="url(#groundBand)"
        />
        {/* Soft cyan halo above the band — sells the wet sheen. */}
        <rect
          x={0}
          y={groundBandY - 60}
          width={vbW}
          height={120}
          fill="url(#groundBand)"
          opacity={0.18}
          style={{ filter: "blur(8px)" }}
        />
      </svg>

      {/* Mascot owned by PersistentMascot (reflect=1 in keyframes) */}

      <WorldSign title={t.s10_title} sub={t.s10_sub} variant="neon" y={pick(aspect, -360, -600)} scale={pick(aspect, 1.0, 0.85)} from={42} />
    </Stage>
  );
};
