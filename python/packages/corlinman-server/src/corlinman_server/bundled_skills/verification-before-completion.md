---
name: verification-before-completion
description: Run the verification command and read its output before claiming any work is complete, fixed, or passing.
metadata:
  openclaw:
    emoji: "✅"
    requires:
      bins: []
      anyBins: []
      config: []
      env: []
    install: |
      No installation needed. The skill enforces a discipline: run the
      command, read the output, then state the result.
allowed-tools:
  - run_shell
---
# Verification Before Completion

**Core principle:** evidence before claims. A status report describes something you observed, not something you expect.

## The gate

Before reporting any status ("done", "fixed", "passing", "clean"):

1. Identify the command whose output would prove the claim.
2. Run it fresh — a run from before your latest change proves nothing.
3. Read the full output: exit code, failure counts, warnings.
4. Report what the output actually shows, with the evidence next to the claim. If it shows a failure, report the failure — that is a useful result, not something to soften.

If verification is impossible in the current environment, say so explicitly and name the command the user should run.

## What counts as evidence

| Claim | Evidence |
|-------|---------|
| Tests pass | The test command's output for this change, 0 failures |
| Build succeeds | Build command exiting 0 — a clean linter is not a build |
| Bug fixed | The originally-failing case now passing |
| Regression test works | Red-green verified: test fails without the fix, passes with it |
| Subagent completed | `git diff`/`git status` showing the change exists — not the agent's own success report |
| Requirements met | Each requirement checked against the actual diff |

## Notes

- Partial checks support partial claims. "Unit tests pass; integration not run here" is a fine report — say exactly that.
- Confidence, plausibility, and "the change is simple" are reasons to *expect* success, not evidence of it.

## Related skills

- `systematic-debugging` — the upstream discipline that produces a fix worth verifying.
- `test-driven-development` — TDD's verify step uses this skill.
- `requesting-code-review` — runs the verification pipeline before commit.
