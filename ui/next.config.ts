import type { NextConfig } from "next";

// corlinman ui — Next.js config
//
// NOTE: `output: "export"` produces a fully static bundle so the Docker
// `ui-builder` stage can copy `out/` into the runtime image (see plan §10).
// TODO(M6): Switch to SSR (`output: undefined` / default) if admin pages
// require request-time data from the gateway that cannot be fetched from
// the client. In that case the Dockerfile needs to change to ship `.next/`
// and run `next start` instead of serving a static export.
const nextConfig: NextConfig = {
  reactStrictMode: true,
  output: "export",
  // Per-deploy asset prefix lets us defeat a poisoned CDN cache: when
  // Cloudflare (or any edge) cached an HTML 404.html body under a
  // chunk URL during a deploy gap, the only fix without CDN purge
  // credentials is to serve chunks from a brand-new URL the CDN has
  // never seen. Set NEXT_ASSET_PREFIX=/a/v<timestamp> at build time
  // and pair with an nginx `rewrite ^/a/[^/]+/(_next/.*)$ /$1 last;`
  // so the prefix is stripped at the origin.
  assetPrefix: process.env.NEXT_ASSET_PREFIX ?? "",
  // next-intl plugin is wired in lib/i18n (see lib/i18n/*); keep here explicit.
  images: {
    // Static export cannot use the Next image optimizer.
    unoptimized: true,
  },
  typedRoutes: true,
};

export default nextConfig;
