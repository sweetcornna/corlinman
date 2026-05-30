---
name: executing-plans
description: Execute a written implementation plan in the current session, single-threaded, with review checkpoints.
metadata:
  openclaw:
    emoji: "▶️"
    requires:
      bins: []
      anyBins: []
      config: []
      env: []
    install: |
      No installation needed. The skill is procedural — it follows the
      plan and uses whatever read + shell + write tools the active agent
      already has.
allowed-tools:
  - file.read
  - file.write
  - shell.run
---
# Executing Plans

## Overview

Load a plan, review it critically, then execute every task. Report back when complete.

**Note:** If `subagent_spawn` is available in the active agent's tool whitelist, prefer the `subagent-driven-development` skill — fresh subagents per task produce higher quality. Use this skill when no subagents are available, or when the plan is short and a single pass is enough.

## The process

### Step 1 — load and review the plan

1. Read the plan file.
2. Review it critically — identify any questions, missing pieces, or concerns.
3. **If concerns exist:** raise them with the user before starting.
4. **If no concerns:** create a task list (mirroring the plan's tasks) and proceed.

### Step 2 — execute tasks

For each task:

1. Mark as in-progress.
2. Follow each step exactly — the plan has bite-sized steps for a reason.
3. Run the verifications the plan specifies (use `shell.run`).
4. Mark completed once verifications pass.

### Step 3 — completion

After every task is done and verified:

- Announce: "All plan tasks complete. Running final verification."
- Run the full test suite.
- Report: tests, file changes, any deferred items.

## When to stop and ask

**STOP immediately when:**

- You hit a blocker (missing dependency, failing test, unclear instruction).
- The plan has a critical gap that prevents starting.
- You don't understand an instruction.
- Verification fails repeatedly (>2 attempts on the same task).

Ask for clarification — do not guess.

## When to revisit earlier steps

Return to Step 1 if:

- The user updates the plan based on your feedback.
- The fundamental approach needs rethinking.

Do not force through blockers — stop and ask.

## Discipline reminders

- Review the plan critically first.
- Follow plan steps exactly.
- Do not skip verifications.
- Reference other skills (`test-driven-development`, `verification-before-completion`) when the plan says to.
- Never start implementation on `main` / `master` without explicit user consent.

## Integration

- **`git-worktrees`** — set up an isolated workspace before starting.
- **`writing-plans`** — the producer of the plan this skill executes.
- **`verification-before-completion`** — apply at every "verify" step.
- **`subagent-driven-development`** — the preferred alternative when subagents are available.
