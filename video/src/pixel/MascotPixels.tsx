import { interpolate, useCurrentFrame, Easing } from "remotion";
import gridData from "./mascotGrid.json";

type Cell = { x: number; y: number; c: string };
type Props = {
  // pixel size of each grid cell on screen (e.g. 18-26 for hero-size mascot)
  cellSize?: number;
  // overall opacity
  opacity?: number;
  // assembly progress 0..1 — 0 = fully scattered, 1 = assembled.
  // Scattered pixels start from random positions in a 800px radius,
  // shrinking and rotating in toward their assembled positions.
  assemble?: number;
  // scatter progress 0..1 — reverse: 0 = assembled, 1 = scattered (dissolve away).
  scatter?: number;
  // center anchor offsets (px from composition center)
  x?: number;
  y?: number;
  // per-cell stagger frames — larger = waves of pixels assemble at different times
  stagger?: number;
  // optional seeded jitter (px) for individual cells
  jitter?: number;
};

const data = gridData as { w: number; h: number; cells: Cell[] };
const W = data.w;
const H = data.h;

// Deterministic pseudo-random for a given (x,y) — gives each pixel a stable start position
function hash(x: number, y: number, salt = 0): number {
  let n = (x * 73856093) ^ (y * 19349663) ^ (salt * 83492791);
  n = (n ^ (n >> 13)) * 1274126177;
  n = n ^ (n >> 16);
  return (n & 0x7fffffff) / 0x7fffffff;
}

// Procedural pixel-grid renderer of the mascot.
// Each non-empty cell is a div. Drives assembly / scatter / dissolve effects.
export const MascotPixels: React.FC<Props> = ({
  cellSize = 24,
  opacity = 1,
  assemble,
  scatter,
  x = 0,
  y = 0,
  stagger = 0,
  jitter = 0,
}) => {
  const frame = useCurrentFrame();
  const easeOut = Easing.bezier(0.16, 1, 0.3, 1);

  const spriteW = W * cellSize;
  const spriteH = H * cellSize;

  return (
    <div
      style={{
        position: "absolute",
        left: `calc(50% + ${x}px - ${spriteW / 2}px)`,
        top: `calc(50% + ${y}px - ${spriteH / 2}px)`,
        width: spriteW,
        height: spriteH,
        opacity,
      }}
    >
      {data.cells.map((c) => {
        const rx = hash(c.x, c.y, 1);
        const ry = hash(c.x, c.y, 2);
        const rz = hash(c.x, c.y, 3);
        // start position relative to assembled center: a vector pointing outward
        const ang = rx * Math.PI * 2;
        const dist = 400 + rz * 600;
        const sx = Math.cos(ang) * dist;
        const sy = Math.sin(ang) * dist;
        const jx = (hash(c.x, c.y, 4) - 0.5) * jitter;
        const jy = (hash(c.x, c.y, 5) - 0.5) * jitter;

        // per-cell delay for stagger (in frames)
        const cellOrder = (c.x + c.y) / (W + H); // 0..1, diagonal sweep
        const delay = cellOrder * stagger;
        const localFrame = Math.max(0, frame - delay);
        const localStretch = stagger > 0 ? interpolate(localFrame, [0, 30], [0, 1], { extrapolateRight: "clamp" }) : 1;

        // compute t (0..1) based on assemble or scatter prop
        let t = 1;
        if (assemble !== undefined) t = assemble * localStretch;
        else if (scatter !== undefined) t = 1 - scatter * localStretch;

        const eased = easeOut(t);

        const ox = sx * (1 - eased);
        const oy = sy * (1 - eased);
        const op = eased < 0.05 ? eased * 20 : 1; // quick fade-in once close
        const rot = (1 - eased) * (rx > 0.5 ? 180 : -180);
        const sc = 0.4 + 0.6 * eased;

        return (
          <div
            key={`${c.x}-${c.y}`}
            style={{
              position: "absolute",
              left: c.x * cellSize + ox + jx,
              top: c.y * cellSize + oy + jy,
              width: cellSize,
              height: cellSize,
              backgroundColor: c.c,
              opacity: op,
              transform: `rotate(${rot}deg) scale(${sc})`,
              transformOrigin: "center",
              boxShadow: c.c === "#0FE8F0" ? `0 0 ${cellSize * 0.6}px ${c.c}` : undefined,
            }}
          />
        );
      })}
    </div>
  );
};
