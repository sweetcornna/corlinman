---
name: git-worktrees
description: Create an isolated git worktree for feature work, with smart directory selection and gitignore safety verification.
metadata:
  openclaw:
    emoji: "🌿"
    requires:
      bins:
        - "git"
      anyBins: []
      config: []
      env: []
    install: |
      1. Ensure `git` is on PATH (`git --version` must print a version).
      2. Run the skill from inside the repository you want to branch from.
allowed-tools:
  - read_file
  - run_shell
---
# Using Git Worktrees

## Overview

Git worktrees create isolated workspaces sharing the same repository, so you can work on multiple branches simultaneously without `git checkout` thrash.

**Core principle:** systematic directory selection + gitignore safety = reliable isolation.

## When to use

- Before starting feature work that must not pollute the current workspace.
- Before executing an implementation plan via `executing-plans` or `subagent-driven-development`.
- Any time a sub-task needs to mutate files without disturbing the main checkout.

## Directory selection (priority order)

1. **Check for an existing worktree dir.**
   ```
   ls -d .worktrees 2>/dev/null
   ls -d worktrees  2>/dev/null
   ```
   If both exist, `.worktrees/` wins. If one exists, use it.

2. **Check CLAUDE.md / project docs for a worktree preference.** If a preference is recorded, use it without asking.

3. **Ask the user** if no directory exists and no preference is recorded:
   > "No worktree directory found. Where should I create worktrees? (1) `.worktrees/` project-local, (2) `~/.corlinman/worktrees/<project>/` global."

## Safety verification

### Project-local (`.worktrees/` or `worktrees/`)

The directory **must** be gitignored before the worktree is created. Otherwise the worktree's files will be tracked in the parent checkout.

```
git check-ignore -q .worktrees || git check-ignore -q worktrees
```

If not ignored: add the path to `.gitignore`, commit the change, *then* create the worktree.

### Global (`~/.corlinman/worktrees/`)

No gitignore verification needed — it lives outside the project.

## Creation steps

1. **Detect project name.**
   ```
   project=$(basename "$(git rev-parse --show-toplevel)")
   ```

2. **Create the worktree.**
   ```
   git worktree add "<location>/<branch>" -b "<branch>"
   cd "<location>/<branch>"
   ```

3. **Run project setup.** Auto-detect by manifest:
   - `package.json` → `pnpm install` / `npm install`
   - `Cargo.toml`   → `cargo build`
   - `pyproject.toml` → `uv sync` / `poetry install`
   - `go.mod`       → `go mod download`

4. **Verify clean baseline.** Run the project test command. If tests fail, report the failures and ask before proceeding — you cannot distinguish your new bugs from pre-existing ones otherwise.

5. **Report.**
   ```
   Worktree ready at <full-path>
   Tests passing (<N> tests, 0 failures)
   Ready to implement <feature-name>
   ```

## Quick reference

| Situation | Action |
|-----------|--------|
| `.worktrees/` exists | Use it (verify ignored) |
| `worktrees/` exists | Use it (verify ignored) |
| Both exist | Use `.worktrees/` |
| Neither exists | Check project docs → ask user |
| Directory not ignored | Add to `.gitignore` + commit |
| Baseline tests fail | Report failures + ask before proceeding |

## Common mistakes

- **Skipping ignore verification** — worktree contents pollute `git status`. Always `git check-ignore` first.
- **Hardcoding setup commands** — auto-detect from manifest files.
- **Proceeding with failing baseline tests** — you'll mis-attribute later failures.

## Related skills

- `executing-plans` — calls this skill before starting a plan.
- `subagent-driven-development` — calls this skill before fan-out.
