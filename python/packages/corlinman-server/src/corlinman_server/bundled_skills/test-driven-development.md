---
name: test-driven-development
description: Enforce RED-GREEN-REFACTOR. Write the failing test before any production code.
metadata:
  openclaw:
    emoji: "🧪"
    requires:
      bins: []
      anyBins: []
      config: []
      env: []
    install: |
      No installation needed. The skill is procedural; it uses whatever
      shell + file tools the active agent already has to run the project's
      existing test command.
allowed-tools:
  - read_file
  - write_file
  - run_shell
---
# Test-Driven Development (TDD)

## Overview

Write the test first. Watch it fail. Write the minimum code to pass.

**Core principle:** If you didn't watch the test fail, you don't know that it tests the right thing.

## The Iron Law

```
NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST
```

If you already wrote code before the test: delete it and start over. No exceptions, no "keep it as reference" — implement fresh from the test.

## When to use

**Always:** new features, bug fixes, refactors, behavior changes.

**Exceptions (ask the user first):** throwaway prototypes, generated code, configuration files.

Thinking "skip TDD just this once"? That's rationalization. Don't.

## Red-Green-Refactor cycle

### RED — write one failing test

One behavior per test, clear name, real code (not mocks unless truly unavoidable).

```python
def test_retries_failed_operations_3_times():
    attempts = 0
    def operation():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("fail")
        return "success"
    assert retry_operation(operation) == "success"
    assert attempts == 3
```

### Verify RED — run it and watch it fail

```
run_shell("pytest tests/test_feature.py::test_specific_behavior -v")
```

Confirm: the failure message is the *expected* one (feature missing), not a typo or import error. If the test passes immediately, you are testing existing behavior — fix the test.

### GREEN — minimum code to pass

The simplest implementation. Hardcode is fine in GREEN — you'll clean it up in REFACTOR.

```python
def add(a, b):
    return a + b  # nothing extra
```

### Verify GREEN

```
run_shell("pytest tests/test_feature.py::test_specific_behavior -v")
run_shell("pytest -q")   # whole suite, no regressions
```

### REFACTOR — clean up

After green only. Remove duplication, improve names, extract helpers — but don't change behavior. Keep tests green throughout. If a test breaks during refactor, undo and take smaller steps.

### Repeat

Next failing test for the next behavior. One cycle at a time.

## Why order matters

Tests written after the code pass immediately — that proves nothing. They may test the wrong thing, miss edge cases, or test the implementation instead of the contract. Test-first forces you to see the test fail before the feature exists, which is the only way to know the test actually catches the regression you care about.

## Verification checklist

Before claiming work complete:

- [ ] Every new function/method has a test.
- [ ] You watched each test fail before implementing.
- [ ] Each test failed for the *expected* reason (feature missing, not typo).
- [ ] You wrote the minimum code to pass each test.
- [ ] Whole suite passes — output pristine, no new warnings.
- [ ] Edge cases and error paths covered.

Can't tick every box? You skipped TDD. Start over.

## Related skills

- `systematic-debugging` — pair with TDD when fixing a bug: failing reproducer first, then fix.
- `verification-before-completion` — confirm the suite genuinely passed before claiming done.
- `subagent-driven-development` — when delegating, put "follow TDD" in the subagent's goal.
