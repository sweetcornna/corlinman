import { AbsoluteFill } from "remotion";
import { pal } from "./palette";
import { PixelGrid } from "./PixelGrid";

type Props = {
  children?: React.ReactNode;
  gridOpacity?: number;
  gridCell?: number;
  showGrid?: boolean;
  vignette?: number;
};

// Shared scene container — navy void + dot grid + soft vignette.
export const Stage: React.FC<Props> = ({
  children,
  gridOpacity = 0.18,
  gridCell = 24,
  showGrid = true,
  vignette = 0.55,
}) => {
  return (
    <AbsoluteFill style={{ backgroundColor: pal.void }}>
      <AbsoluteFill
        style={{
          background: `radial-gradient(ellipse at 50% 50%, ${pal.navy} 0%, ${pal.void} 70%, #02050C 100%)`,
        }}
      />
      {showGrid && <PixelGrid cell={gridCell} opacity={gridOpacity} />}
      {children}
      <AbsoluteFill
        style={{
          pointerEvents: "none",
          background: `radial-gradient(ellipse at 50% 50%, transparent 35%, rgba(2,5,12,${vignette}) 100%)`,
        }}
      />
    </AbsoluteFill>
  );
};
