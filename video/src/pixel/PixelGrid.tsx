import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { pal } from "./palette";

type Props = {
  cell?: number;
  dotSize?: number;
  opacity?: number;
  pulse?: boolean;
  origin?: { x: number; y: number };
  wave?: boolean;
};

// Dot-matrix background grid. The "fabric" the mascot lives on.
export const PixelGrid: React.FC<Props> = ({
  cell = 24,
  dotSize = 2,
  opacity = 0.18,
  pulse = true,
  origin,
  wave = false,
}) => {
  const frame = useCurrentFrame();
  const breath = pulse
    ? interpolate((frame % 100) / 100, [0, 0.5, 1], [0.85, 1.15, 0.85])
    : 1;

  const w = cell;
  const svg =
    `<svg xmlns='http://www.w3.org/2000/svg' width='${w}' height='${w}'>` +
    `<circle cx='${w / 2}' cy='${w / 2}' r='${dotSize / 2}' fill='${pal.bright}' />` +
    `</svg>`;
  const url = `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
  const waveRadius = wave ? interpolate(frame, [0, 90], [0, 2400]) : 0;

  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
      <AbsoluteFill
        style={{
          backgroundImage: `url("${url}")`,
          backgroundRepeat: "repeat",
          backgroundSize: `${cell}px ${cell}px`,
          opacity: opacity * breath,
        }}
      />
      {wave && origin && (
        <AbsoluteFill
          style={{
            background: `radial-gradient(circle at ${origin.x * 100}% ${origin.y * 100}%,
              rgba(15,232,240,0.6) 0px,
              rgba(15,232,240,0.3) ${waveRadius - 80}px,
              transparent ${waveRadius}px)`,
            mixBlendMode: "screen",
            opacity: 0.7,
          }}
        />
      )}
    </AbsoluteFill>
  );
};
