import { Img, staticFile, useCurrentFrame } from "remotion";
import { pal } from "./palette";
import { fonts } from "../tokens/typography";

type Props = {
  scale?: number;
  x?: number;
  y?: number;
  opacity?: number;
  glow?: number;
  reflect?: number;
  rotate?: number;
  blink?: boolean;
  breathe?: boolean;
  trail?: number;
  localFrame?: number;

  // === Narrative behaviors (new) ===
  // walking: bouncy vertical bob + walk-cycle horizontal shift suggestion.
  walking?: boolean;
  walkDir?: -1 | 0 | 1;        // -1 = leaning left, +1 = leaning right
  // jump: vertical Y bounce + squash-stretch on takeoff/landing.
  // Value is 0..1 phase of the jump arc.
  jumping?: number;
  // surprise: shows `!` glyph above head, with bounce-in scale.
  surprised?: number;          // 0..1
  // wave: small rotational oscillation around center (like a head shake).
  waving?: number;             // 0..1
  // mouthText: extends the `>_` mouth with typed text BELOW the sprite.
  mouthText?: string;
  mouthProgress?: number;      // 0..1 — typewriter progress through mouthText
  // lean: lean toward a direction. dx, dy in normalized [-1,1].
  leanX?: number;              // -1..1 (visible up to ~12deg tilt)
  leanY?: number;              // -1..1 (subtle perspective squish)
};

// Sprite renderer for public/mascot-cut.png with narrative behaviors.
// Native sprite: 480×500. We scale via CSS image-rendering: pixelated.
export const Mascot: React.FC<Props> = ({
  scale = 1,
  x = 0,
  y = 0,
  opacity = 1,
  glow = 0.6,
  reflect = 0,
  rotate = 0,
  blink = true,
  breathe = true,
  trail = 0,
  localFrame,
  walking = false,
  walkDir = 1,
  jumping = 0,
  surprised = 0,
  waving = 0,
  mouthText,
  mouthProgress = 1,
  leanX = 0,
  leanY = 0,
}) => {
  const cur = useCurrentFrame();
  const frame = localFrame ?? cur;

  // Breath: slow vertical sine ±4px (3s cycle)
  const breathBob = breathe ? Math.sin((frame / 90) * Math.PI * 2) * 4 : 0;
  // Walk-cycle: faster vertical bounce ±8px (8-frame cycle)
  const walkBob = walking ? Math.abs(Math.sin((frame / 4) * Math.PI)) * -8 : 0;
  // Jump arc: parabolic Y (0..1 → 0..-1..0 in altitude)
  const jumpY = jumping > 0 ? -Math.sin(jumping * Math.PI) * 200 : 0;
  // Squash on jump landing/takeoff
  const jumpSquashY = jumping > 0
    ? jumping < 0.15
      ? 1 + (jumping / 0.15) * 0.12        // takeoff squish
      : jumping > 0.85
      ? 1 + ((jumping - 0.85) / 0.15) * 0.15 // landing squish
      : 1
    : 1;

  // Blink — smooth eye-dip over 8 frames, every ~3.7s. Subtle so it
  // never looks like the whole body squashes (that was rubber/glitchy).
  let blinkSquishY = 1;
  if (blink) {
    const phase = frame % 110;
    if (phase >= 96 && phase < 104) {
      const t = (phase - 96) / 8;
      blinkSquishY = 1 - 0.07 * Math.sin(t * Math.PI);
    }
  }

  // Walk lean: tilt rotation based on direction
  const walkLean = walking ? walkDir * 4 : 0;
  // Manual lean overlay
  const totalRotate = rotate + walkLean + leanX * 8 + waving * Math.sin(frame / 5) * 5;

  // Trail
  const trails: number[] = [];
  if (trail > 0) {
    for (let i = 1; i <= 4; i++) trails.push(i);
  }

  const size = 480 * scale;

  const halo = glow > 0 && (
    <div
      style={{
        position: "absolute",
        left: -size * 0.4,
        top: -size * 0.4,
        width: size * 1.8,
        height: size * 1.8,
        background: `radial-gradient(circle, ${pal.cyanGlow} 0%, rgba(31,79,184,${glow * 0.4}) 25%, transparent 60%)`,
        filter: `blur(${size * 0.04}px)`,
        opacity: glow,
        pointerEvents: "none",
      }}
    />
  );

  // Jump squash anchors at bottom (feet stay planted). Blink + leanY are a
  // separate centered transform on a nested wrapper so they don't fight the
  // jump origin.
  const jumpScaleX = 2 - jumpSquashY;
  const innerScaleY = blinkSquishY * (1 + leanY * 0.05);
  const sprite = (
    <div
      style={{
        width: size,
        transform: `scale(${jumpScaleX}, ${jumpSquashY})`,
        transformOrigin: "center bottom",
      }}
    >
      <Img
        src={staticFile("mascot-cut.png")}
        style={{
          width: size,
          height: "auto",
          imageRendering: "pixelated",
          transform: `scaleY(${innerScaleY})`,
          transformOrigin: "center",
          filter: "contrast(1.05) saturate(1.05)",
          display: "block",
        }}
      />
    </div>
  );

  const reflectImg = reflect > 0 && (
    <Img
      src={staticFile("mascot-cut.png")}
      style={{
        position: "absolute",
        top: size * 0.95,
        left: 0,
        width: size,
        height: "auto",
        imageRendering: "pixelated",
        transform: `scaleY(-${blinkSquishY})`,
        transformOrigin: "top center",
        opacity: reflect * 0.45,
        filter: `blur(${size * 0.005}px) contrast(0.8) brightness(0.6)`,
        maskImage:
          "linear-gradient(to bottom, rgba(0,0,0,1) 0%, rgba(0,0,0,0.4) 30%, transparent 75%)",
        WebkitMaskImage:
          "linear-gradient(to bottom, rgba(0,0,0,1) 0%, rgba(0,0,0,0.4) 30%, transparent 75%)",
        pointerEvents: "none",
      }}
    />
  );

  // Surprise: `!` glyph above head, bouncy entry
  const surpriseEl = surprised > 0 && (
    <div
      style={{
        position: "absolute",
        left: size * 0.65,
        top: -size * 0.25,
        fontFamily: fonts.mono,
        fontWeight: 800,
        fontSize: size * 0.32,
        color: pal.cyan,
        textShadow: `0 0 ${size * 0.05}px ${pal.cyan}, 0 ${size * 0.01}px 0 ${pal.deep}`,
        opacity: surprised,
        transform: `scale(${0.5 + 0.5 * surprised}) translateY(${(1 - surprised) * 20}px)`,
        transformOrigin: "center bottom",
        pointerEvents: "none",
      }}
    >
      !
    </div>
  );

  // Mouth-extend: render `>_ text...` BELOW the sprite, where the mascot's
  // terminal-prompt mouth sits. The text appears as if typed by the mascot.
  const visibleChars = mouthText
    ? Math.floor(mouthText.length * Math.max(0, Math.min(1, mouthProgress)))
    : 0;
  const visibleText = mouthText?.slice(0, visibleChars) ?? "";
  const cursorBlink = Math.floor(frame / 8) % 2 === 0 ? "▌" : " ";
  const mouthEl = mouthText && (
    <div
      style={{
        position: "absolute",
        left: size * 0.6,
        top: size * 0.65,
        fontFamily: fonts.mono,
        fontWeight: 500,
        fontSize: size * 0.075,
        color: pal.cyan,
        whiteSpace: "nowrap",
        letterSpacing: "0.05em",
        textShadow: `0 0 ${size * 0.04}px ${pal.cyanGlow}`,
        pointerEvents: "none",
      }}
    >
      {visibleText}
      {visibleChars < (mouthText?.length ?? 0) ? cursorBlink : ""}
    </div>
  );

  return (
    <div
      style={{
        position: "absolute",
        left: `calc(50% + ${x}px - ${size / 2}px)`,
        top: `calc(50% + ${y + breathBob + walkBob + jumpY}px - ${size / 2}px)`,
        width: size,
        height: size * (500 / 480),
        opacity,
        transform: `rotate(${totalRotate}deg)`,
        transformOrigin: "center",
      }}
    >
      {halo}
      {trails.map((i) => {
        const tOp = trail * (1 - i / 5);
        return (
          <Img
            key={`t${i}`}
            src={staticFile("mascot-cut.png")}
            style={{
              position: "absolute",
              left: -i * size * 0.08,
              top: 0,
              width: size,
              height: "auto",
              imageRendering: "pixelated",
              opacity: tOp,
              filter: `blur(${i * 1.5}px)`,
              transform: `scaleY(${blinkSquishY})`,
              pointerEvents: "none",
            }}
          />
        );
      })}
      {sprite}
      {reflectImg}
      {surpriseEl}
      {mouthEl}
    </div>
  );
};
