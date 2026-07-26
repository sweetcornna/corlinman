---
name: receiving-feedback
description: Receive code-review, design-review, or planning feedback. Verify before implementing; ask before assuming; technical rigor over performative agreement.
metadata:
  openclaw:
    emoji: "👂"
    requires:
      bins: []
      anyBins: []
      config: []
      env: []
    install: |
      No installation needed. The skill is procedural — a response
      discipline that uses whatever read tools the active agent already
      has to verify the feedback before acting.
allowed-tools:
  - read_file
  - run_shell
---
# Receiving Feedback

## Overview

Feedback — code review, design review, planning critique — requires technical evaluation, not emotional performance.

**Core principle:** verify before implementing; ask before assuming; technical correctness over social comfort.

## The response pattern

```
WHEN receiving feedback:
  1. READ — complete feedback without reacting.
  2. UNDERSTAND — restate the requirement in your own words (or ask).
  3. VERIFY — check against the actual codebase / design / plan reality.
  4. EVALUATE — is it technically sound for THIS context?
  5. RESPOND — technical acknowledgement or reasoned pushback.
  6. IMPLEMENT — one item at a time; verify each.
```

## Forbidden responses

- "You're absolutely right!"
- "Great point!" / "Excellent feedback!"
- "Let me implement that now" — before any verification.
- "Thanks for catching that!" — any gratitude expression.

These are performative. The code itself shows you heard the feedback — actions over words.

## Handling unclear feedback

```
IF any item is unclear:
  STOP — implement nothing yet.
  ASK for clarification on the unclear items.
```

Items may be related. Partial understanding = wrong implementation. If you understand items 1, 2, 3, 6 but not 4, 5 — say so. Do NOT implement 1, 2, 3, 6 first and ask about 4, 5 later.

## Source-specific handling

### From a trusted human partner

- Implement after understanding.
- Still ask if scope is unclear.
- No performative agreement — skip to action or technical acknowledgement.

### From an external reviewer (CI bot, third-party PR review, AI reviewer)

Before implementing, verify:

1. Is it technically correct *for this codebase*?
2. Does it break existing functionality?
3. Is there a reason for the current implementation?
4. Does it work on all platforms/versions you support?
5. Does the reviewer understand the full context?

If the suggestion seems wrong: push back with technical reasoning, not defensiveness.

If you can't easily verify: say so — "I can't verify this without `<command>`. Should I investigate, ask, or proceed?"

## YAGNI check for "professional" suggestions

```
IF reviewer says "implement properly":
  grep / run_shell "rg <symbol>" — find actual usage.

  IF unused: "This isn't called anywhere. Remove it (YAGNI)?"
  IF used:   then implement properly.
```

## Implementation order for multi-item feedback

1. Clarify anything unclear FIRST.
2. Then implement in this order:
   - Blocking issues (breaks, security).
   - Simple fixes (typos, imports).
   - Complex fixes (refactoring, logic).
3. Verify each fix individually.
4. Run the full test suite after the last fix to catch regressions.

## When to push back

- The suggestion breaks existing functionality.
- The reviewer lacks full context.
- It violates YAGNI.
- It's technically incorrect for this stack.
- There are legacy/compatibility reasons.
- It conflicts with the user's prior architectural decisions.

How to push back: technical reasoning + specific questions + reference working tests/code. Involve the user if the disagreement is architectural.

## Acknowledging correct feedback

```
✅ "Fixed. <brief description of the change>"
✅ "Good catch — <specific issue>. Fixed in <location>."
✅ [Just fix it and show in the diff.]

❌ "You're absolutely right!" / "Great point!" / any thanks expression.
```

## When you pushed back and were wrong

```
✅ "You were right — I checked <X> and it does <Y>. Implementing now."
✅ "Verified — my initial reading was wrong because <reason>. Fixing."

❌ Long apology. Defending why you pushed back. Over-explaining.
```

State the correction factually and move on.

## Common mistakes

| Mistake | Fix |
|---------|-----|
| Performative agreement | State the requirement or just act. |
| Blind implementation | Verify against the codebase first. |
| Batch fixes without testing | One at a time; verify each. |
| Assuming reviewer is right | Check whether it breaks things. |
| Avoiding pushback | Technical correctness over comfort. |
| Partial implementation while unclear | Clarify all items first. |
| Can't verify but proceeding | State the limitation; ask for direction. |

## Bottom line

External feedback = suggestions to evaluate, not orders to follow. Verify. Question. *Then* implement. No performative agreement.

## Related skills

- `code_review` / `requesting-code-review` — the opposite side of this conversation.
- `verification-before-completion` — every "I implemented it" claim runs through this skill.
