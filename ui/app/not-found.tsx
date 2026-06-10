"use client";

/**
 * Global 404 page. Next.js App Router picks this up automatically whenever
 * a route doesn't match.
 *
 * Visuals: large "404", short helper copy, Home link. Backdrop is 15 small
 * dots drifting slowly via pure CSS keyframes — no JS, no framer-motion, no
 * layout impact (absolutely positioned, `pointer-events: none`). Each dot
 * has a unique animation-delay so the field stays desynchronised.
 *
 * `prefers-reduced-motion: reduce` freezes every dot in its initial position
 * via a single `@media` block — no JS gate required.
 *
 * Client component so the copy reacts to the language toggle at runtime.
 */

import Link from "next/link";
import { useTranslation } from "react-i18next";

import { Mascot } from "@/components/ui/mascot";

// Deterministic per-dot config. Keeping this static avoids hydration drift
// and lets us ship zero client JS for the decoration.
const DOTS: ReadonlyArray<{ top: string; left: string; delay: string; dur: string; size: number }> =
  [
    { top: "12%", left: "8%", delay: "0s", dur: "11s", size: 6 },
    { top: "22%", left: "72%", delay: "1.2s", dur: "13s", size: 4 },
    { top: "34%", left: "18%", delay: "2.6s", dur: "9s", size: 5 },
    { top: "45%", left: "88%", delay: "0.8s", dur: "14s", size: 3 },
    { top: "58%", left: "12%", delay: "3.1s", dur: "12s", size: 7 },
    { top: "67%", left: "62%", delay: "1.9s", dur: "10s", size: 4 },
    { top: "78%", left: "82%", delay: "4.2s", dur: "15s", size: 5 },
    { top: "86%", left: "28%", delay: "2.1s", dur: "11s", size: 6 },
    { top: "18%", left: "48%", delay: "3.7s", dur: "13s", size: 3 },
    { top: "52%", left: "38%", delay: "0.4s", dur: "12s", size: 5 },
    { top: "72%", left: "52%", delay: "4.8s", dur: "14s", size: 4 },
    { top: "28%", left: "92%", delay: "1.5s", dur: "10s", size: 6 },
    { top: "62%", left: "72%", delay: "3.3s", dur: "13s", size: 4 },
    { top: "82%", left: "8%", delay: "2.4s", dur: "9s", size: 5 },
    { top: "40%", left: "58%", delay: "0.6s", dur: "15s", size: 3 },
  ];

export default function NotFound() {
  const { t } = useTranslation();
  return (
    <main
      className="relative grid min-h-dvh place-items-center overflow-hidden bg-background px-6"
      data-testid="not-found"
    >
      {/* Drifting dot field — purely decorative. */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0"
        data-testid="not-found-dots"
      >
        {DOTS.map((d, i) => (
          <span
            key={i}
            className={`nf-dot nf-dot-${i % 4}`}
            style={{
              top: d.top,
              left: d.left,
              width: d.size,
              height: d.size,
              animationDelay: d.delay,
              animationDuration: d.dur,
            }}
          />
        ))}
      </div>

      <div className="relative z-10 flex flex-col items-center text-center">
        <Mascot size={108} />
        <p className="mt-4 font-mono text-sm uppercase tracking-[0.3em] text-muted-foreground">
          404
        </p>
        <h1 className="sg-grad-text mt-2 text-7xl font-semibold tracking-tight md:text-8xl">
          404
        </h1>
        <p className="mt-4 max-w-sm text-sm text-muted-foreground">
          {t("notFound.description")}
        </p>
        <Link
          href="/"
          className="mt-6 inline-flex items-center rounded-md border border-border bg-panel px-4 py-2 text-sm font-medium text-foreground transition-colors hover:border-primary/40 hover:bg-accent/40"
        >
          {t("notFound.backToDashboard")}
        </Link>
      </div>

      {/* Scoped CSS — four keyframes so neighbouring dots drift in distinct
          directions. Amplitude stays ≤12px to keep this unobtrusive. */}
      <style>{`
        .nf-dot {
          position: absolute;
          border-radius: 9999px;
          background: hsl(var(--muted-foreground) / 0.35);
          animation-name: nf-drift-0;
          animation-iteration-count: infinite;
          animation-timing-function: ease-in-out;
          animation-direction: alternate;
          will-change: transform, opacity;
        }
        .nf-dot-0 { animation-name: nf-drift-0; }
        .nf-dot-1 { animation-name: nf-drift-1; }
        .nf-dot-2 { animation-name: nf-drift-2; }
        .nf-dot-3 { animation-name: nf-drift-3; }

        @keyframes nf-drift-0 {
          0%   { transform: translate3d(0, 0, 0);     opacity: 0.35; }
          100% { transform: translate3d(10px, -8px, 0); opacity: 0.6;  }
        }
        @keyframes nf-drift-1 {
          0%   { transform: translate3d(0, 0, 0);      opacity: 0.5;  }
          100% { transform: translate3d(-9px, 11px, 0); opacity: 0.3;  }
        }
        @keyframes nf-drift-2 {
          0%   { transform: translate3d(0, 0, 0);       opacity: 0.4;  }
          100% { transform: translate3d(8px, 10px, 0);  opacity: 0.65; }
        }
        @keyframes nf-drift-3 {
          0%   { transform: translate3d(0, 0, 0);        opacity: 0.55; }
          100% { transform: translate3d(-12px, -9px, 0); opacity: 0.35; }
        }

        @media (prefers-reduced-motion: reduce) {
          .nf-dot {
            animation: none !important;
            transform: none !important;
            opacity: 0.4;
          }
        }
      `}</style>
    </main>
  );
}
