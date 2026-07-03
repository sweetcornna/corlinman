---
name: web_search
description: Search the public web (keyless DuckDuckGo by default; Brave/SerpApi optional) and fetch individual pages for follow-up reading.
metadata:
  openclaw:
    emoji: "🔍"
    requires:
      bins: []
      anyBins: []
      config: []
      env: []
    install: |
      Works out of the box — `web.search` defaults to the keyless DuckDuckGo
      backend, so no API key is required.

      Optional higher-volume / higher-quality backends:
      - Brave: create a key at https://brave.com/search/api/ (free tier ~2k
        queries/mo), then
            corlinman config set providers.brave.api_key '{ env = "BRAVE_API_KEY" }'
        export `BRAVE_API_KEY`, and restart the gateway.
      - SerpApi: set `CORLINMAN_WEB_SEARCH_API_KEY` (or pick a backend with
        `CORLINMAN_WEB_SEARCH_BACKEND`).
allowed-tools:
  - web.search
  - web.fetch
---
# Web Search

Use the `web.search` tool when you need information from the public internet that
the model could not have in its training data, or that is likely to have changed.
For deep reads, chain a `web.fetch` call on the most promising result URL.

## When to use

- Questions about events, releases, or prices from the **past 12 months**.
- Technical lookups where the canonical source (RFC, API docs, release notes,
  GitHub README) is more authoritative than the model's recollection.
- Fact-checking a claim the user disputed, before taking a side.

## When NOT to use

- The user asked you to reason, not to research. Reaching for search is a tell
  that you are offloading thinking — only do it when external data is the gap.
- The answer is already present in the attached files, RAG context, or session
  memory. Search those first; the web is the last resort.
- The query involves sensitive internal terms (customer names, unreleased
  project codenames). Those must not leak to a third-party API.

## How to use

1. Call `web.search` with a focused query (≤ 8 keywords). Prefer concrete
   nouns over questions — search engines reward keywords, not grammar.
2. Read the titles and snippets returned. Pick the one or two links that
   look most authoritative (official docs > blog posts > forum answers).
3. For each chosen URL, call `web.fetch` to retrieve the page body, then
   quote the specific passage that answers the question.
4. In your final reply, cite the URL inline: `[source](https://...)`.

## Failure modes to anticipate

- **Rate limit**: Brave returns `429`. Back off 30 s and retry once; if it
  fails again, surface the error rather than silently producing a guess.
- **Empty results**: the query was too narrow. Broaden one keyword and
  retry; do not fabricate a result.
- **Paywalled pages**: `web.fetch` returns the paywall stub, not the body.
  Note this in your reply and move to the next result.
