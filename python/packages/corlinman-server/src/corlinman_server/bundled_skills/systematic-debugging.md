---
name: systematic-debugging
description: Find root cause before proposing any fix. Use for every bug, test failure, or unexpected behavior.
metadata:
  openclaw:
    emoji: "🔬"
    requires:
      bins: []
      anyBins: []
      config: []
      env: []
    install: |
      No installation needed. The skill is procedural; it uses whatever
      read + shell tools the active agent already has.
allowed-tools:
  - read_file
  - run_shell
---
# Systematic Debugging

## Overview

Random fixes waste time and create new bugs. Quick patches mask the underlying issue.

**Core principle:** Find root cause *before* attempting any fix. Symptom fixes are failure.

## The Iron Law

```
NO FIXES WITHOUT ROOT CAUSE INVESTIGATION FIRST
```

If you haven't completed Phase 1, you cannot propose a fix.

## When to use

For any technical issue: test failures, production bugs, unexpected behavior, performance regressions, build failures, integration breaks. Especially under time pressure — guessing is slower than the four-phase process.

## The four phases

You MUST complete each phase before proceeding to the next.

### Phase 1 — Root-cause investigation

1. **Read the error carefully.** Stack traces, line numbers, error codes. Don't skim.
2. **Reproduce consistently.** What are the exact steps? If you can't reproduce reliably, gather more data — don't guess.
3. **Check recent changes.** `git log`, `git diff`, new dependencies, config drift.
4. **Gather evidence in multi-component systems.** When the system has multiple layers (CI → build → signing, API → service → DB), add diagnostic logging at *each* component boundary and run once to find WHERE the chain breaks before investigating *why*.
5. **Trace data flow.** Where does the bad value originate? What called the failing function with that value? Keep tracing up until you find the source. Fix at the source, not at the symptom.

### Phase 2 — Pattern analysis

1. Find similar working code in the same codebase.
2. Compare against the canonical reference implementation, completely — don't skim.
3. List every difference, even ones that "can't matter".
4. Understand what dependencies, settings, environment the working code assumes.

### Phase 3 — Hypothesis and testing

1. State one hypothesis clearly: "I think X is the root cause because Y."
2. Test minimally — the smallest possible change, one variable at a time.
3. If it worked, advance to Phase 4. If not, form a new hypothesis. Do NOT stack fixes.

### Phase 4 — Implementation

1. **Create a failing test case first** — simplest reproduction, automated if possible. Use the `test-driven-development` skill.
2. **One change at a time.** No "while I'm here" cleanup.
3. **Verify the fix.** Original failure passes; no new tests broken.
4. **If a fix doesn't work and you've already tried 3+**, stop and question the architecture. Each fix revealing a new problem elsewhere = wrong pattern, not failed hypothesis. Discuss with the user before attempting fix #4.

## Red flags — STOP and return to Phase 1

If you catch yourself thinking any of these, you skipped the process:

- "Quick fix for now, investigate later."
- "Just try changing X and see if it works."
- "Skip the test, I'll manually verify."
- "I don't fully understand but this might work."
- "Pattern says X but I'll adapt it differently."
- "One more fix attempt" — when you've already tried 2+.

## Quick reference

| Phase | Activities | Done when |
|-------|-----------|-----------|
| 1. Root cause | Read errors, reproduce, check changes, gather evidence | You understand WHAT and WHY |
| 2. Pattern | Find working examples, compare, list differences | Differences identified |
| 3. Hypothesis | One theory, smallest test | Confirmed or new hypothesis |
| 4. Implementation | Failing test → fix → verify | Bug resolved, suite green |

## Related skills

- `test-driven-development` — for the Phase 4 failing-test step.
- `verification-before-completion` — for confirming the fix actually landed before claiming done.
