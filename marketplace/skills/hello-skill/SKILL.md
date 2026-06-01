---
name: hello-skill
description: A minimal example skill used to seed and smoke-test the corlinman marketplace registry. Use when you need a tiny, known-good skill to verify install/extraction wiring end to end, or as a template for authoring a real SKILL.md.
emoji: "👋"
---

# Hello Skill

A deliberately tiny skill that exists to prove the marketplace install path
works: catalog → tarball download → sha256 verify → extract `<slug>/SKILL.md`.

## When to use

- You are wiring up or debugging the marketplace `GitHubSource` and want a
  known-good, reproducible item to install.
- You want a starter template for a real skill. Copy this directory, rename
  the slug, and replace the body with your own instructions.

## What it does

Nothing operational — it is documentation only. When invoked it simply greets
the user and points them at the registry build instructions in the repo
`README.md`.

## Authoring notes

- The YAML frontmatter must declare `name` and `description`. `emoji` is
  optional and mirrors the `emoji` field surfaced in `index.json`.
- Bump the version by repackaging via `scripts/build-registry.py`, which keeps
  the `index.json` sha256 in sync with the tarball bytes.
