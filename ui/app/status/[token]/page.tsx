/**
 * Public **agent status card** — `GET {public_url}/status/{token}`.
 *
 * This page sits OUTSIDE the `(admin)` route group: it's public and
 * unauthenticated. A chat user clicks the link the agent posts in a channel
 * reply and lands here to watch the agent's current step + a read-only work
 * trajectory, with NO admin login and NO admin shell / nav.
 *
 * Static-export note: `next.config.ts` ships `output: "export"`, so a dynamic
 * `[token]` segment needs a `generateStaticParams()` enumeration. The token is
 * unbounded (it's a signed capability minted at runtime), so we emit a SINGLE
 * placeholder HTML shell and read the real token from the URL at runtime in
 * the client component. With `dynamicParams = false`, every `/status/<token>`
 * request the gateway serves resolves to that one shell (the gateway's
 * `_NextStaticFiles` mount serves it for any unmatched path); the shell then
 * fetches the JSON capability payload + subscribes to the live SSE feed.
 */

import { StatusClient } from "./status-client";

// Single placeholder param. The real token is read client-side from the URL
// (`window.location`) so one exported shell serves every token. `__shell__`
// is just the filename Next emits (`status/__shell__.html`); it's never shown.
export function generateStaticParams(): { token: string }[] {
  return [{ token: "__shell__" }];
}

// Reject build-time enumeration of arbitrary tokens — we intentionally ship
// exactly one shell and resolve the token at request time in the browser.
export const dynamicParams = false;

export default function StatusTokenPage() {
  return <StatusClient />;
}
