import React from "react";
import { AbsoluteFill, Easing, interpolate } from "remotion";
import { Stage } from "../Stage";
import { WorldSign } from "../WorldSign";
import { Mascot } from "../Mascot";
import { Wordmark } from "../../primitives/Wordmark";
import { pal } from "../palette";
import { useAspect, pick } from "../../tokens/aspect";
import { fonts } from "../../tokens/typography";
import { useCopy } from "../i18n";

const easeOut = Easing.bezier(0.16, 1, 0.3, 1);

const URL = "github.com/corlinman/corlinman";

export const Scene12_Wordmark: React.FC<{ localFrame: number; aspect: "h" | "v" }> = ({
  localFrame,
  aspect: _aspect,
}) => {
  const aspect = useAspect();
  const t = useCopy();

  // Tagline reveal — clipPath sweep left-to-right, frames 60..96.
  const taglineReveal = interpolate(localFrame, [60, 96], [0, 100], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: easeOut,
  });
  const taglineOp = interpolate(localFrame, [60, 72], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: easeOut,
  });

  // URL line — fades in around frame 90.
  const urlOp = interpolate(localFrame, [90, 110], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: easeOut,
  });

  // Final ping pulse around frame 120 — single cyan ring expands and fades.
  const pingRadius = interpolate(localFrame, [120, 150], [0, pick(aspect, 800, 700)], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: easeOut,
  });
  const pingOp = interpolate(localFrame, [120, 138, 150], [0.85, 0.3, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: easeOut,
  });

  // End-of-film white flash punch — last ~20 frames (130..150).
  const endFlash = interpolate(
    localFrame,
    [130, 140, 150],
    [0, 0.15, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: easeOut },
  );

  // Aspect-specific layout
  const isH = aspect === "h";

  // Mascot transform — fade up slightly on entry so it settles before wordmark.
  const mascotSettle = interpolate(localFrame, [0, 18], [12, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: easeOut,
  });

  // Wordmark wrapper position — for H, sits to the right of mascot; for V, sits below.
  const wordmarkSize = pick(aspect, 220, 180);

  // Tagline / URL container — sits below or beside the wordmark depending on aspect.
  // We position them via flex inside an AbsoluteFill so we never use the
  // banned top:50%/left:50%/translate(-50%,-50%) pattern.
  const taglineFontSize = pick(aspect, 24, 28);
  const urlFontSize = pick(aspect, 20, 22);

  return (
    <Stage showGrid gridCell={pick(aspect, 28, 24)} gridOpacity={0.1} vignette={0.55}>
      {/* Mascot owned by PersistentMascot */}

      {/* Wordmark + tagline + URL block. Centered via flex on AbsoluteFill. */}
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
            // For H: shift the whole text block to the right side so the mascot has room on the left.
            // For V: shift the whole block downward.
            transform: isH
              ? `translate(${260}px, 0px)`
              : `translate(0px, ${260}px)`,
            display: "flex",
            flexDirection: "column",
            alignItems: isH ? "flex-start" : "center",
            gap: 28,
          }}
        >
          {/* Wordmark itself — primitive handles its own letter-by-letter entry. */}
          <div style={{ lineHeight: 1 }}>
            <Wordmark from={15} size={wordmarkSize} mode="night" />
          </div>

          {/* Tagline — left-to-right clipPath reveal. */}
          <div
            style={{
              fontFamily: fonts.mono,
              fontSize: taglineFontSize,
              letterSpacing: "0.18em",
              textTransform: "uppercase",
              color: pal.light,
              opacity: taglineOp,
              clipPath: `inset(0 ${100 - taglineReveal}% 0 0)`,
              WebkitClipPath: `inset(0 ${100 - taglineReveal}% 0 0)`,
              whiteSpace: "nowrap",
              textShadow: `0 0 12px rgba(130,168,232,0.3)`,
            }}
          >
            {t.tagline}
          </div>

          {/* URL line with cyan chevron. */}
          <div
            style={{
              fontFamily: fonts.mono,
              fontSize: urlFontSize,
              letterSpacing: "0.12em",
              color: pal.cyan,
              opacity: urlOp,
              display: "flex",
              alignItems: "center",
              gap: 10,
              textShadow: `0 0 10px ${pal.cyanGlow}`,
            }}
          >
            <span style={{ color: pal.cyan, fontWeight: 500 }}>{">"}</span>
            <span>{URL}</span>
          </div>
        </div>
      </AbsoluteFill>

      {/* Final ping ring — emanates from mascot center. */}
      {pingOp > 0 && (
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
              // Match mascot's anchor so the ring emanates from it.
              transform: isH
                ? `translate(${-500}px, 0px)`
                : `translate(0px, ${-400}px)`,
            }}
          >
            <div
              style={{
                position: "absolute",
                left: -pingRadius,
                top: -pingRadius,
                width: pingRadius * 2,
                height: pingRadius * 2,
                borderRadius: "50%",
                border: `3px solid ${pal.cyan}`,
                opacity: pingOp,
                boxShadow: `0 0 40px ${pal.cyanGlow}, inset 0 0 40px ${pal.cyanGlow}`,
              }}
            />
          </div>
        </AbsoluteFill>
      )}

      {/* End-of-film white flash punch. */}
      {endFlash > 0 && (
        <AbsoluteFill
          style={{
            backgroundColor: pal.white,
            opacity: endFlash,
            pointerEvents: "none",
          }}
        />
      )}

      {/* Small in-world tag near the wordmark — keeps the systems-y feel. */}
      <WorldSign title={t.s12_title} variant="tag" x={pick(aspect, 600, 0)} y={pick(aspect, 380, 700)} scale={pick(aspect, 0.7, 0.6)} from={90} />
    </Stage>
  );
};
