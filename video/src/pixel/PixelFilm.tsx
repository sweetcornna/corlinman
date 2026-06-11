import { AbsoluteFill, Audio, interpolate, Sequence, staticFile, useCurrentFrame } from "remotion";
import { useAspect } from "../tokens/aspect";
import { GrainOverlay } from "../primitives/GrainOverlay";
import { PersistentMascot } from "./PersistentMascot";
import { LocaleContext, type Locale } from "./i18n";
import { SCENE_DURATION, CROSSFADE } from "./timeline";
import { Scene01_Spark } from "./scenes/Scene01_Spark";
import { Scene02_Assemble } from "./scenes/Scene02_Assemble";
import { Scene03_Hero } from "./scenes/Scene03_Hero";
import { Scene04_Dash } from "./scenes/Scene04_Dash";
import { Scene05_Orbit } from "./scenes/Scene05_Orbit";
import { Scene06_Mesh } from "./scenes/Scene06_Mesh";
import { Scene07_Circuit } from "./scenes/Scene07_Circuit";
import { Scene08_Swarm } from "./scenes/Scene08_Swarm";
import { Scene09_Cards } from "./scenes/Scene09_Cards";
import { Scene10_Reflect } from "./scenes/Scene10_Reflect";
import { Scene11_Frame } from "./scenes/Scene11_Frame";
import { Scene12_Wordmark } from "./scenes/Scene12_Wordmark";

type SceneComp = React.FC<{ localFrame: number; aspect: "h" | "v" }>;

const SCENES: SceneComp[] = [
  Scene01_Spark,
  Scene02_Assemble,
  Scene03_Hero,
  Scene04_Dash,
  Scene05_Orbit,
  Scene06_Mesh,
  Scene07_Circuit,
  Scene08_Swarm,
  Scene09_Cards,
  Scene10_Reflect,
  Scene11_Frame,
  Scene12_Wordmark,
];

const SceneSlot: React.FC<{
  Comp: SceneComp;
  isFirst: boolean;
  isLast: boolean;
}> = ({ Comp, isFirst, isLast }) => {
  const seqFrame = useCurrentFrame();
  const aspect = useAspect();

  // localFrame inside the scene: 0 == when scene officially begins (after fade-in lead)
  const localFrame = seqFrame - (isFirst ? 0 : CROSSFADE);
  const totalIn = isFirst ? 0 : CROSSFADE;
  const totalOut = isLast ? 0 : CROSSFADE;
  const slotDuration = SCENE_DURATION + totalIn + totalOut;

  // Crossfade opacity
  const opIn = totalIn > 0
    ? interpolate(seqFrame, [0, totalIn], [0, 1], {
        extrapolateLeft: "clamp",
        extrapolateRight: "clamp",
      })
    : 1;
  const opOut = totalOut > 0
    ? interpolate(seqFrame, [slotDuration - totalOut, slotDuration], [1, 0], {
        extrapolateLeft: "clamp",
        extrapolateRight: "clamp",
      })
    : 1;
  const opacity = Math.min(opIn, opOut);

  return (
    <AbsoluteFill style={{ opacity }}>
      <Comp localFrame={localFrame} aspect={aspect} />
    </AbsoluteFill>
  );
};

// Master pixel-mascot composition.
// 12 scenes × 150f (5s each) = 1800f total = 60s.
// Adjacent scenes overlap by CROSSFADE so the fade-out of N overlaps
// the fade-in of N+1, eliminating any black gap.
export const PixelFilm: React.FC<{ locale?: Locale }> = ({ locale = "en" }) => {
  return (
    <LocaleContext.Provider value={locale}>
    <AbsoluteFill style={{ backgroundColor: "#050912" }}>
      {SCENES.map((Comp, i) => {
        const isFirst = i === 0;
        const isLast = i === SCENES.length - 1;
        const lead = isFirst ? 0 : CROSSFADE;
        const tail = isLast ? 0 : CROSSFADE;
        const from = i * SCENE_DURATION - lead;
        const dur = SCENE_DURATION + lead + tail;
        return (
          <Sequence
            key={i}
            from={from}
            durationInFrames={dur}
            name={`scene-${String(i + 1).padStart(2, "0")}`}
          >
            <SceneSlot Comp={Comp} isFirst={isFirst} isLast={isLast} />
          </Sequence>
        );
      })}
      {/* The mascot is the protagonist — one continuous performance across the
          whole 60s. Decorations in scenes morph around it; the mascot itself
          never disappears or teleports. */}
      <PersistentMascot />
      <GrainOverlay opacity={0.25} />
      <Audio src={staticFile("bgm-60.mp3")} volume={0.65} />
    </AbsoluteFill>
    </LocaleContext.Provider>
  );
};
