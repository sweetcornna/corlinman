import React from "react";
import { AbsoluteFill, Easing, interpolate } from "remotion";
import { Stage } from "../Stage";
import { WorldSign } from "../WorldSign";
import { Mascot } from "../Mascot";
import { pal } from "../palette";
import { useAspect, pick, viewBox } from "../../tokens/aspect";
import { useCopy } from "../i18n";

const easeOut = Easing.bezier(0.16, 1, 0.3, 1);

// Corner reticle bracket — drawn as 4 path L-shapes inset by 1/3 of viewport.
// Returns SVG path data for one bracket given its anchor corner and arm length.
const bracketPath = (
  cx: number,
  cy: number,
  dirX: 1 | -1,
  dirY: 1 | -1,
  arm: number,
): string => {
  const tipX = cx;
  const tipY = cy;
  const endX = cx + dirX * arm;
  const endY = cy + dirY * arm;
  return `M ${endX} ${tipY} L ${tipX} ${tipY} L ${tipX} ${endY}`;
};

export const Scene03_Hero: React.FC<{ localFrame: number; aspect: "h" | "v" }> = ({
  localFrame,
  aspect: _aspect,
}) => {
  const aspect = useAspect();
  const t = useCopy();

  // Camera-zoom on the whole scene — subtle 1.0 → 1.06 push.
  const zoom = interpolate(localFrame, [0, 150], [1.0, 1.06], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Ripple behind the mascot — single expanding cyan halo, fr 40-80.
  const rippleProgress = interpolate(localFrame, [40, 80], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: easeOut,
  });
  const rippleRadius = rippleProgress * pick(aspect, 760, 560);
  const rippleOpacity = rippleProgress > 0 && rippleProgress < 1
    ? (1 - rippleProgress) * 0.45
    : 0;

  // HUD reticle brackets fade in at 90, out by 130.
  const reticleOp = interpolate(
    localFrame,
    [90, 105, 120, 132],
    [0, 1, 1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: easeOut },
  );

  const vbW = aspect === "h" ? 1920 : 1080;
  const vbH = aspect === "h" ? 1080 : 1920;
  const insetX = vbW / 3;
  const insetY = vbH / 3;
  const arm = pick(aspect, 80, 70);

  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        transform: `scale(${zoom})`,
        transformOrigin: "center center",
      }}
    >
      <Stage showGrid gridCell={pick(aspect, 24, 22)} gridOpacity={0.16} vignette={0.5}>
        <AbsoluteFill
          style={{ display: "flex", alignItems: "center", justifyContent: "center" }}
        >
          {rippleOpacity > 0 && (
            <div
              style={{
                position: "absolute",
                width: rippleRadius * 2,
                height: rippleRadius * 2,
                borderRadius: "50%",
                background: `radial-gradient(circle, transparent 58%, ${pal.cyanGlow} 70%, transparent 88%)`,
                opacity: rippleOpacity,
                filter: "blur(4px)",
                pointerEvents: "none",
              }}
            />
          )}
        </AbsoluteFill>

        <Mascot
          scale={pick(aspect, 0.9, 1.0)}
          x={0}
          y={0}
          glow={0.9}
          blink
          breathe
          localFrame={localFrame}
        />

        {reticleOp > 0 && (
          <svg
            viewBox={viewBox(aspect)}
            style={{
              position: "absolute",
              inset: 0,
              width: "100%",
              height: "100%",
              opacity: reticleOp,
              pointerEvents: "none",
            }}
          >
            <g
              stroke={pal.cyan}
              strokeWidth={3}
              strokeDasharray="6 8"
              fill="none"
              strokeLinecap="square"
              opacity={0.85}
            >
              <path d={bracketPath(insetX, insetY, -1, -1, arm)} />
              <path d={bracketPath(vbW - insetX, insetY, 1, -1, arm)} />
              <path d={bracketPath(insetX, vbH - insetY, -1, 1, arm)} />
              <path d={bracketPath(vbW - insetX, vbH - insetY, 1, 1, arm)} />
            </g>
          </svg>
        )}

        <WorldSign title={t.s3_title} sub={t.s3_sub} variant="billboard" y={pick(aspect, 320, 460)} scale={pick(aspect, 1.0, 0.85)} from={30} />
        <WorldSign title={t.s3_above_title} variant="neon" y={pick(aspect, -300, -480)} scale={pick(aspect, 0.7, 0.6)} from={48} />
      </Stage>
    </div>
  );
};
