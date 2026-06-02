# any-search

Real-time search plugin for corlinman, backed by AnySearch. It exposes four tools:

- `any_search_search`
- `any_search_list_domains`
- `any_search_batch_search`
- `any_search_extract`

Author: `Skye`
Version: `1.0.0`

## What It Does

- General web search with optional freshness, content type, and region filters
- Vertical-domain search for areas like finance, academic, security, and code
- Batch search for up to 5 queries at once
- URL content extraction

## Configuration

Set these environment variables before enabling the plugin:

```env
ANYSEARCH_API_KEY=
ANYSEARCH_ENDPOINT=https://api.anysearch.com/mcp
ANYSEARCH_TIMEOUT_MS=30000
```

Notes:

- `ANYSEARCH_API_KEY` is optional. Anonymous access works but has lower limits.
- You may provide multiple API keys separated by commas.
- `ANYSEARCH_TIMEOUT_MS` is clamped to `1000-120000`.

## Tool Usage

### `any_search_search`

Required:

```json
{ "query": "AI regulation 2026" }
```

Optional fields:

- `domain`
- `sub_domain`
- `sub_domain_params`
- `content_types`
- `zone`
- `freshness`
- `max_results`

### `any_search_list_domains`

Examples:

```json
{}
```

```json
{ "domain": "finance" }
```

```json
{ "domains": ["finance", "academic"] }
```

### `any_search_batch_search`

```json
{
  "queries": ["AI agents", "LLM safety", "transformer architecture"]
}
```

### `any_search_extract`

```json
{
  "url": "https://example.com/article"
}
```

## Testing

Quick syntax check:

```bash
node --check marketplace/plugins/any-search/any_search.js
```

Project validation:

```bash
python3 marketplace/scripts/build-registry.py
python3 marketplace/scripts/validate-index.py
```
