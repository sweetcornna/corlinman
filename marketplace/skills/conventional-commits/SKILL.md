---
name: conventional-commits
description: "Write git commit messages that follow the Conventional Commits v1.0.0 specification."
emoji: 🔖
---

Format: `<type>(<optional scope>): <description>` then an optional body and
footers.

Types: feat, fix, docs, style, refactor, perf, test, build, ci, chore, revert.

Rules:
- Description in the imperative mood ("add", not "added"), <= 72 chars, no
  trailing period.
- A breaking change is marked with `!` after the type/scope AND/OR a
  `BREAKING CHANGE:` footer explaining the break.
- Body explains the what/why (not the how); wrap at ~72 columns.
- One logical change per commit.

Examples:
  feat(auth): add OAuth2 device-code login
  fix(api): reject negative page sizes (closes #214)
  refactor(store)!: drop the legacy sync API

  BREAKING CHANGE: `Store.sync()` is removed; use `await Store.async()`.

---
Source: github.com/inprojectspl/conventional-commits (MIT). Condensed + attributed for the corlinman marketplace; see ../../ATTRIBUTION.md.
