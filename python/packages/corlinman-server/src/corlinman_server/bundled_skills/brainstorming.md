---
name: brainstorming
description: Turn an idea into a validated design via collaborative dialogue. Use BEFORE any creative or implementation work.
metadata:
  openclaw:
    emoji: "💡"
    requires:
      bins: []
      anyBins: []
      config: []
      env: []
    install: |
      No installation needed. The skill is procedural — a structured
      dialogue ending in a saved design doc under `docs/specs/`.
allowed-tools:
  - read_file
  - write_file
---
# Brainstorming Ideas Into Designs

Turn ideas into fully formed designs through natural collaborative dialogue.

Start by understanding the current project context, then ask one question at a time to refine the idea. Once you understand what you're building, present the design and get user approval *before* anything is built.

## HARD GATE

Do not invoke any implementation skill, write any code, or scaffold any project until you have presented a design and the user has approved it. This applies to **every** project, regardless of perceived simplicity.

## Anti-pattern: "this is too simple to need a design"

Every project goes through this. A todo list, a single-function utility, a config change — all of them. "Simple" projects are where unexamined assumptions cause the most wasted work. The design can be one paragraph for a truly simple project, but you must present it and get approval.

## Checklist

Track each item; complete in order.

1. **Explore project context** — files, docs, recent commits.
2. **Ask clarifying questions** — one at a time. Understand purpose, constraints, success criteria.
3. **Propose 2–3 approaches** — with trade-offs and your recommendation (lead with the recommended one, explain why).
4. **Present the design** — in sections sized to their complexity. Get approval after each section.
5. **Write the design doc** — save to `docs/specs/YYYY-MM-DD-<topic>-design.md` and commit.
6. **Spec self-review** — inline pass for placeholders, contradictions, ambiguity, scope.
7. **User reviews written spec** — pause and ask for sign-off before proceeding.
8. **Transition to `writing-plans`** — that is the *only* next step.

## Process flow

```
Explore project context
       │
       ▼
Ask clarifying questions (one at a time)
       │
       ▼
Propose 2-3 approaches
       │
       ▼
Present design sections ──no──► revise ──┐
       │                                 │
       │approved                         │
       ▼                                 │
Write design doc ◄───────────────────────┘
       │
       ▼
Spec self-review (fix inline)
       │
       ▼
User reviews ──changes requested──► revise
       │
       │approved
       ▼
Invoke `writing-plans` skill
```

The terminal state is `writing-plans`. Do not jump into implementation directly.

## Understanding the idea

- Check the current project state first (files, docs, recent commits).
- Assess scope: if the request describes multiple independent subsystems (e.g. "build a platform with chat, file storage, billing, analytics"), flag this immediately. Don't refine details of something that needs to be decomposed first.
- If too large for one spec, help the user decompose into sub-projects. Each gets its own spec → plan → implementation cycle.
- For appropriately-scoped projects, ask questions one at a time.
- Prefer multiple-choice questions when possible. Open-ended is fine when it isn't.
- Only one question per message.

## Presenting the design

- Scale each section to its complexity: a few sentences for straightforward, up to 200–300 words for nuanced.
- Ask after each section whether it looks right before continuing.
- Cover: architecture, components, data flow, error handling, testing.
- Be ready to go back and clarify if something doesn't make sense.

## Design for isolation and clarity

- Break the system into units that each have one clear purpose, communicate through well-defined interfaces, and can be understood and tested independently.
- For each unit you should be able to answer: what does it do, how do you use it, what does it depend on?
- Smaller, well-bounded units are easier to reason about — when a file grows large, that's a signal it's doing too much.

## Working in existing codebases

- Explore the current structure before proposing changes. Follow existing patterns.
- Where existing code has problems that affect the work (oversized files, unclear boundaries, tangled responsibilities), include targeted improvements as part of the design.
- Do not propose unrelated refactoring. Stay focused on what serves the current goal.

## Spec self-review (after writing the doc)

1. **Placeholder scan** — any TBD, TODO, vague requirements? Fix them.
2. **Internal consistency** — sections don't contradict? Architecture matches feature descriptions?
3. **Scope check** — focused enough for a single implementation plan, or needs decomposition?
4. **Ambiguity check** — could any requirement be read two ways? Pick one, make it explicit.

Fix issues inline. No need to re-review — fix and move on.

## User-review gate

> "Spec written and committed to `<path>`. Please review it and tell me if you want to change anything before we move to the implementation plan."

Wait for the user's response. If changes are requested, make them and re-run the self-review. Only proceed once the user approves.

## Key principles

- **One question at a time.**
- **Multiple-choice preferred** when feasible.
- **YAGNI ruthlessly** — remove unnecessary features from every design.
- **Always 2–3 alternatives** before settling.
- **Incremental validation** — section by section.
- **Be flexible** — go back when something doesn't add up.

## Related skills

- `writing-plans` — the only skill brainstorming hands off to.
- `note-taking` — capture in-flight decisions before the spec is final.
