import React from "react";
import { AbsoluteFill, Easing, interpolate } from "remotion";
import { Stage } from "../Stage";
import { WorldSign } from "../WorldSign";
import { Mascot } from "../Mascot";
import { MascotPixels } from "../MascotPixels";
import { pal } from "../palette";
import { useAspect, pick } from "../../tokens/aspect";
import { useCopy } from "../i18n";

const easeOut = Easing.bezier(0.16, 1, 0.3, 1);

export const Scene02_Assemble: React.FC<{ localFrame: number; aspect: "h" | "v" }> = ({
  localFrame,
  aspect: _aspect,
}) => {
  const aspect = useAspect();
  const t = useCopy();
  const cellSize = pick(aspect, 22, 28);

  const assemble = interpolate(localFrame, [0, 110], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: easeOut,
  });

  // Hand-off from procedural pixels to the polished sprite once assembly completes,
  // so the breathing/blink kicks in without a visible swap.
  const handoff = interpolate(localFrame, [110, 130], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: easeOut,
  });

  // Shockwave fires the moment the last pixel snaps home.
  const shockStart = 100;
  const shockProgress = interpolate(localFrame, [shockStart, shockStart + 40], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: easeOut,
  });
  const shockRadius = shockProgress * pick(aspect, 720, 540);
  const shockOpacity = (1 - shockProgress) * 0.65;

  return (
    <Stage
      showGrid
      gridCell={pick(aspect, 24, 22)}
      gridOpacity={interpolate(localFrame, [0, 60], [0.08, 0.2], {
        extrapolateLeft: "clamp",
        extrapolateRight: "clamp",
      })}
      vignette={0.55}
    >
      <AbsoluteFill
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        {shockProgress > 0 && shockProgress < 1 && (
          <div
            style={{
              position: "absolute",
              width: shockRadius * 2,
              height: shockRadius * 2,
              borderRadius: "50%",
              border: `2px solid ${pal.cyan}`,
              opacity: shockOpacity,
              boxShadow: `0 0 ${cellSize * 2}px ${pal.cyanGlow}, inset 0 0 ${cellSize * 1.6}px ${pal.cyanGlow}`,
              pointerEvents: "none",
            }}
          />
        )}
      </AbsoluteFill>

      <div style={{ opacity: 1 - handoff }}>
        <MascotPixels
          cellSize={cellSize}
          assemble={assemble}
          stagger={45}
          jitter={4}
        />
      </div>

      {/* Mascot handoff now owned by PersistentMascot at the film level */}

      <WorldSign title={t.s2_title} sub={t.s2_sub} variant="banner" y={320} scale={pick(aspect, 1.0, 0.85)} from={42} />
    </Stage>
  );
};
