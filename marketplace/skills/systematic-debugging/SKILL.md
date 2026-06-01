---
name: systematic-debugging
description: "Use when encountering any bug, test failure, or unexpected behavior, before proposing fixes"
emoji: 🐛
---

Core rule: NO FIXES WITHOUT ROOT-CAUSE INVESTIGATION FIRST. Symptom fixes mask
the real issue and cause rework.

Four phases:
1. Root cause — read the full error + stack trace, reproduce it reliably,
   review recent changes, instrument boundaries in multi-component systems,
   and trace the bad value backward to its origin.
2. Pattern analysis — find a working example in the same codebase, read the
   reference implementation fully, and list every difference vs. the broken path.
3. Hypothesis & testing — form ONE specific hypothesis, change ONE variable at
   a time, and verify before continuing. Form a new hypothesis instead of
   stacking fixes.
4. Implementation — write a failing test first, make one root-cause change,
   then confirm nothing else broke.

Red flags (go back to phase 1): proposing a fix before investigating, changing
several things at once, skipping the test, or attempting a 4th fix after 3
failures (3 failures = an architectural problem — discuss, don't keep patching).

---
Source: github.com/obra/superpowers (MIT). Condensed + attributed for the corlinman marketplace; see ../../ATTRIBUTION.md.
