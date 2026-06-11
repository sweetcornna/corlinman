import { Composition } from "remotion";
import { PixelFilm } from "./pixel/PixelFilm";
import { FPS, TOTAL_FRAMES } from "./tokens/timeline";

export const Root: React.FC = () => {
  return (
    <>
      <Composition
        id="CorlinmanPromoH"
        component={PixelFilm}
        durationInFrames={TOTAL_FRAMES}
        fps={FPS}
        width={1920}
        height={1080}
        defaultProps={{ locale: "en" as const }}
      />
      <Composition
        id="CorlinmanPromoV"
        component={PixelFilm}
        durationInFrames={TOTAL_FRAMES}
        fps={FPS}
        width={1080}
        height={1920}
        defaultProps={{ locale: "en" as const }}
      />
      <Composition
        id="CorlinmanPromoHCN"
        component={PixelFilm}
        durationInFrames={TOTAL_FRAMES}
        fps={FPS}
        width={1920}
        height={1080}
        defaultProps={{ locale: "zh" as const }}
      />
      <Composition
        id="CorlinmanPromoVCN"
        component={PixelFilm}
        durationInFrames={TOTAL_FRAMES}
        fps={FPS}
        width={1080}
        height={1920}
        defaultProps={{ locale: "zh" as const }}
      />
    </>
  );
};
