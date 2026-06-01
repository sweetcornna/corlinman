---
name: brainstorming
description: "You MUST use this before any creative work - creating features, building components, adding functionality, or modifying behavior. Explores user intent, requirements and design before implementation."
emoji: 💡
---

Hard rule: do NOT write code, scaffold, or invoke any implementation skill until you have presented a design and the user has approved it.

Process:
1. Explore the project context (read the relevant code/docs first).
2. Ask clarifying questions ONE at a time — prefer multiple-choice options.
3. Propose 2-3 distinct approaches with trade-offs, and recommend one.
4. Present the design section by section (architecture, components, data flow,
   error handling, testing), getting approval on each before moving on.
5. Write the agreed design to `docs/specs/YYYY-MM-DD-<topic>-design.md`.
6. Self-review the spec: remove placeholders, fix contradictions/ambiguity,
   check scope. Flag multi-subsystem scope for decomposition.
7. Have the user review the written spec — this is an approval gate.

Principles: apply YAGNI ruthlessly, validate incrementally, and give even
"simple" projects a short design pass before building.

---
Source: github.com/obra/superpowers (MIT). Condensed + attributed for the corlinman marketplace; see ../../ATTRIBUTION.md.
