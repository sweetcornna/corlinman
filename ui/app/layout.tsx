import type { Metadata } from "next";
import "./globals.css";
import { jetbrainsMono, misans, mplus1 } from "./fonts";
import { Providers } from "@/components/providers";
import { ICON_SPRITE } from "@/components/icons/sprite";

export const metadata: Metadata = {
  title: "corlinman admin",
  description:
    "corlinman admin UI — Rust gateway + Python AI layer + Next.js control plane.",
};

// Inline boot script. Runs before React hydrates to restore the Eclipse theme
// (light/dark) and tint from localStorage so theme-sensitive surfaces paint in
// the correct mode on first paint, not after React.
// Do not mutate language here: exported static HTML is zh-CN and the client
// must match it for the first render. The provider applies the user's
// persisted/browser language after hydration.
const BOOT = `
(function(){try{
  var el = document.documentElement;
  // Theme. URL ?theme=light|dark wins over storage (handy for demos /
  // screenshot testing) — and is persisted to localStorage so that
  // next-themes (initialised later inside React) sees the same value and
  // doesn't override our choice. Otherwise falls back to stored value,
  // then the legacy next-themes key, then dark as the default.
  var tk="corlinman-theme";
  var qs=(location.search||"").match(/[?&]theme=(light|dark)/);
  var t = qs ? qs[1] : localStorage.getItem(tk);
  if (!t) { var ts=localStorage.getItem("theme"); if (ts==="light"||ts==="dark") t=ts; }
  if (t!=="light" && t!=="dark") t="dark";
  if (qs) { try { localStorage.setItem(tk, t); } catch(_){} }
  el.setAttribute("data-theme", t);
  if (t==="dark") el.classList.add("dark"); else el.classList.remove("dark");
  // Purge Spatial Glass era theme keys: their persisted CSS blobs would
  // override Eclipse tokens with pre-redesign colors on first paint.
  try{
    ["corlinman-theme-css","corlinman-theme-studio","corlinman-accent"].forEach(function(k){localStorage.removeItem(k);});
  }catch(_){}
  // Tint (corlinman-tint = {"preset":"ice"} or {"hue":210}). Presets map to
  // the pure-CSS data-tint rules in globals.css; custom hues inject the same
  // two-block CSS lib/tint.ts generates — keep the two generators in sync.
  var raw=localStorage.getItem("corlinman-tint");
  if(raw){
    var v=JSON.parse(raw);
    if(v&&typeof v.preset==="string"&&/^(dawn|ice|rose|moss|iris)$/.test(v.preset)){
      el.setAttribute("data-tint", v.preset);
    } else if(v&&typeof v.hue==="number"&&isFinite(v.hue)){
      var H=((v.hue%360)+360)%360;
      var st=document.createElement("style");
      st.id="sg-tint-override";
      st.textContent=":root:not(.dark){--sg-tint:oklch(0.55 0.09 "+H+");--sg-tint-ink:#fff;--sg-tint-glow:oklch(0.55 0.09 "+H+" / 0.3);--sg-tint-soft:oklch(0.55 0.09 "+H+" / 0.08);}.dark{--sg-tint:oklch(0.85 0.09 "+H+");--sg-tint-ink:#000;--sg-tint-glow:oklch(0.85 0.09 "+H+" / 0.42);--sg-tint-soft:oklch(0.85 0.09 "+H+" / 0.1);}";
      document.head.appendChild(st);
    }
  }
}catch(e){}})();
`;

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  // `suppressHydrationWarning` is required by next-themes when it toggles the
  // dark/light class on <html>. The font CSS vars (--font-misans, --font-mplus,
  // --font-jetbrains-mono) are consumed by tailwind.config.ts and the
  // --st-font-* stacks in globals.css.
  return (
    <html
      lang="zh-CN"
      suppressHydrationWarning
      className={`${misans.variable} ${mplus1.variable} ${jetbrainsMono.variable}`}
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: BOOT }} />
      </head>
      {/* Body does NOT paint a background: the pure-black canvas + moonrise
          halo + vignette are painted on <html> by globals.css (pre-hydration,
          zero JS) and must show through everywhere. */}
      <body className="min-h-dvh font-sans text-foreground antialiased">
        {/* Eclipse icon sprite — one hidden <svg> of <symbol>s; every icon
            renders as <use href="#i-…"> against it (static-export safe). */}
        <div aria-hidden dangerouslySetInnerHTML={{ __html: ICON_SPRITE }} />
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
