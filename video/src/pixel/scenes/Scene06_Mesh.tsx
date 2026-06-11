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

type PluginNode = {
  glyph: string;
  angleDeg: number;
  radius: number; // px from composition center
};

// 8 plugins arranged around the mascot. Angles are spread but slightly jittered
// so the ring doesn't feel mechanically uniform; radii alternate inside/outside
// the nominal band specified in the brief.
const HNODES: PluginNode[] = [
  { glyph: ">", angleDeg: -90, radius: 340 },
  { glyph: "⚙", angleDeg: -45, radius: 420 },
  { glyph: "↔", angleDeg: 0, radius: 360 },
  { glyph: "▣", angleDeg: 45, radius: 430 },
  { glyph: "◇", angleDeg: 90, radius: 350 },
  { glyph: "△", angleDeg: 135, radius: 440 },
  { glyph: "○", angleDeg: 180, radius: 370 },
  { glyph: "✱", angleDeg: 225, radius: 410 },
];

const VNODES: PluginNode[] = [
  { glyph: ">", angleDeg: -90, radius: 300 },
  { glyph: "⚙", angleDeg: -45, radius: 380 },
  { glyph: "↔", angleDeg: 0, radius: 320 },
  { glyph: "▣", angleDeg: 45, radius: 390 },
  { glyph: "◇", angleDeg: 90, radius: 310 },
  { glyph: "△", angleDeg: 135, radius: 400 },
  { glyph: "○", angleDeg: 180, radius: 330 },
  { glyph: "✱", angleDeg: 225, radius: 370 },
];

type RenderNode = PluginNode & {
  cx: number; // svg-space center x (composition coords)
  cy: number; // svg-space center y
  nodeFade: number; // 0..1
  lineFade: number; // 0..1
  packetT: number; // 0..1 position along line from node→mascot
};

export const Scene06_Mesh: React.FC<{ localFrame: number; aspect: "h" | "v" }> = ({
  localFrame,
  aspect: _aspect,
}) => {
  const aspect = useAspect();
  const t = useCopy();

  const cx = aspect === "h" ? 960 : 540;
  // mascot is shifted up slightly per brief; match the SVG center to it.
  const mascotYOffset = pick(aspect, -20, -60);
  const cy = (aspect === "h" ? 540 : 960) + mascotYOffset;

  const nodes = (aspect === "h" ? HNODES : VNODES).map((n, i) => {
    const a = (n.angleDeg * Math.PI) / 180;
    const nx = cx + Math.cos(a) * n.radius;
    const ny = cy + Math.sin(a) * n.radius;

    // Stagger: ~7 frames between nodes. Total stagger ≈ 56 frames.
    const nodeStart = i * 7;
    const nodeFade = interpolate(
      localFrame,
      [nodeStart, nodeStart + 14],
      [0, 1],
      { extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: easeOut },
    );

    // Lines draw in after their node appears.
    const lineStart = nodeStart + 10;
    const lineFade = interpolate(
      localFrame,
      [lineStart, lineStart + 16],
      [0, 1],
      { extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: easeOut },
    );

    // Data packet cycles continuously after the line is drawn.
    // Period 36 frames per packet; offset per-index so packets aren't synced.
    const period = 36;
    const phase = ((localFrame - lineStart + i * 5) % period + period) % period;
    const packetT = phase / period;

    return { ...n, cx: nx, cy: ny, nodeFade, lineFade, packetT };
  }) as RenderNode[];

  // Handshake pulse around frame 90.
  const handshake = interpolate(localFrame, [90, 120, 140], [0, 1, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: easeOut,
  });
  // Convert to a fading-expanding ring.
  const handshakeProgress = interpolate(localFrame, [90, 140], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const handshakeOpacity = handshake * (1 - handshakeProgress) * 0.7;
  const handshakeRadius = 60 + handshakeProgress * pick(aspect, 520, 460);

  const vbox = viewBox(aspect);

  return (
    <Stage showGrid gridCell={pick(aspect, 28, 24)} gridOpacity={0.12} vignette={0.6}>
      {/* SVG layer for lines, packets, handshake — all in composition coords. */}
      <AbsoluteFill style={{ pointerEvents: "none" }}>
        <svg
          width="100%"
          height="100%"
          viewBox={vbox}
          preserveAspectRatio="xMidYMid meet"
        >
          {/* Lines: from each node center toward mascot center. */}
          {nodes.map((n, i) => {
            if (n.lineFade <= 0) return null;
            // Trim endpoints so the line doesn't visually pierce the node box or mascot.
            const dx = cx - n.cx;
            const dy = cy - n.cy;
            const len = Math.sqrt(dx * dx + dy * dy) || 1;
            const ux = dx / len;
            const uy = dy / len;
            const nodeInset = 24;
            const mascotInset = pick(aspect, 130, 150);
            const x1 = n.cx + ux * nodeInset;
            const y1 = n.cy + uy * nodeInset;
            const x2 = cx - ux * mascotInset;
            const y2 = cy - uy * mascotInset;

            // Use strokeDasharray + dashoffset to "draw" the line in.
            const segLen = Math.sqrt(
              (x2 - x1) * (x2 - x1) + (y2 - y1) * (y2 - y1),
            );
            const dashOffset = segLen * (1 - n.lineFade);

            // Packet position along trimmed segment, node→mascot.
            const px = x1 + (x2 - x1) * n.packetT;
            const py = y1 + (y2 - y1) * n.packetT;

            return (
              <g key={`line-${i}`}>
                <line
                  x1={x1}
                  y1={y1}
                  x2={x2}
                  y2={y2}
                  stroke={pal.bright}
                  strokeWidth={1.5}
                  strokeDasharray={`${segLen} ${segLen}`}
                  strokeDashoffset={dashOffset}
                  opacity={0.7}
                />
                {n.lineFade >= 1 && (
                  <circle
                    cx={px}
                    cy={py}
                    r={4}
                    fill={pal.cyan}
                    opacity={0.95}
                  />
                )}
              </g>
            );
          })}

          {/* Handshake ring expanding from mascot. */}
          {handshakeOpacity > 0 && (
            <circle
              cx={cx}
              cy={cy}
              r={handshakeRadius}
              fill="none"
              stroke={pal.cyan}
              strokeWidth={2}
              opacity={handshakeOpacity}
            />
          )}
        </svg>
      </AbsoluteFill>

      {/* Plugin nodes — DOM boxes (so the unicode glyph stays crisp). */}
      <AbsoluteFill style={{ pointerEvents: "none" }}>
        {nodes.map((n, i) => {
          if (n.nodeFade <= 0) return null;
          // Offset relative to composition center for css positioning.
          const offX = n.cx - (aspect === "h" ? 960 : 540);
          const offY = n.cy - (aspect === "h" ? 540 : 960);
          // Pop-in scale.
          const scale = 0.6 + n.nodeFade * 0.4;
          return (
            <div
              key={`node-${i}`}
              style={{
                position: "absolute",
                left: `calc(50% + ${offX}px)`,
                top: `calc(50% + ${offY}px)`,
                transform: `translate(-50%, -50%) scale(${scale})`,
                width: 36,
                height: 36,
                border: `2px solid ${pal.bright}`,
                background: pal.navy,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                color: pal.cyan,
                fontFamily: fonts.mono,
                fontSize: 18,
                lineHeight: 1,
                opacity: n.nodeFade,
                boxShadow: `0 0 12px rgba(15,232,240,0.35)`,
              }}
            >
              {n.glyph}
            </div>
          );
        })}
      </AbsoluteFill>

      {/* Mascot owned by PersistentMascot */}

      <WorldSign
        title={t.s6_title}
        sub={t.s6_sub}
        variant="billboard"
        y={pick(aspect, 340, 540)}
        scale={pick(aspect, 1.0, 0.85)}
        from={42}
      />
    </Stage>
  );
};
