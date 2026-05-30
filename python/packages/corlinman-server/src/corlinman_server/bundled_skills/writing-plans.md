---
name: writing-plans
description: Author bite-sized implementation plans with exact file paths, complete code, and TDD-shaped tasks.
metadata:
  openclaw:
    emoji: "📝"
    requires:
      bins: []
      anyBins: []
      config: []
      env: []
    install: |
      No installation needed. Output is a single markdown file under
      `docs/plans/` (or a path the user specifies).
allowed-tools:
  - file.read
  - file.write
  - shell.run
---
# Writing Implementation Plans

## Overview

Write a plan that the next implementer can execute without guessing. Assume they have zero context about this codebase and questionable taste — document every file path, every command, every verification step.

**Core principle:** A good plan makes implementation obvious. If someone has to guess, the plan is incomplete.

## When to use

- Before implementing any multi-step feature.
- Before delegating to `subagent_spawn_many` via the `subagent-driven-development` skill.
- Whenever a "small" change is touching more than 2 files.

## Bite-sized task granularity

Each task = 2–5 minutes of focused work. Each step inside a task is one action.

**Too big**

```
Task 1: Build authentication system
[50 lines of code across 5 files]
```

**Right size**

```
Task 1: Create User model with email field      (10 lines, 1 file)
Task 2: Add password_hash field to User         (8 lines, 1 file)
Task 3: Create password-hashing utility         (15 lines, 1 file)
```

## Plan document structure

```markdown
# <Feature Name> Implementation Plan

**Goal:** <one sentence>

**Architecture:** <2-3 sentences>

**Tech stack:** <key libs>

---

### Task N: <descriptive name>

**Objective:** <one sentence>

**Files:**
- Create: `exact/path/to/new_file.py`
- Modify: `exact/path/to/existing.py:45-67`
- Test:   `tests/path/to/test_file.py`

**Step 1 — Write failing test**

(copy-pasteable code)

**Step 2 — Verify failure**
`pytest tests/path/test.py::test_specific_behavior -v` → FAIL ("function not defined")

**Step 3 — Write minimal implementation**

(copy-pasteable code)

**Step 4 — Verify pass**
`pytest tests/path/test.py::test_specific_behavior -v` → PASS

**Step 5 — Commit**
`git add tests/path/test.py src/path/file.py && git commit -m "feat: ..."`
```

## Writing process

1. **Understand requirements.** Acceptance criteria, constraints.
2. **Explore the codebase.** Use `file.read` + `shell.run "rg <pattern>"` to find similar features and existing test patterns.
3. **Design the approach.** Architecture, file organization, dependencies.
4. **Write tasks in order:** setup → core (TDD per task) → edge cases → integration → cleanup.
5. **Fill in exact details:** exact paths (not "the config file"), complete code (not "add validation"), exact commands with expected output.
6. **Self-review checklist** before saving:
   - Each task is bite-sized.
   - File paths are exact.
   - Code is copy-pasteable.
   - Commands have expected output.
   - DRY, YAGNI, TDD applied.
7. **Save** under `docs/plans/YYYY-MM-DD-<feature>.md` and commit.

## Principles

**DRY** — extract shared logic; don't copy-paste validation in three places.

**YAGNI** — implement only what's needed *now*. No "future flexibility" fields.

**TDD** — every code-producing task runs through the full RED-GREEN cycle (see `test-driven-development`).

**Frequent commits** — one commit per task.

## Common mistakes

| Bad | Good |
|-----|------|
| "Add authentication" | "Create User model with email + password_hash fields" |
| "Step 1: Add validation function" | The complete function body |
| "Step 3: Test it works" | `pytest tests/test_auth.py -v` → 3 passed |
| "Create the model file" | `Create: src/models/user.py` |

## Execution handoff

After saving the plan:

> "Plan saved to `docs/plans/<file>.md`. Ready to execute via `subagent-driven-development` — fresh subagent per task with two-stage review (spec then quality). Shall I proceed?"

## Related skills

- `plan` — single-turn plan-only mode for smaller asks.
- `subagent-driven-development` — executes the plan via fan-out.
- `executing-plans` — executes the plan single-threaded.
- `test-driven-development` — the discipline each implementation task should follow.
