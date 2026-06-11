#!/usr/bin/env node
// Spatial Glass visual sweep — screenshots every app route in dark+light
// themes at desktop+mobile viewports against a running dev server.
//
// Usage:
//   pnpm dev                       # terminal 1 (plus the gateway on :6005)
//   node scripts/screenshot-sweep.mjs                 # public pages only
//   CORLINMAN_SWEEP_USER=admin CORLINMAN_SWEEP_PASS=… \
//     node scripts/screenshot-sweep.mjs               # full admin sweep
//
// Output: /_design/sweep/<label>/<theme>-<viewport>/<route>.png (gitignored).
import { chromium } from "@playwright/test";
import path from "node:path";
import fs from "node:fs";

const BASE_URL = process.env.PLAYWRIGHT_BASE_URL ?? "http://localhost:3000";
const USER = process.env.CORLINMAN_SWEEP_USER;
const PASS = process.env.CORLINMAN_SWEEP_PASS;
const LABEL = process.env.SWEEP_LABEL ?? new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-");
const ONLY = process.env.SWEEP_ONLY; // optional comma-separated route filter

const UI_DIR = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const OUT_ROOT = path.join(UI_DIR, "..", "_design", "sweep", LABEL);

// Build the route list from the app directory (route groups stripped).
function discoverRoutes(dir, prefix = "") {
  const routes = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (!entry.isDirectory()) {
      if (entry.name === "page.tsx") routes.push(prefix || "/");
      continue;
    }
    const name = entry.name;
    if (name.startsWith("_") || name === "node_modules") continue;
    const seg = name.startsWith("(") && name.endsWith(")") ? "" : `/${name}`;
    routes.push(...discoverRoutes(path.join(dir, name), prefix + seg));
  }
  return routes;
}

const PUBLIC_ROUTES = new Set(["/login", "/onboard"]);
let routes = [...new Set(discoverRoutes(path.join(UI_DIR, "app")))].sort();
// Dynamic segments render their empty/error shells — keep them but flag.
routes = routes.map((r) => r.replace("[token]", "__sweep-token__"));
if (ONLY) {
  const allow = ONLY.split(",");
  routes = routes.filter((r) => allow.some((a) => r.startsWith(a)));
}
if (!USER) {
  console.warn("No CORLINMAN_SWEEP_USER set — capturing public routes only.");
  routes = routes.filter((r) => PUBLIC_ROUTES.has(r) || r.startsWith("/status"));
}

const VIEWPORTS = [
  { key: "desktop", width: 1440, height: 900 },
  { key: "mobile", width: 390, height: 844 },
];
const THEMES = ["dark", "light"];

function slug(route) {
  return route === "/" ? "home" : route.slice(1).replace(/[/?=&[\]]+/g, "_");
}

const browser = await chromium.launch();
let failures = 0;

for (const theme of THEMES) {
  for (const vp of VIEWPORTS) {
    const ctx = await browser.newContext({
      viewport: { width: vp.width, height: vp.height },
      deviceScaleFactor: 2,
      colorScheme: theme === "dark" ? "dark" : "light",
    });
    const page = await ctx.newPage();

    if (USER) {
      // Authenticate through the real login form once per context.
      await page.goto(`${BASE_URL}/login?theme=${theme}`, { waitUntil: "networkidle" });
      await page.fill('input[name="username"]', USER);
      await page.fill('input[name="password"]', PASS ?? "");
      await Promise.all([
        page.waitForURL((u) => !u.pathname.startsWith("/login"), { timeout: 15_000 }),
        page.click('button[type="submit"]'),
      ]).catch(() => {
        console.error(`login failed (${theme}/${vp.key}) — admin routes will 401`);
      });
    }

    const dir = path.join(OUT_ROOT, `${theme}-${vp.key}`);
    fs.mkdirSync(dir, { recursive: true });

    for (const route of routes) {
      const url = `${BASE_URL}${route}?theme=${theme}`;
      try {
        await page.goto(url, { waitUntil: "networkidle", timeout: 20_000 });
        await page.waitForTimeout(450); // settle entrance animations
        await page.screenshot({ path: path.join(dir, `${slug(route)}.png`), fullPage: true });
        process.stdout.write(`✓ ${theme}/${vp.key} ${route}\n`);
      } catch (err) {
        failures += 1;
        console.error(`✗ ${theme}/${vp.key} ${route}: ${String(err).split("\n")[0]}`);
      }
    }
    await ctx.close();
  }
}

await browser.close();
console.log(`\nSweep written to ${OUT_ROOT}${failures ? ` — ${failures} route(s) failed` : ""}`);
process.exit(failures > routes.length / 2 ? 1 : 0);
