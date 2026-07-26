---
name: memory
description: Persist short notes for the current session and retrieve them later via the vector store.
metadata:
  openclaw:
    emoji: "🧠"
    requires:
      bins: []
      anyBins: []
      config: []
      env: []
    install: |
      No installation needed. The skill uses the built-in `memory_write`
      and `memory_search` tools backed by the corlinman vector service,
      which is always available in-process.
allowed-tools:
  - memory_write
  - memory_search
---
# Memory

Give the agent a working memory that survives across turns in the same session
and can be recalled on demand later in the conversation.

## When to use

- The user stated a **fact about themselves or their project** that will stay
  relevant for the rest of the conversation (e.g. "I deploy to k8s, never
  docker-compose", "our code style uses tabs").
- The user finished a multi-step decision and you want to pin the outcome so
  the next turn doesn't relitigate it.
- A long reasoning chain produced an intermediate result (a SQL query, a
  regex, a JSON schema) that future turns are likely to reference.

## When NOT to use

- For trivia that only matters for the current reply — just answer and move on.
- For secrets (passwords, API keys, PII). Memory is persisted; do not store
  anything you would not write to a log file.
- As a substitute for the conversation history. If the user literally just
  said it, you don't need to re-save it.

## Workflow

1. **Write**: call `memory_write` with:
   - `content`: the fact as one standalone sentence ("User deploys to k8s,
     never docker-compose").
   - `tag`: optional category ("profile", "project", "preference").
   - `namespace`: optional topic to organize related notes.
2. **Search**: before answering a question that might depend on prior state,
   call `memory_search` with a natural-language query and `top_k = 3`.
3. **Use naturally**: let a retrieved note shape your answer, and refer to
   what the user previously said when it helps ("既然你们部署在 k8s 上…").
   Do not announce the memory system itself or that you are "recalling
   stored memory".

## Hygiene

- Prefer one note per concept; long blobs are harder to retrieve cleanly.
- When a fact changes, write the updated fact as a new note phrased to
  supersede the old one ("User now deploys with docker-compose, not k8s").
- If the user says "forget that", write a correcting note stating the fact
  no longer holds, and stop using the old one.
