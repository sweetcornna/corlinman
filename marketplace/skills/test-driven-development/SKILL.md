---
name: test-driven-development
description: "Use when implementing any feature or bugfix, before writing implementation code"
emoji: ✅
---

The Iron Law: no production code without a failing test first.

Red-Green-Refactor:
1. RED — write the smallest test that captures the next bit of behavior and
   watch it fail for the right reason.
2. GREEN — write the minimum code to make it pass.
3. REFACTOR — clean up with the test as a safety net.

Rejected rationalizations: "it's too simple to test", "I'll add tests after",
"the test is obvious". Each is a trap — write the test first anyway. One
behavior per test; descriptive test names; keep tests fast and independent.

---
Source: github.com/obra/superpowers (MIT). Condensed + attributed for the corlinman marketplace; see ../../ATTRIBUTION.md.
