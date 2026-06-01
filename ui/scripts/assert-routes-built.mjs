#!/usr/bin/env node
// @ts-check
//
// Build-time regression guard for the Next.js static export.
//
// Why this exists
// ---------------
// The admin UI ships as a Next.js static export (`out/`). In prod (native
// systemd) `install.sh` rsyncs `ui/out` -> `$PREFIX/ui-static`, and the
// gateway serves that baked dir via the `_NextStaticFiles` mount in
// python/.../gateway/lifecycle/entrypoint.py. That mount resolves an
// extensionless route `channels/qq` by appending `.html` and, when the
// target file is absent, silently falls through to `404.html`.
//
// The failure mode this guards against: a stale or partial build that is
// MISSING a route file (e.g. channels/qq.html) ships without error, and
// every request to that page quietly serves the 404 shell. Nothing in the
// pipeline notices because the request still returns 200-ish HTML.
//
// This script runs AFTER `next build` and fails the build (non-zero exit)
// if any required route is either missing OR byte-identical to 404.html
// (which is how Next represents a route that did not actually render).
//
// Uses only Node builtins so it works in any CI image without installs.

import { readFileSync, existsSync, statSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// ---------------------------------------------------------------------------
// Required routes.
//
// IMPORTANT: when you add a new top-level page or channel page, APPEND its
// emitted HTML path here (relative to out/, forward slashes). Each entry is
// asserted to (a) exist on disk and (b) NOT be byte-identical to 404.html.
// Keeping this list in sync is the whole point of the guard.
// ---------------------------------------------------------------------------
const REQUIRED_ROUTES = [
  'index.html',
  'login.html',
  'chat.html',
  'channels/qq.html',
  'channels/telegram.html',
  'channels/discord.html',
  'channels/slack.html',
  'channels/feishu.html',
  'channels/wechat_official.html',
  'channels/qq_official.html',
  'marketplace.html',
  'marketplace/acceleration.html',
];

const NOT_FOUND_ROUTE = '404.html';

const scriptDir = dirname(fileURLToPath(import.meta.url));
// out/ lives one level up from scripts/ (ui/scripts -> ui/out).
const outDir = resolve(scriptDir, '..', 'out');

/** @param {string} rel */
function readRoute(rel) {
  const abs = join(outDir, rel);
  if (!existsSync(abs) || !statSync(abs).isFile()) {
    return null;
  }
  return readFileSync(abs);
}

/** @type {string[]} */
const problems = [];

if (!existsSync(outDir) || !statSync(outDir).isDirectory()) {
  console.error(
    `assert-routes-built: export dir not found: ${outDir}\n` +
      'Did `next build` run and emit a static export?',
  );
  process.exit(1);
}

const notFoundBuf = readRoute(NOT_FOUND_ROUTE);
if (notFoundBuf === null) {
  // Without 404.html we cannot run the byte-identity half of the check, and
  // its absence is itself a broken export.
  problems.push(
    `${NOT_FOUND_ROUTE} is missing — cannot verify routes against the 404 shell`,
  );
}

for (const rel of REQUIRED_ROUTES) {
  const buf = readRoute(rel);
  if (buf === null) {
    problems.push(`${rel} is MISSING`);
    continue;
  }
  if (notFoundBuf !== null && buf.equals(notFoundBuf)) {
    problems.push(
      `${rel} is byte-identical to ${NOT_FOUND_ROUTE} (route did not render)`,
    );
  }
}

if (problems.length > 0) {
  console.error(
    'assert-routes-built: static export is missing required route(s):\n' +
      problems.map((p) => `  - ${p}`).join('\n') +
      `\n\nChecked ${REQUIRED_ROUTES.length} route(s) under ${outDir}.\n` +
      'A build missing these would silently serve the 404 shell in prod. ' +
      'Fix the build before shipping.',
  );
  process.exit(1);
}

console.log(
  `assert-routes-built: OK — ${REQUIRED_ROUTES.length} required route(s) ` +
    `present and distinct from ${NOT_FOUND_ROUTE} in ${outDir}.`,
);
