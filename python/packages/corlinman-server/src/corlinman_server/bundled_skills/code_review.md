---
name: code_review
description: Review a git diff or a pull request branch and produce prioritized, actionable review comments.
metadata:
  openclaw:
    emoji: "🧐"
    requires:
      bins:
        - "git"
      anyBins: []
      config: []
      env: []
    install: |
      1. Ensure `git` is on PATH (`git --version` must print a version string).
      2. Run the agent from inside the repository you want reviewed, or pass
         the repo path via the `read_file` tool.
      3. No API keys required — the skill uses only local git + in-model
         reasoning. Remote review services would live in a separate skill.
allowed-tools:
  - read_file
  - run_shell
---
# Code Review

Review a git diff or a branch against its merge-base and produce review
comments ordered by **impact**, not by file order.

## When to use

- The user asked for a review of a branch, a PR, or an uncommitted change.
- The user pasted a diff and asked "does this look right".
- A scheduled job wants a second pair of eyes on an auto-generated PR.

## What to look for (in this priority)

1. **Correctness** — off-by-one, null handling, concurrency hazards, resource
   leaks, misuse of an API. These are the only comments that must block merge.
2. **Security** — injection sinks, missing auth checks, secrets in diffs,
   dependency pins that pull in CVEs.
3. **Tests** — new behavior without a new test is a P1 comment; changed
   behavior without an updated test is a P2 comment.
4. **Readability** — surprising names, dead parameters, TODOs without owners.
   These are suggestions, not blockers.
5. **Style** — only mention if it violates the project's formatter/lint
   config. Never re-litigate taste.

## Workflow

1. `git diff --merge-base origin/main` — produce the canonical review diff.
2. `git log --oneline origin/main..HEAD` — read commit messages; a good
   message explains the "why" that the diff itself cannot.
3. Identify the **2–5 highest-impact issues**, each with file path + line
   number + a concrete suggested fix.
4. Write the final summary as:
   - `BLOCKERS:` bullet list (correctness + security only)
   - `SUGGESTIONS:` bullet list (tests, readability)
   - `NITS:` bullet list (style) — may be empty; skip the section if so.

## Self-checks before replying

- Did every blocker quote the exact offending line?
- Did I avoid vague advice like "consider refactoring"?
- Did I suggest a concrete change for each blocker, not just a complaint?
