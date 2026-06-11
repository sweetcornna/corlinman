import { Easing, interpolate, useCurrentFrame } from "remotion";
import { fonts } from "../tokens/typography";
import { pal } from "./palette";

type Props = {
  // label text — monospaced, all-caps, tiny corner marker
  label?: string;
  // optional metric (e.g. "0.91" or "6/6")
  metric?: string;
  // corner placement
  corner?: "tl" | "tr" | "bl" | "br";
  // localFrame for entry; 0..30 fade-in window
  from?: number;
  // 0..1 manual opacity multiplier (so PixelFilm can crossfade)
  visible?: number;
};

const easeOut = Easing.bezier(0.16, 1, 0.3, 1);

// Tiny corner marker for each scene — terminal-style label + optional metric.
// Inspired by image #4's "♪ Doors  Painterly -" top corner indicator.
export const Caption: React.FC<Props> = ({
  label,
  metric,
  corner = "tl",
  from = 0,
  visible = 1,
}) => {
  const frame = useCurrentFrame();
  const op =
    interpolate(frame - from, [0, 16], [0, 1], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
      easing: easeOut,
    }) * visible;

  const pos: React.CSSProperties = {
    position: "absolute",
    fontFamily: fonts.mono,
    fontSize: 18,
    letterSpacing: "0.32em",
    textTransform: "uppercase",
    color: pal.light,
    display: "flex",
    alignItems: "center",
    gap: 16,
    opacity: op,
    pointerEvents: "none",
  };
  if (corner === "tl") Object.assign(pos, { top: 56, left: 72 });
  if (corner === "tr") Object.assign(pos, { top: 56, right: 72 });
  if (corner === "bl") Object.assign(pos, { bottom: 56, left: 72 });
  if (corner === "br") Object.assign(pos, { bottom: 56, right: 72 });

  return (
    <div style={pos}>
      <span style={{ width: 10, height: 10, background: pal.cyan, boxShadow: `0 0 10px ${pal.cyan}` }} />
      {label && <span>{label}</span>}
      {metric && (
        <span style={{ color: pal.cyan, fontWeight: 500, letterSpacing: "0.2em" }}>
          {metric}
        </span>
      )}
    </div>
  );
};
