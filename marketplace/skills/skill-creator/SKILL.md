---
name: skill-creator
description: "Create new skills, modify and improve existing skills, and measure skill performance. Use when users want to create a skill from scratch, edit, or optimize an existing skill, run evals to test a skill, benchmark skill performance, or optimize a skill's description for better triggering accuracy."
emoji: 🛠️
---

A skill is a `SKILL.md` with YAML frontmatter (`name`, `description`, optional
`when_to_use`/`allowed-tools`) plus a markdown body of instructions, optionally
with bundled resource files.

Loop: define intent -> draft -> test with realistic prompts -> evaluate
(qualitatively + with a few saved evals) -> improve -> repeat -> package.

Guidance:
- Make the `description` "pushy" and specific so the skill triggers reliably
  (under-triggering is the most common failure).
- Keep the body focused (~under 500 lines); use progressive disclosure — link
  out to reference files for depth instead of inlining everything.
- Explain the "why", prefer imperative voice, and give concrete examples.
- Save 2-3 should-trigger and should-not-trigger prompts to sanity-check
  triggering before shipping.

---
Source: github.com/anthropics/skills (Apache-2.0). Condensed + attributed for the corlinman marketplace; see ../../ATTRIBUTION.md.
