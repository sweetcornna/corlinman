import React from "react";
import { AbsoluteFill, Easing, interpolate, spring, useVideoConfig } from "remotion";
import { Stage } from "../Stage";
import { WorldSign } from "../WorldSign";
import { Mascot } from "../Mascot";
import { pal } from "../palette";
import { useAspect, pick, viewBox } from "../../tokens/aspect";
import { fonts } from "../../tokens/typography";
import { useCopy } from "../i18n";

const easeOut = Easing.bezier(0.16, 1, 0.3, 1);

type CardSpec = {
  cmd: string;
  sub: string;
  approval?: boolean;
};

const CARDS: CardSpec[] = [
  { cmd: "> shell.exec", sub: `"git status"` },
  { cmd: "> rag.query", sub: `"vector_store(5)"` },
  { cmd: "> browser.click", sub: `"#submit"` },
  { cmd: "> approval.grant", sub: `"hitl: confirm"`, approval: true },
];

// Frames at which each card enters (staggered by 30).
const ENTER_FRAMES = [10, 40, 70, 90];

export const Scene09_Cards: React.FC<{ localFrame: number; aspect: "h" | "v" }> = ({
  localFrame,
  aspect: _aspect,
}) => {
  const aspect = useAspect();
  const t = useCopy();
  const { fps } = useVideoConfig();

  // Mascot position kept in sync with PersistentMascot (MascotState) so
  // connector lines really start from the live mascot.
  const mascotX = pick(aspect, -650, 0);
  const mascotY = pick(aspect, 0, -550);
  const mascotScale = 0.4;

  // Cards stack VERTICALLY in both H and V — only the dimensions change.
  // V is 1080 wide so cardW must fit with margin; cards sit below the mascot.
  const cardW = pick(aspect, 380, 640);
  const cardH = pick(aspect, 100, 110);
  const cardGap = pick(aspect, 24, 22);

  // Stack centre offset (px from composition centre).
  // H: stack lives to the right of the mascot.
  // V: stack lives below the mascot.
  const stackCenterX = pick(aspect, 320, 0);
  const stackCenterY = pick(aspect, 0, 230);

  // Total stack height (cards stacked vertically in BOTH aspects)
  const stackSpan = CARDS.length * cardH + (CARDS.length - 1) * cardGap;

  // SVG canvas dimensions for the connector line
  const svgW = pick(aspect, 1920, 1080);
  const svgH = pick(aspect, 1080, 1920);

  // Mascot center in svg coords (canvas-relative)
  const mascotCx = svgW / 2 + mascotX;
  const mascotCy = svgH / 2 + mascotY;

  return (
    <Stage showGrid gridCell={pick(aspect, 28, 24)} gridOpacity={0.14} vignette={0.6}>
      {/* SVG layer for connector lines (under cards) */}
      <AbsoluteFill style={{ pointerEvents: "none" }}>
        <svg
          width="100%"
          height="100%"
          viewBox={viewBox(aspect)}
          preserveAspectRatio="xMidYMid meet"
          style={{ position: "absolute", inset: 0 }}
        >
          {CARDS.map((_, idx) => {
            const enter = ENTER_FRAMES[idx];
            const lineOp = interpolate(
              localFrame,
              [enter, enter + 18],
              [0, 0.45],
              {
                extrapolateLeft: "clamp",
                extrapolateRight: "clamp",
                easing: easeOut,
              },
            );
            if (lineOp <= 0) return null;

            // Card centre in SVG/composition coords. Cards always stack
            // vertically, so y advances with idx.
            const cardIndexOffset = (idx - (CARDS.length - 1) / 2) * (cardH + cardGap);
            const cardCx = svgW / 2 + stackCenterX;
            const cardCy = svgH / 2 + stackCenterY + cardIndexOffset;
            // Wire enters the LEFT edge of the card on H, TOP edge on V.
            const targetX = aspect === "h" ? cardCx - cardW / 2 : cardCx;
            const targetY = aspect === "h" ? cardCy : cardCy - cardH / 2;

            return (
              <line
                key={`conn-${idx}`}
                x1={mascotCx}
                y1={mascotCy}
                x2={targetX}
                y2={targetY}
                stroke={pal.cyan}
                strokeWidth={2}
                strokeOpacity={lineOp}
                strokeDasharray="3 6"
              />
            );
          })}
        </svg>
      </AbsoluteFill>

      {/* Mascot owned by PersistentMascot */}

      {/* Cards layer */}
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
            width: cardW,
            height: stackSpan,
            transform: `translate(${stackCenterX}px, ${stackCenterY}px)`,
          }}
        >
          {CARDS.map((card, idx) => {
            const enter = ENTER_FRAMES[idx];
            const slideIn = spring({
              frame: localFrame - enter,
              fps,
              config: { damping: 18, stiffness: 140, mass: 0.9 },
            });

            // Cards always stack vertically; slide-in direction differs by aspect:
            // H: slide in from the right. V: slide in from the bottom.
            const offX = aspect === "h" ? (1 - slideIn) * 900 : 0;
            const offY = aspect === "v" ? (1 - slideIn) * 900 : 0;

            // Slot position within the container (always vertical stack).
            // Container is sized cardW × stackSpan, so cards live at left:0.
            const slotIdx = idx - (CARDS.length - 1) / 2;
            const slotX = 0;
            const slotY = slotIdx * (cardH + cardGap) + (stackSpan - cardH) / 2;

            // Approval flash for card 4
            let approvalFlash = 0;
            if (card.approval) {
              approvalFlash = interpolate(
                localFrame,
                [enter + 6, enter + 26, enter + 50],
                [0, 1, 0.15],
                {
                  extrapolateLeft: "clamp",
                  extrapolateRight: "clamp",
                  easing: easeOut,
                },
              );
            }

            const opacity = interpolate(
              localFrame,
              [enter, enter + 12],
              [0, 1],
              {
                extrapolateLeft: "clamp",
                extrapolateRight: "clamp",
              },
            );

            return (
              <div
                key={card.cmd}
                style={{
                  position: "absolute",
                  left: slotX,
                  top: slotY,
                  width: cardW,
                  height: cardH,
                  transform: `translate(${offX}px, ${offY}px)`,
                  opacity,
                  // Top-right notch via clip-path (4px cosmetic corner cut)
                  clipPath:
                    "polygon(0 0, calc(100% - 12px) 0, 100% 12px, 100% 100%, 0 100%)",
                  background: pal.navy,
                  border: `3px solid ${pal.bright}`,
                  boxShadow: card.approval
                    ? `0 0 ${24 + approvalFlash * 36}px rgba(15,232,240,${0.3 + approvalFlash * 0.5})`
                    : `0 0 12px rgba(90,136,247,0.25)`,
                  display: "flex",
                  alignItems: "center",
                }}
              >
                {/* Left indicator stripe */}
                <div
                  style={{
                    position: "absolute",
                    left: 0,
                    top: 0,
                    bottom: 0,
                    width: 8,
                    background: pal.cyan,
                    boxShadow: `0 0 12px ${pal.cyanGlow}`,
                  }}
                />

                {/* Card content */}
                <div
                  style={{
                    paddingLeft: 28,
                    paddingRight: 24,
                    fontFamily: fonts.mono,
                    color: pal.highlight,
                    width: "100%",
                  }}
                >
                  <div
                    style={{
                      fontSize: 22,
                      letterSpacing: "0.04em",
                      color: card.approval ? pal.cyan : pal.highlight,
                      fontWeight: 500,
                    }}
                  >
                    {card.cmd}
                  </div>
                  <div
                    style={{
                      fontSize: 16,
                      marginTop: 6,
                      color: pal.light,
                      opacity: 0.85,
                      letterSpacing: "0.02em",
                    }}
                  >
                    {card.sub}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </AbsoluteFill>

      {/* Approval checkmark stamp — appears when card 4 enters */}
      {(() => {
        const approvalEnter = ENTER_FRAMES[3];
        const stampSpring = spring({
          frame: localFrame - (approvalEnter + 6),
          fps,
          config: { damping: 9, stiffness: 200, mass: 0.6 },
        });
        if (stampSpring <= 0) return null;

        const stampOpacity = interpolate(
          localFrame,
          [approvalEnter + 6, approvalEnter + 16],
          [0, 1],
          {
            extrapolateLeft: "clamp",
            extrapolateRight: "clamp",
          },
        );

        // Card 4 is the last in the vertical stack. For H the stamp sits to
        // the right; for V (where horizontal space is tight) it sits below.
        const slotIdx = 3 - (CARDS.length - 1) / 2;
        const stampX = aspect === "h"
          ? stackCenterX + cardW / 2 + 80
          : stackCenterX + cardW / 2 - 40;
        const stampY = aspect === "h"
          ? stackCenterY + slotIdx * (cardH + cardGap)
          : stackCenterY + slotIdx * (cardH + cardGap) + 90;

        return (
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
                position: "absolute",
                left: `calc(50% + ${stampX}px)`,
                top: `calc(50% + ${stampY}px)`,
                transform: `translate(-50%, -50%) scale(${stampSpring})`,
                opacity: stampOpacity,
              }}
            >
              {/* Pixel-art chunky checkmark built from rectangles */}
              <svg width={96} height={96} viewBox="0 0 16 16" shapeRendering="crispEdges">
                {/* Outer cyan ring/square for emphasis */}
                <rect x={0} y={0} width={16} height={16} fill="none" stroke={pal.cyan} strokeWidth={1} opacity={0.35} />
                {/* Diagonal stroke (going down) */}
                <rect x={2} y={7} width={2} height={2} fill={pal.cyan} />
                <rect x={3} y={8} width={2} height={2} fill={pal.cyan} />
                <rect x={4} y={9} width={2} height={2} fill={pal.cyan} />
                <rect x={5} y={10} width={2} height={2} fill={pal.cyan} />
                {/* Diagonal stroke (going up) */}
                <rect x={6} y={9} width={2} height={2} fill={pal.cyan} />
                <rect x={7} y={8} width={2} height={2} fill={pal.cyan} />
                <rect x={8} y={7} width={2} height={2} fill={pal.cyan} />
                <rect x={9} y={6} width={2} height={2} fill={pal.cyan} />
                <rect x={10} y={5} width={2} height={2} fill={pal.cyan} />
                <rect x={11} y={4} width={2} height={2} fill={pal.cyan} />
                <rect x={12} y={3} width={2} height={2} fill={pal.cyan} />
              </svg>
            </div>
          </AbsoluteFill>
        );
      })()}

      <WorldSign title={t.s9_title} sub={t.s9_sub} variant="billboard" y={pick(aspect, 340, 540)} x={pick(aspect, 200, 0)} scale={pick(aspect, 0.95, 0.8)} from={36} />
    </Stage>
  );
};
