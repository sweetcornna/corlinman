import { Easing, interpolate, useCurrentFrame } from "remotion";
import { Mascot } from "./Mascot";
import { mascotStateAt } from "./MascotState";
import { useAspect } from "../tokens/aspect";
import { useCopy } from "./i18n";

const easeOut = Easing.bezier(0.16, 1, 0.3, 1);
const easeInOut = Easing.bezier(0.4, 0, 0.2, 1);

// Narrative action layer on top of the base mascotStateAt() positions.
// Returns extra behaviors that bring the mascot alive: walking between zones,
// leaning when interacting, surprise pop, mouth-typing, waving.
type MouthCopy = { stream: string; tool: string; approve: string };

function actionAt(frame: number, mouth: MouthCopy): {
  walking: boolean;
  walkDir: -1 | 0 | 1;
  jumping: number;
  surprised: number;
  waving: number;
  mouthText?: string;
  mouthProgress: number;
  leanX: number;
  leanY: number;
} {
  // -------- Beat 1 — Boot/Genesis (0..150) --------
  // Mascot not visible yet; spark forming.

  // -------- Beat 2 — Assemble (150..300) --------
  // Pixels swarming. Mascot materialises ~frame 240.

  // -------- Beat 3 — Hero / Recognition (300..450) --------
  // Mascot center, looks around (subtle look L/R via leanX oscillation).
  if (frame >= 300 && frame < 450) {
    const t = (frame - 300) / 150;
    return {
      walking: false,
      walkDir: 0,
      jumping: 0,
      surprised: 0,
      waving: 0,
      mouthProgress: 0,
      // gentle "looking around" left-right scan
      leanX: Math.sin(t * Math.PI * 2) * 0.4,
      leanY: 0,
    };
  }

  // -------- Beat 4 — Dash / streaming (450..600) --------
  // Mascot leans forward (walkDir=1) with strong trail; mouth types "stream..."
  if (frame >= 450 && frame < 600) {
    const t = (frame - 450) / 150;
    return {
      walking: true,
      walkDir: 1,
      jumping: 0,
      surprised: 0,
      waving: 0,
      mouthText: mouth.stream,
      mouthProgress: interpolate(t, [0.1, 0.6], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" }),
      leanX: 0.5,
      leanY: 0,
    };
  }

  // -------- Beat 5 — Orbit / providers (600..750) --------
  // Mascot rotates SLOWLY (faces toward each provider in turn).
  if (frame >= 600 && frame < 750) {
    const t = (frame - 600) / 150;
    return {
      walking: false,
      walkDir: 0,
      jumping: 0,
      surprised: 0,
      waving: 0,
      mouthProgress: 0,
      leanX: Math.sin(t * Math.PI * 3) * 0.6,
      leanY: 0,
    };
  }

  // -------- Beat 6 — Mesh / plugins (750..900) --------
  // Mascot leans up-right (looking at plugin nodes). Around frame 820,
  // briefly surprised as a plugin connects.
  if (frame >= 750 && frame < 900) {
    const surpriseT = frame >= 800 && frame < 850
      ? Math.sin(((frame - 800) / 50) * Math.PI)
      : 0;
    return {
      walking: false,
      walkDir: 0,
      jumping: 0,
      surprised: surpriseT,
      waving: 0,
      mouthProgress: 0,
      leanX: 0.3,
      leanY: -0.2,
    };
  }

  // -------- Beat 7 — Circuit / tools hot-swap (900..1050) --------
  // Mascot lean LEFT then RIGHT (watching packets travel) + types "tool.call".
  if (frame >= 900 && frame < 1050) {
    const t = (frame - 900) / 150;
    return {
      walking: false,
      walkDir: 0,
      jumping: 0,
      surprised: 0,
      waving: 0,
      mouthText: mouth.tool,
      mouthProgress: interpolate(t, [0.2, 0.7], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" }),
      leanX: Math.sin(t * Math.PI * 2) * 0.6,
      leanY: 0,
    };
  }

  // -------- Beat 8 — Swarm (1050..1200) --------
  // The PersistentMascot fades to 0 here (handled by MascotState).
  // Scene's own multiplied copies take over.

  // -------- Beat 9 — Cards / HITL (1200..1350) --------
  // Mascot is offset to the left. Looks UP-RIGHT at cards, surprised when
  // approval lands. Mouth types "approved".
  if (frame >= 1200 && frame < 1350) {
    const t = (frame - 1200) / 150;
    const surpriseT = frame >= 1280 && frame < 1310
      ? Math.sin(((frame - 1280) / 30) * Math.PI)
      : 0;
    return {
      walking: false,
      walkDir: 0,
      jumping: 0,
      surprised: surpriseT,
      waving: 0,
      mouthText: mouth.approve,
      mouthProgress: interpolate(t, [0.5, 0.85], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" }),
      leanX: 0.4,
      leanY: -0.3,
    };
  }

  // -------- Beat 10 — Tidepool reflect (1350..1500) --------
  // Hero pose w/ reflection. Slight slow lean for posing.
  if (frame >= 1350 && frame < 1500) {
    const t = (frame - 1350) / 150;
    return {
      walking: false,
      walkDir: 0,
      jumping: 0,
      surprised: 0,
      waving: 0,
      mouthProgress: 0,
      leanX: Math.sin(t * Math.PI) * 0.2,
      leanY: 0,
    };
  }

  // -------- Beat 11 — Frame / lock-on (1500..1650) --------
  // Mascot still, eyes scanning.
  if (frame >= 1500 && frame < 1650) {
    const t = (frame - 1500) / 150;
    return {
      walking: false,
      walkDir: 0,
      jumping: 0,
      surprised: 0,
      waving: 0,
      mouthProgress: 0,
      leanX: Math.sin(t * Math.PI * 4) * 0.15,
      leanY: 0,
    };
  }

  // -------- Beat 12 — Wordmark / wave (1650..1800) --------
  // Mascot WAVES (rotational oscillation).
  if (frame >= 1650) {
    const t = (frame - 1650) / 150;
    const waveT = interpolate(t, [0.0, 0.3, 0.7, 1.0], [0, 1, 1, 0], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
      easing: easeInOut,
    });
    return {
      walking: false,
      walkDir: 0,
      jumping: 0,
      surprised: 0,
      waving: waveT,
      mouthProgress: 0,
      leanX: 0,
      leanY: 0,
    };
  }

  // default (early frames, mascot not yet born)
  return {
    walking: false,
    walkDir: 0,
    jumping: 0,
    surprised: 0,
    waving: 0,
    mouthProgress: 0,
    leanX: 0,
    leanY: 0,
  };
}

// Single mascot, alive the whole 60 seconds. Position + state read from a
// keyframe path; behaviors layered on top.
export const PersistentMascot: React.FC = () => {
  const frame = useCurrentFrame();
  const aspect = useAspect();
  const copy = useCopy();
  const mouth: MouthCopy = {
    stream: copy.mouth_stream,
    tool: copy.mouth_tool,
    approve: copy.mouth_approve,
  };
  const s = mascotStateAt(frame, aspect);
  const a = actionAt(frame, mouth);

  if (s.opacity <= 0.001) return null;

  return (
    <Mascot
      scale={s.scale}
      x={s.x}
      y={s.y}
      opacity={s.opacity}
      glow={s.glow}
      trail={s.trail}
      reflect={s.reflect}
      rotate={s.rotate}
      localFrame={frame}
      blink
      breathe
      walking={a.walking}
      walkDir={a.walkDir}
      jumping={a.jumping}
      surprised={a.surprised}
      waving={a.waving}
      mouthText={a.mouthText}
      mouthProgress={a.mouthProgress}
      leanX={a.leanX}
      leanY={a.leanY}
    />
  );
};
