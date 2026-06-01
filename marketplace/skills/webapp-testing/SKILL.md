---
name: webapp-testing
description: "Toolkit for interacting with and testing local web applications using Playwright. Supports verifying frontend functionality, debugging UI behavior, capturing browser screenshots, and viewing browser logs."
emoji: 🧪
---

Prefer small, native Playwright (Python) scripts over ad-hoc clicking.

Recon-then-action pattern:
1. First inspect the rendered DOM — take a screenshot and dump the content.
2. Identify selectors from what you actually see (not what you assume).
3. Then act on those selectors.

Decision: static HTML -> just read it; dynamic content -> drive a real browser.

Pitfalls: don't inspect the DOM before the app finishes loading — wait for
`networkidle` on dynamic apps. Use the synchronous Playwright API, close
browsers properly, use descriptive selectors, and add appropriate waits.
Capture console logs to debug runtime errors.

---
Source: github.com/anthropics/skills (Apache-2.0). Condensed + attributed for the corlinman marketplace; see ../../ATTRIBUTION.md.
