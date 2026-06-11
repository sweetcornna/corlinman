import { AbsoluteFill, useCurrentFrame } from "remotion";

type Props = {
  opacity?: number;
};

// SVG fractal-noise grain. Reseeds every 2 frames for animated grain ("filmic shake").
export const GrainOverlay: React.FC<Props> = ({ opacity = 0.4 }) => {
  const frame = useCurrentFrame();
  const seed = Math.floor(frame / 2);
  const noiseUrl =
    `data:image/svg+xml;utf8,` +
    encodeURIComponent(
      `<svg xmlns='http://www.w3.org/2000/svg' width='400' height='400'>` +
        `<filter id='n'>` +
        `<feTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch' seed='${seed}'/>` +
        `<feColorMatrix values='0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 0.35 0'/>` +
        `</filter>` +
        `<rect width='100%' height='100%' filter='url(#n)'/>` +
        `</svg>`
    );

  return (
    <AbsoluteFill
      style={{
        backgroundImage: `url("${noiseUrl}")`,
        backgroundSize: "400px 400px",
        mixBlendMode: "overlay",
        opacity,
        pointerEvents: "none",
      }}
    />
  );
};
