import { Easing, interpolate, useCurrentFrame } from "remotion";
import { pal } from "./palette";
import { fonts } from "../tokens/typography";

type Variant = "engraved" | "neon" | "billboard" | "tag" | "banner";
type Anchor = "tl" | "tr" | "bl" | "br" | "center";

type Props = {
  title: string;
  sub?: string;
  variant?: Variant;
  // px offset from composition center
  x?: number;
  y?: number;
  scale?: number;
  // entry frame (local) and visibility 0..1
  from?: number;
  visible?: number;
  // rotation for tilted signs
  rotate?: number;
  // anchor (only relevant for billboard which has a backing rect)
  anchor?: Anchor;
};

const easeOut = Easing.bezier(0.16, 1, 0.3, 1);

// World-embedded label. NOT a corner caption — this is a pixel-art SIGN
// that lives IN the scene. The mascot walks past it / interacts with it.
//
// Variants:
//   engraved   — looks etched into stone (subtle sapphire on dark plate)
//   neon       — glowing cyan tube text
//   billboard  — pixel-art rectangle with text inside, casts a shadow
//   tag        — small pinned tag with a string going up to an object
//   banner     — wide horizontal banner with cyan accent stripes
export const WorldSign: React.FC<Props> = ({
  title,
  sub,
  variant = "billboard",
  x = 0,
  y = 0,
  scale = 1,
  from = 0,
  visible = 1,
  rotate = 0,
  anchor: _anchor = "center",
}) => {
  const frame = useCurrentFrame();
  const entryT = interpolate(frame - from, [0, 24], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: easeOut,
  });
  const opacity = entryT * visible;
  const liftPx = (1 - entryT) * 12;

  const baseFontSize = 38 * scale;
  const subFontSize = 16 * scale;

  if (variant === "neon") {
    return (
      <div
        style={{
          position: "absolute",
          left: `calc(50% + ${x}px)`,
          top: `calc(50% + ${y + liftPx}px)`,
          transform: `translate(-50%, -50%) rotate(${rotate}deg)`,
          opacity,
          pointerEvents: "none",
        }}
      >
        <div
          style={{
            fontFamily: fonts.mono,
            fontSize: baseFontSize,
            fontWeight: 700,
            color: pal.cyan,
            letterSpacing: "0.16em",
            textShadow: `0 0 ${10 * scale}px ${pal.cyan}, 0 0 ${24 * scale}px ${pal.cyanGlow}, 0 0 ${48 * scale}px ${pal.cyanGlow}`,
            textTransform: "uppercase",
          }}
        >
          {title}
        </div>
        {sub && (
          <div
            style={{
              fontFamily: fonts.mono,
              fontSize: subFontSize,
              color: pal.light,
              letterSpacing: "0.32em",
              textAlign: "center",
              marginTop: 6 * scale,
              textTransform: "uppercase",
              opacity: 0.85,
            }}
          >
            {sub}
          </div>
        )}
      </div>
    );
  }

  if (variant === "engraved") {
    return (
      <div
        style={{
          position: "absolute",
          left: `calc(50% + ${x}px)`,
          top: `calc(50% + ${y + liftPx}px)`,
          transform: `translate(-50%, -50%) rotate(${rotate}deg)`,
          opacity,
          padding: `${10 * scale}px ${28 * scale}px`,
          background: `linear-gradient(to bottom, ${pal.navyDeep}, ${pal.navy})`,
          border: `${2 * scale}px solid ${pal.deep}`,
          boxShadow: `inset 0 ${2 * scale}px ${6 * scale}px rgba(0,0,0,0.6), 0 ${2 * scale}px 0 ${pal.deep}`,
          textAlign: "center",
          pointerEvents: "none",
        }}
      >
        <div
          style={{
            fontFamily: fonts.mono,
            fontSize: baseFontSize * 0.75,
            fontWeight: 600,
            color: pal.light,
            letterSpacing: "0.22em",
            textTransform: "uppercase",
            textShadow: `0 ${1 * scale}px 0 ${pal.navyDeep}, 0 -${1 * scale}px 0 ${pal.shadow}`,
          }}
        >
          {title}
        </div>
        {sub && (
          <div
            style={{
              fontFamily: fonts.mono,
              fontSize: subFontSize * 0.9,
              color: pal.mid,
              letterSpacing: "0.3em",
              textTransform: "uppercase",
              marginTop: 4 * scale,
            }}
          >
            {sub}
          </div>
        )}
      </div>
    );
  }

  if (variant === "tag") {
    // A small "luggage tag" pinned via a string
    return (
      <div
        style={{
          position: "absolute",
          left: `calc(50% + ${x}px)`,
          top: `calc(50% + ${y + liftPx}px)`,
          transform: `translate(-50%, 0) rotate(${rotate}deg)`,
          opacity,
          pointerEvents: "none",
        }}
      >
        {/* string */}
        <div
          style={{
            width: 2 * scale,
            height: 40 * scale,
            background: pal.cyan,
            margin: "0 auto",
            opacity: 0.6,
          }}
        />
        {/* tag body */}
        <div
          style={{
            background: pal.navy,
            border: `${3 * scale}px solid ${pal.cyan}`,
            padding: `${6 * scale}px ${16 * scale}px`,
            fontFamily: fonts.mono,
            fontSize: baseFontSize * 0.55,
            color: pal.cyan,
            letterSpacing: "0.2em",
            textTransform: "uppercase",
            boxShadow: `0 0 ${10 * scale}px ${pal.cyanGlow}`,
            textShadow: `0 0 ${6 * scale}px ${pal.cyanGlow}`,
          }}
        >
          {title}
          {sub && (
            <div
              style={{
                fontSize: subFontSize * 0.8,
                color: pal.light,
                marginTop: 2 * scale,
                letterSpacing: "0.16em",
              }}
            >
              {sub}
            </div>
          )}
        </div>
      </div>
    );
  }

  if (variant === "banner") {
    // Wide horizontal banner
    return (
      <div
        style={{
          position: "absolute",
          left: `calc(50% + ${x}px)`,
          top: `calc(50% + ${y + liftPx}px)`,
          transform: `translate(-50%, -50%) rotate(${rotate}deg)`,
          opacity,
          display: "flex",
          alignItems: "center",
          gap: 18 * scale,
          padding: `${10 * scale}px ${32 * scale}px`,
          background: `linear-gradient(to right, transparent, ${pal.navy}, ${pal.navy}, transparent)`,
          borderTop: `${2 * scale}px solid ${pal.bright}`,
          borderBottom: `${2 * scale}px solid ${pal.bright}`,
          pointerEvents: "none",
        }}
      >
        <div style={{ width: 12 * scale, height: 12 * scale, background: pal.cyan, boxShadow: `0 0 ${10 * scale}px ${pal.cyan}` }} />
        <div
          style={{
            fontFamily: fonts.mono,
            fontSize: baseFontSize,
            fontWeight: 700,
            color: pal.ivory,
            letterSpacing: "0.28em",
            textTransform: "uppercase",
          }}
        >
          {title}
        </div>
        {sub && (
          <div
            style={{
              fontFamily: fonts.mono,
              fontSize: subFontSize,
              color: pal.cyan,
              letterSpacing: "0.22em",
              textTransform: "uppercase",
            }}
          >
            {sub}
          </div>
        )}
        <div style={{ width: 12 * scale, height: 12 * scale, background: pal.cyan, boxShadow: `0 0 ${10 * scale}px ${pal.cyan}` }} />
      </div>
    );
  }

  // billboard (default) — pixel-art rectangle with text
  return (
    <div
      style={{
        position: "absolute",
        left: `calc(50% + ${x}px)`,
        top: `calc(50% + ${y + liftPx}px)`,
        transform: `translate(-50%, -50%) rotate(${rotate}deg)`,
        opacity,
        pointerEvents: "none",
      }}
    >
      <div
        style={{
          background: pal.navyDeep,
          border: `${3 * scale}px solid ${pal.bright}`,
          padding: `${14 * scale}px ${24 * scale}px ${14 * scale}px ${28 * scale}px`,
          position: "relative",
          minWidth: 180 * scale,
          // left side colored stripe like a tab
          boxShadow: `inset ${8 * scale}px 0 0 ${pal.cyan}, 0 ${4 * scale}px 0 ${pal.shadow}`,
        }}
      >
        <div
          style={{
            fontFamily: fonts.mono,
            fontSize: baseFontSize,
            fontWeight: 600,
            color: pal.ivory,
            letterSpacing: "0.16em",
            textTransform: "uppercase",
            lineHeight: 1,
          }}
        >
          {title}
        </div>
        {sub && (
          <div
            style={{
              fontFamily: fonts.mono,
              fontSize: subFontSize,
              color: pal.cyan,
              letterSpacing: "0.22em",
              textTransform: "uppercase",
              marginTop: 6 * scale,
              opacity: 0.85,
            }}
          >
            {sub}
          </div>
        )}
      </div>
    </div>
  );
};
