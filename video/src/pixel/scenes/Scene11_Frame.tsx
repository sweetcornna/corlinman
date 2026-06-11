import React from "react";
import { AbsoluteFill, Easing, interpolate } from "remotion";
import { Stage } from "../Stage";
import { WorldSign } from "../WorldSign";
import { Mascot } from "../Mascot";
import { pal } from "../palette";
import { useAspect, pick } from "../../tokens/aspect";
import { fonts } from "../../tokens/typography";
import { useCopy } from "../i18n";

const easeOut = Easing.bezier(0.16, 1, 0.3, 1);
const snap = Easing.bezier(0.34, 1.56, 0.64, 1);

type Corner = "tl" | "tr" | "bl" | "br";
const CORNERS: ReadonlyArray<Corner> = ["tl", "tr", "bl", "br"];

// Directional sign per corner (x, y) — used for off-screen entry and L-shape orientation.
const dirFor = (c: Corner): { dx: -1 | 1; dy: -1 | 1 } => {
  switch (c) {
    case "tl": return { dx: -1, dy: -1 };
    case "tr": return { dx: 1, dy: -1 };
    case "bl": return { dx: -1, dy: 1 };
    case "br": return { dx: 1, dy: 1 };
  }
};

// Small readout text per corner — terminal-style status lines.
const READOUTS: Record<Corner, string> = {
  tl: "ENC: 8C2A",
  tr: "CHK: OK",
  bl: "LAT: 12ms",
  br: "STS: 200",
};

// One scope bracket — a square wrapper of size 2*arm whose top-left sits at the bracket
// corner; we draw the two legs of the L using two divs so the corner is crisp.
const ScopeBracket: React.FC<{
  corner: Corner;
  targetX: number;       // distance from center to bracket corner (px, signed = sign(dx)*targetX)
  targetY: number;       // distance from center to bracket corner (px, signed = sign(dy)*targetY)
  travel: number;        // 0..1 entry progress
  pulseT: number;        // scanning dot phase 0..1 (loops)
  legLength: number;
  thickness: number;
  flash: number;         // 0..1 flash strength
  readoutOp: number;     // 0..1 fade of readout text
}> = ({ corner, targetX, targetY, travel, pulseT, legLength, thickness, flash, readoutOp }) => {
  const { dx, dy } = dirFor(corner);

  // Off-screen start offset (way outside) plus tiny overshoot at travel≈1.
  const offX = (1 - travel) * 700 * dx;
  const offY = (1 - travel) * 700 * dy;

  // Final position: bracket "corner" point at (dx*targetX, dy*targetY) from composition center.
  // The bracket's own anchor is its corner; legs extend INWARD (toward center).
  const cornerX = dx * targetX + offX;
  const cornerY = dy * targetY + offY;

  // Leg orientations: legs go from corner toward center, so opposite of (dx, dy).
  // Horizontal leg: from cornerX inward along -dx, length=legLength, thickness=thickness.
  // Vertical leg: from cornerY inward along -dy.
  // We'll position both legs absolutely within an outer wrapper centered at composition center.
  const baseColor = pal.cyan;
  const flashColor = flash > 0.01
    ? `rgba(255,255,255,${flash})`
    : "transparent";

  // Scanning dot — a small bright square that slides along ONE leg per pulse cycle.
  // Alternate between horizontal and vertical leg over time so each bracket gets a sweep.
  const sweepOnHoriz = (pulseT < 0.5);
  const sweepLocal = sweepOnHoriz ? pulseT / 0.5 : (pulseT - 0.5) / 0.5;
  const sweepVisible = travel > 0.95;
  const sweepSize = thickness + 4;

  // Horizontal leg lives along x from cornerX (outer end) toward center.
  // Inner end x: cornerX + (-dx) * legLength
  const horizInnerX = cornerX + -dx * legLength;
  const horizMinX = Math.min(cornerX, horizInnerX);
  // y centered on cornerY (with thickness)
  const horizTop = cornerY - thickness / 2;

  const vertInnerY = cornerY + -dy * legLength;
  const vertMinY = Math.min(cornerY, vertInnerY);
  const vertLeft = cornerX - thickness / 2;

  // Sweep position along the horizontal leg (from outer corner inward).
  const sweepXAlong = sweepLocal; // 0..1
  const sweepXPx = cornerX + -dx * sweepXAlong * legLength - sweepSize / 2;
  const sweepYPx = cornerY + -dy * sweepXAlong * legLength - sweepSize / 2;

  // Readout position — anchored at the inside-most tip of the bracket corner with a small offset.
  // Place text BELOW (for top corners) or ABOVE (for bottom corners), and aligned with the leg.
  const readoutFontSize = 14;
  const readoutOffset = 14; // px from corner
  const readoutLeft = cornerX + -dx * (legLength * 0.35);
  const readoutTop = cornerY + dy * readoutOffset;
  // Align text so it always sits inside the frame.
  const textAlign: React.CSSProperties["textAlign"] = dx === -1 ? "left" : "right";

  return (
    <div style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
      {/* Horizontal leg */}
      <div
        style={{
          position: "absolute",
          left: `calc(50% + ${horizMinX}px)`,
          top: `calc(50% + ${horizTop}px)`,
          width: legLength,
          height: thickness,
          background: baseColor,
          boxShadow: `0 0 ${thickness * 3}px ${pal.cyanGlow}`,
          imageRendering: "pixelated",
        }}
      />
      {/* Vertical leg */}
      <div
        style={{
          position: "absolute",
          left: `calc(50% + ${vertLeft}px)`,
          top: `calc(50% + ${vertMinY}px)`,
          width: thickness,
          height: legLength,
          background: baseColor,
          boxShadow: `0 0 ${thickness * 3}px ${pal.cyanGlow}`,
          imageRendering: "pixelated",
        }}
      />
      {/* Corner cap (crisp square at the intersection) */}
      <div
        style={{
          position: "absolute",
          left: `calc(50% + ${cornerX - thickness / 2}px)`,
          top: `calc(50% + ${cornerY - thickness / 2}px)`,
          width: thickness + 2,
          height: thickness + 2,
          background: pal.highlight,
          boxShadow: `0 0 ${thickness * 4}px ${pal.cyan}`,
        }}
      />
      {/* Lock-in flash overlay on the corner cap */}
      {flash > 0.01 && (
        <div
          style={{
            position: "absolute",
            left: `calc(50% + ${cornerX - 12}px)`,
            top: `calc(50% + ${cornerY - 12}px)`,
            width: 24,
            height: 24,
            background: flashColor,
            boxShadow: `0 0 40px rgba(255,255,255,${flash * 0.8}), 0 0 80px ${pal.cyanGlow}`,
            borderRadius: 2,
          }}
        />
      )}
      {/* Scanning dot — alternates between horiz/vert legs */}
      {sweepVisible && (
        <div
          style={{
            position: "absolute",
            left: sweepOnHoriz
              ? `calc(50% + ${sweepXPx}px)`
              : `calc(50% + ${cornerX - sweepSize / 2}px)`,
            top: sweepOnHoriz
              ? `calc(50% + ${cornerY - sweepSize / 2}px)`
              : `calc(50% + ${sweepYPx}px)`,
            width: sweepSize,
            height: sweepSize,
            background: pal.white,
            boxShadow: `0 0 16px ${pal.cyan}, 0 0 32px ${pal.cyanGlow}`,
          }}
        />
      )}
      {/* Readout text — tiny mono label at the corner. */}
      {readoutOp > 0 && (
        <div
          style={{
            position: "absolute",
            left: `calc(50% + ${readoutLeft - 60}px)`,
            top: `calc(50% + ${readoutTop - (dy === -1 ? 22 : 0)}px)`,
            width: 120,
            fontFamily: fonts.mono,
            fontSize: readoutFontSize,
            letterSpacing: "0.18em",
            color: pal.cyan,
            opacity: readoutOp,
            textAlign,
            textShadow: `0 0 8px ${pal.cyanGlow}`,
          }}
        >
          {READOUTS[corner]}
        </div>
      )}
    </div>
  );
};

export const Scene11_Frame: React.FC<{ localFrame: number; aspect: "h" | "v" }> = ({
  localFrame,
  aspect: _aspect,
}) => {
  const aspect = useAspect();
  const t = useCopy();

  // Bracket geometry — distance from center to each bracket's corner point.
  const targetX = pick(aspect, 320, 280);
  const targetY = pick(aspect, 300, 380);
  const legLength = pick(aspect, 80, 70);
  const thickness = 6;

  // Travel: 0..1 over frames 0..30 with overshoot easing.
  const travel = interpolate(localFrame, [0, 30], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: snap,
  });

  // Lock-in flash 30..45.
  const flash = interpolate(localFrame, [30, 36, 45], [0, 0.85, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: easeOut,
  });

  // Scanning pulse — one cycle every 60 frames once locked.
  const pulseT = ((localFrame - 45) % 60) / 60;

  // Readouts fade in after frame 60.
  const readoutOp = interpolate(localFrame, [60, 80], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: easeOut,
  });

  // Crosshair behind mascot — fades in early, sits low-opacity throughout.
  const crosshairOp = interpolate(localFrame, [10, 40], [0, 0.4], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: easeOut,
  });

  return (
    <Stage showGrid gridCell={pick(aspect, 24, 22)} gridOpacity={0.14} vignette={0.55}>
      {/* Crosshair behind mascot */}
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
          <div
            style={{
              position: "absolute",
              left: -60,
              top: -1,
              width: 120,
              height: 2,
              background: pal.cyan,
              opacity: crosshairOp,
              boxShadow: `0 0 8px ${pal.cyanGlow}`,
            }}
          />
          <div
            style={{
              position: "absolute",
              left: -1,
              top: -60,
              width: 2,
              height: 120,
              background: pal.cyan,
              opacity: crosshairOp,
              boxShadow: `0 0 8px ${pal.cyanGlow}`,
            }}
          />
          <div
            style={{
              position: "absolute",
              left: -5,
              top: -5,
              width: 10,
              height: 10,
              background: pal.cyan,
              opacity: crosshairOp * 1.4,
              boxShadow: `0 0 12px ${pal.cyan}`,
            }}
          />
        </div>
      </AbsoluteFill>

      {/* Mascot owned by PersistentMascot */}

      {/* Four scope brackets */}
      {CORNERS.map((c) => (
        <ScopeBracket
          key={c}
          corner={c}
          targetX={targetX}
          targetY={targetY}
          travel={travel}
          pulseT={pulseT}
          legLength={legLength}
          thickness={thickness}
          flash={flash}
          readoutOp={readoutOp}
        />
      ))}

      <WorldSign title={t.s11_title} sub={t.s11_sub} variant="neon" y={pick(aspect, 360, 580)} scale={pick(aspect, 0.9, 0.78)} from={48} />
    </Stage>
  );
};
