import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { colors } from "../tokens/colors";
import { fonts } from "../tokens/typography";
import { springs } from "../tokens/motion";

type Props = {
  // entry frame (when wordmark begins to appear)
  from?: number;
  // base font size in px (at composition size; scale via wrapper as needed)
  size?: number;
  mode?: "day" | "night";
};

// Variant C: Serif + Sapphire Pulse + italic tail "n".
// Locked as the corlinman hero mark per W1 user decision.
export const Wordmark: React.FC<Props> = ({ from = 0, size = 160, mode = "night" }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const localFrame = Math.max(0, frame - from);

  // Letters fade + spring in one-by-one
  const letters = "corli".split("");
  const tail = "nma".split("");

  const baseStyle: React.CSSProperties = {
    fontFamily: fonts.serif,
    fontWeight: 400,
    fontSize: size,
    lineHeight: 1,
    letterSpacing: "-0.015em",
    color: mode === "night" ? colors.ivory : colors.inkDay,
    textShadow:
      mode === "night"
        ? "0 0 30px rgba(130,168,232,0.45), 0 0 80px rgba(31,79,184,0.3)"
        : "none",
    display: "inline-flex",
    alignItems: "baseline",
  };

  const pulseSize = size * 0.18;
  const pulseColor = mode === "night" ? colors.sapphireLight : colors.sapphire;

  // Pulse appears between corli and nma — animate scale + glow breathing
  const pulseAppear = spring({
    frame: localFrame - 25,
    fps,
    config: springs.firm,
  });
  const pulseBreath = interpolate(
    (localFrame % 60) / 60,
    [0, 0.5, 1],
    [0.85, 1.15, 0.85]
  );

  const italicColor = mode === "night" ? colors.sapphireLight : colors.sapphire;

  return (
    <div style={baseStyle}>
      {letters.map((ch, i) => {
        const appear = spring({
          frame: localFrame - i * 3,
          fps,
          config: springs.firm,
        });
        return (
          <span
            key={`a${i}`}
            style={{ opacity: appear, transform: `translateY(${(1 - appear) * 8}px)` }}
          >
            {ch}
          </span>
        );
      })}
      <span
        style={{
          display: "inline-block",
          width: pulseSize,
          height: pulseSize,
          borderRadius: "50%",
          backgroundColor: pulseColor,
          margin: `0 ${size * 0.05}px 0 ${size * 0.1}px`,
          transform: `translateY(-${size * 0.12}px) scale(${pulseAppear * pulseBreath})`,
          opacity: pulseAppear,
          boxShadow: `0 0 ${size * 0.15}px ${size * 0.04}px ${pulseColor}66`,
        }}
      />
      {tail.map((ch, i) => {
        const appear = spring({
          frame: localFrame - 18 - i * 3,
          fps,
          config: springs.firm,
        });
        return (
          <span
            key={`b${i}`}
            style={{ opacity: appear, transform: `translateY(${(1 - appear) * 8}px)` }}
          >
            {ch}
          </span>
        );
      })}
      {/* italic tail 'n' */}
      <span
        style={{
          fontStyle: "italic",
          color: italicColor,
          marginLeft: "-0.02em",
          opacity: spring({
            frame: localFrame - 30,
            fps,
            config: springs.firm,
          }),
          transform: `translateY(${
            (1 -
              spring({
                frame: localFrame - 30,
                fps,
                config: springs.firm,
              })) *
            8
          }px)`,
        }}
      >
        n
      </span>
    </div>
  );
};
