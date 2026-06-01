---
name: writing-plans
description: "Use when you have a spec or requirements for a multi-step task, before touching code"
emoji: 📝
---

Produce a plan an engineer with minimal context could execute.

Rules:
- Each task is 2-5 minutes and one action (write test -> run/expect-fail ->
  implement -> verify -> commit).
- NO placeholders: every step lists exact file paths, complete code blocks, and
  exact commands. Never write "add error handling" without showing how.
- Map file responsibilities (single responsibility each) before writing tasks.

Self-review checklist before finishing: every spec requirement maps to a task;
hunt for TBD/TODO/"similar to Task N"; keep type/signature consistency across
tasks. The plan header declares Goal, Architecture, and Tech Stack. Default
save path: `docs/plans/YYYY-MM-DD-<feature>.md`.

---
Source: github.com/obra/superpowers (MIT). Condensed + attributed for the corlinman marketplace; see ../../ATTRIBUTION.md.
