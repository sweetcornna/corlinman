---
name: subagent-driven-development
description: Execute an implementation plan via fresh subagents per task with two-stage review (spec compliance then code quality).
metadata:
  openclaw:
    emoji: "🛰️"
    requires:
      bins: []
      anyBins: []
      config: []
      env: []
    install: |
      No installation needed. The skill relies on the `subagent_spawn`
      tool, which is part of the corlinman runtime.
allowed-tools:
  - file.read
  - subagent_spawn
  - subagent_spawn_many
  - blackboard.read
  - blackboard.write
---
# Subagent-Driven Development

## Overview

Execute an implementation plan by dispatching a fresh subagent per task, with a systematic two-stage review (spec compliance, then code quality) between each task.

**Core principle:** Fresh subagent per task + two-stage review = high quality, fast iteration.

## When to use

- You have an implementation plan (from `writing-plans` or the user).
- Tasks are mostly independent.
- Quality and spec compliance are non-negotiable.
- You want automated review between tasks instead of accumulating context.

## The process

### 1. Read and parse the plan

Read the plan file ONCE. Extract every task with its full text upfront. Do **not** make subagents read the plan file — provide the full task body directly in `extra_context`.

### 2. Per-task workflow

For each task:

#### Step 1 — dispatch implementer subagent

```
subagent_spawn(
  agent="editor",
  goal="Implement Task N: <descriptive name>",
  extra_context={
    "task_spec": "...full task text from plan...",
    "discipline": "Follow TDD: failing test → verify fail → minimal code → verify pass → commit.",
    "project_context": "Python 3.12, FastAPI in src/app.py, tests use pytest from project root."
  }
)
```

#### Step 2 — dispatch spec-compliance reviewer

After the implementer returns, verify against the original spec:

```
subagent_spawn(
  agent="researcher",
  goal="Verify the implementation matches the spec exactly.",
  extra_context={
    "task_spec": "...original spec...",
    "checklist": [
      "All requirements from spec implemented?",
      "File paths match spec?",
      "Function signatures match spec?",
      "Nothing extra added (no scope creep)?"
    ],
    "output_format": "PASS or list of specific spec gaps."
  }
)
```

**If spec issues:** dispatch a fix subagent, then re-run the spec reviewer. Continue only when PASS.

#### Step 3 — dispatch code-quality reviewer

After spec compliance passes:

```
subagent_spawn(
  agent="researcher",
  goal="Review code quality for Task N.",
  extra_context={
    "files": ["src/...", "tests/..."],
    "output_format": "Critical / Important / Minor + APPROVED or REQUEST_CHANGES."
  }
)
```

**If quality issues:** fix, re-review. Continue only when APPROVED.

#### Step 4 — mark complete and advance

### 3. Final integration review

After ALL tasks complete, dispatch one more reviewer to check the *system* (do components compose, any inconsistencies between tasks, all tests green, ready for merge?).

### 4. Verify and commit

Run the full test suite. Review `git diff --stat`. Final commit if needed.

## Task granularity

Each task = 2–5 minutes of focused work, touching ideally one file.

- Too big: "Implement user authentication system"
- Right size: "Create User model with email + password fields"; "Add password-hash utility"; "Add login endpoint"; "Add JWT token generation"

## Red flags — never do these

- Start implementation without a plan.
- Skip either review (spec compliance OR code quality).
- Proceed with unfixed critical/important issues.
- Dispatch multiple implementer subagents for tasks that touch the same files.
- Make subagents read the plan file (provide full text in extra_context).
- Skip scene-setting context (the subagent needs to understand where its task fits).
- Start code-quality review before spec compliance has PASSED.
- Move to the next task with an open issue.

## Why this works

- **Fresh subagent per task** — prevents context pollution from accumulated state.
- **Two-stage review** — spec catches under/over-building; quality catches sloppiness.
- **Catch issues early** — cheaper than debugging compounded problems later.

## Sharing context across siblings

If two siblings genuinely need to coordinate (rare), pass a `blackboard_key` in each one's `extra_context` and have them read/write via `blackboard.read` / `blackboard.write`. Default to *no* shared blackboard — most tasks are independent.

## Related skills

- `writing-plans` — produces the plan this skill executes.
- `executing-plans` — alternative when no subagents are available.
- `requesting-code-review` — the discipline behind the code-quality stage.
- `test-driven-development` — implementer subagents must follow this.
- `git-worktrees` — call before fan-out to isolate the workspace.
