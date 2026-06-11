// Reusable SVG filter defs for impasto brush stroke + glow.
// Drop <ImpastoDefs /> once at the root of a scene; reference via filter="url(#impasto)".

export const ImpastoDefs: React.FC = () => (
  <svg width="0" height="0" style={{ position: "absolute" }}>
    <defs>
      <filter id="impasto" x="-15%" y="-30%" width="130%" height="160%">
        <feTurbulence
          type="fractalNoise"
          baseFrequency="0.04"
          numOctaves="3"
          seed="7"
          result="noise"
        />
        <feDisplacementMap in="SourceGraphic" in2="noise" scale="22" />
        <feGaussianBlur stdDeviation="1.2" />
      </filter>
      <filter id="impastoFine" x="-15%" y="-30%" width="130%" height="160%">
        <feTurbulence
          type="fractalNoise"
          baseFrequency="0.08"
          numOctaves="2"
          seed="3"
          result="noise"
        />
        <feDisplacementMap in="SourceGraphic" in2="noise" scale="10" />
        <feGaussianBlur stdDeviation="0.6" />
      </filter>
      <filter id="softGlow">
        <feGaussianBlur stdDeviation="2.5" result="b" />
        <feMerge>
          <feMergeNode in="b" />
          <feMergeNode in="SourceGraphic" />
        </feMerge>
      </filter>
      <radialGradient id="nodeGlow" cx="50%" cy="50%" r="50%">
        <stop offset="0%" stopColor="#82A8E8" stopOpacity="1" />
        <stop offset="40%" stopColor="#5A88F7" stopOpacity="0.6" />
        <stop offset="100%" stopColor="#1F4FB8" stopOpacity="0" />
      </radialGradient>
      <radialGradient id="nodeGlowDay" cx="50%" cy="50%" r="50%">
        <stop offset="0%" stopColor="#1F4FB8" stopOpacity="0.7" />
        <stop offset="100%" stopColor="#1F4FB8" stopOpacity="0" />
      </radialGradient>
    </defs>
  </svg>
);
