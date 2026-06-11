import type { Metadata } from "next";
import "./globals.css";
import { GeistSans } from "geist/font/sans";
import { GeistMono } from "geist/font/mono";
import { Providers } from "@/components/providers";

// Tidepool display serif (Phase 0). Used only where explicitly opted in
// via `font-serif` (hero greeting, uptime streak card, italic emphasis).
// The CSS variable is defined in globals.css using local system fallbacks so
// Docker builds do not depend on Google Fonts availability.

export const metadata: Metadata = {
  title: "corlinman admin",
  description:
    "corlinman admin UI — Rust gateway + Python AI layer + Next.js control plane.",
};

// Inline boot script. Runs before React hydrates so <html lang> matches
// the persisted i18n choice (or the browser hint) — no FOUC when the user
// previously picked English. Also hydrates the Tidepool theme (light/dark)
// from localStorage so theme-sensitive surfaces (aurora, glass, palette
// outline) paint in the correct mode on first paint, not after React.
const BOOT = `
(function(){try{
  var el = document.documentElement;
  // Language
  var k="corlinman_lang";
  var s=localStorage.getItem(k);
  var l=(s==="zh-CN"||s==="en")?s:((navigator.language||"").toLowerCase().indexOf("zh")===0?"zh-CN":"en");
  el.setAttribute("lang",l);
  // Theme (Tidepool). URL ?theme=light|dark wins over storage (handy for
  // demos / screenshot testing) — and is persisted to localStorage so that
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
  // Custom accent (corlinman-accent = oklch hue). Mirrors lib/accent.ts
  // buildAccentCss — keep the two generators in sync. Injected before
  // first paint so the chosen theme color never flashes the default.
  var ah=localStorage.getItem("corlinman-accent");
  if(ah!==null&&ah!==""&&isFinite(Number(ah))){
    var H=((Number(ah)%360)+360)%360, H2=(H+55)%360, H3=(H-15+360)%360, HH=Math.round((H-18+360)%360);
    var st=document.createElement("style");
    st.id="sg-accent-override";
    st.textContent=":root{--sg-accent:oklch(0.5 0.16 "+H+");--sg-accent-soft:oklch(0.5 0.16 "+H+" / 0.1);--sg-accent-glow:oklch(0.55 0.16 "+H+" / 0.3);--sg-accent-2:oklch(0.47 0.2 "+H2+");--sg-accent-2-soft:oklch(0.47 0.2 "+H2+" / 0.1);--sg-accent-3:oklch(0.55 0.1 "+H3+");--sg-accent-3-soft:oklch(0.55 0.1 "+H3+" / 0.1);--sg-grad-text:linear-gradient(115deg, oklch(0.46 0.17 "+H+"), oklch(0.52 0.12 "+H3+") 45%, oklch(0.44 0.21 "+H2+"));--primary:"+HH+" 70% 45%;--ring:"+HH+" 75% 55%;}.dark{--sg-accent:oklch(0.78 0.13 "+H+");--sg-accent-soft:oklch(0.78 0.13 "+H+" / 0.14);--sg-accent-glow:oklch(0.78 0.13 "+H+" / 0.45);--sg-accent-2:oklch(0.7 0.17 "+H2+");--sg-accent-2-soft:oklch(0.7 0.17 "+H2+" / 0.14);--sg-accent-3:oklch(0.85 0.07 "+H3+");--sg-accent-3-soft:oklch(0.85 0.07 "+H3+" / 0.14);--sg-grad-text:linear-gradient(115deg, oklch(0.86 0.11 "+H+"), oklch(0.92 0.05 "+H3+") 45%, oklch(0.76 0.17 "+H2+"));--primary:"+HH+" 75% 70%;--ring:"+HH+" 80% 70%;}";
    document.head.appendChild(st);
  }
}catch(e){}})();
`;

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  // `suppressHydrationWarning` is required by next-themes when it toggles the
  // dark/light class on <html>. Geist sans + mono are exposed as CSS vars
  // (`--font-geist-sans`, `--font-geist-mono`) consumed by tailwind.config.ts.
  return (
    <html
      lang="zh-CN"
      suppressHydrationWarning
      className={`${GeistSans.variable} ${GeistMono.variable}`}
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: BOOT }} />
      </head>
      {/* Body does NOT paint a background. Admin routes mount their own
          <AuroraBackground />, login/status build their own nebula layers.
          Route groups that need a solid color set it on their own wrapper.
          This lets the deep-space backdrop painted on <html> show through —
          otherwise bg-background sits on top of the fixed -z-10 layer. */}
      <body className="min-h-dvh font-sans text-foreground antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
