---
name: plan
description: Plan-only mode — produce a written implementation plan under .corlinman/plans/ without touching code.
metadata:
  openclaw:
    emoji: "📐"
    requires:
      bins: []
      anyBins: []
      config: []
      env: []
    install: |
      No installation needed. The skill writes a single markdown file under
      `.corlinman/plans/` in the active working directory.
allowed-tools:
  - read_file
  - write_file
---
# Plan Mode

Use this skill when the user wants a plan, not execution.

## Core behavior

For this turn, you are planning only.

- Do not implement code.
- Do not edit project files except the plan markdown file you write at the end.
- Do not run mutating shell commands, commit, push, or perform external actions.
- Read-only inspection of the repo or other context is fine.
- Your deliverable is a single markdown plan saved under `.corlinman/plans/`.

## Output requirements

Write a markdown plan that is concrete and actionable. Include, when relevant:

- **Goal** — one sentence describing what this builds.
- **Current context / assumptions** — what you've verified vs. what you're guessing.
- **Proposed approach** — 2–3 sentences on architecture.
- **Step-by-step plan** — bite-sized tasks (2–5 min each), with exact file paths.
- **Tests / validation** — how each task is verified.
- **Risks, tradeoffs, open questions** — anything the user needs to decide.

If the task is code-related, include exact file paths, likely test targets, and verification steps.

## Save location

```
.corlinman/plans/YYYY-MM-DD_HHMMSS-<slug>.md
```

Relative to the active working directory. If the runtime provides a specific target path, use that exact path instead.

## Interaction style

- If the request is clear, write the plan directly.
- If genuinely underspecified, ask one brief clarifying question instead of guessing.
- After saving the plan, reply with a one-line summary and the saved path.

## Related skills

- `writing-plans` — the longer-form discipline for multi-task plans.
- `subagent-driven-development` — executes a plan via fan-out.
- `executing-plans` — executes a plan single-threaded in the current session.
