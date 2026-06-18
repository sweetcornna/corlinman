---
name: deep-research
description: Multi-source investigation — decompose the question, fan out searches, cross-check sources, synthesise an evidence-backed answer with citations.
metadata:
  openclaw:
    emoji: "🔎"
    requires:
      bins: []
      anyBins: []
      config: []
      env: []
    install: |
      1. Works out of the box — `web.search` defaults to the keyless
         DuckDuckGo backend, so no API key is required. For higher
         volume / quality, optionally configure Brave (see the
         `web_search` skill) or set `CORLINMAN_WEB_SEARCH_BACKEND` /
         `CORLINMAN_WEB_SEARCH_API_KEY` (SerpApi).
      2. The skill works without `subagent.*` access (single-threaded
         fallback), but quality is dramatically higher when the agent
         can fan out via `subagent_spawn_many`.
allowed-tools:
  - web.search
  - web.fetch
  - kb.search
  - memory.search
  - memory.write
  - subagent_spawn
  - subagent_spawn_many
  - blackboard.read
  - blackboard.write
---
# Deep Research

## Overview

A question that crosses 3+ sources, requires reconciling conflicting claims, or needs primary-source citations should not be answered from the model's recollection alone. Deep research is the structured discipline for those questions.

**Core principle:** evidence beats fluency. A confident wrong answer is worse than "I don't know — here are the three primary sources and where they disagree."

## When to use

- Comparative questions ("X vs Y for use-case Z").
- Anything time-sensitive ("what's the latest on X?", "what changed in version N?").
- Claim-validation ("the user said X — is that actually true?").
- Background scoping before a planning skill (`plan`, `writing-plans`).

## When NOT to use

- The user asked you to reason, not to research — search is offloading thinking.
- The answer is in the attached files / RAG context / session memory. Search those first via `kb.search` and `memory.search`.
- Queries involve sensitive internal terms (customer names, unreleased codenames) that must not leak to third parties.

## Process

### 1. Decompose the question

Write down 2–5 sub-questions you need to answer. Each sub-question should be independently searchable and have a clear "answered when…" criterion.

Example — "Should we adopt postgres logical replication?":
- What does postgres logical replication actually do?
- What are the production failure modes other teams report?
- How does it compare to physical replication for our workload?

### 2. Search internal sources first

```
kb.search("logical replication corlinman", top_k=5)
memory.search("postgres replication previous decision", top_k=3)
```

Anything found here saves a web round-trip *and* surfaces prior decisions that are usually more authoritative than blog posts.

### 3. Fan out web searches (one per sub-question)

If `subagent_spawn_many` is available, dispatch one researcher sibling per sub-question — siblings are isolated, which prevents one early "official-looking" source from anchoring the others:

```
subagent_spawn_many(
  agent="researcher",
  goals=[
    "Sub-question 1: ...",
    "Sub-question 2: ...",
    "Sub-question 3: ...",
  ],
  extra_context={"return_format": "cited summary, ≤200 words each"}
)
```

Without subagents: run the web.search calls sequentially yourself.

### 4. Read primary sources, not just snippets

For each sub-question, pick the 1–2 most authoritative results — official docs, RFCs, release notes, the repo's own README — and `web.fetch` the full page. Do not synthesise from snippet text alone; snippets are designed to *look* relevant, not to *be* complete.

Source hierarchy:
- official docs / RFCs / standards
- maintainer changelog / release notes
- vendor engineering blog
- third-party blog by recognisable practitioner
- forum answers, Reddit, Stack Overflow

### 5. Reconcile conflicts explicitly

If two sources disagree, name both, name what they disagree about, and pick one with a *reason*. Do not silently average. Common conflict patterns:

- Different versions (one source is talking about v1, the other v2).
- Different scale (works fine at 10 QPS, breaks at 10k).
- Different deployment model (self-hosted vs SaaS).

### 6. Synthesise with citations

The final answer cites each non-obvious claim inline as `[source](https://...)`. Aim for 3–7 citations for a meaningful research question. Zero citations on a research question = you didn't research.

### 7. Persist non-obvious findings

If the answer is likely to matter for the rest of the conversation (or a later one), write it to memory:

```
memory.write(
  key="research.logical_replication.verdict",
  value="Recommended for our workload because <one sentence>; main risk = <one sentence>.",
  tags=["research", "postgres", "replication"]
)
```

## Failure modes

- **Snippet-only synthesis** — looks confident, gets details wrong. Fetch the page.
- **Anchoring on the first source** — fan out *before* reading deeply.
- **Hidden conflicts** — if every source says the same thing in slightly different words, that's usually one upstream source being recycled. Look for the original.
- **Rate-limit silent fallback** — if web.search returns nothing, surface that — do not invent a result.
- **Paywall** — note "paywalled; couldn't read body" and move to the next result; do not paraphrase from the snippet.

## Output shape

```
## TL;DR
<one or two sentences, the answer>

## What I checked
- <sub-question> → <result>
- <sub-question> → <result>

## Disagreements (if any)
- <Source A> says X; <Source B> says Y; I picked X because <reason>.

## Citations
[1] <title> — https://...
[2] ...
```

## Related skills

- `web_search` — the simpler one-shot version when you only need one fact.
- `note-taking` — capture intermediate findings during a long research session.
- `brainstorming` — research often feeds into design; hand off when scope clarifies.
- `plan` / `writing-plans` — the implementation cycle after research concludes.
