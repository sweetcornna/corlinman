# zhihu-search

Zhihu OpenAPI search plugin for corlinman. It exposes:

- `zhihu_site_search`
- `zhihu_global_search`

Author: `Skye`
Version: `1.0.0`

## What It Does

- Searches Zhihu site content such as answers and articles
- Runs broader Zhihu global search
- Returns both Markdown-friendly summaries and structured metadata

## Configuration

Set these environment variables before enabling the plugin:

```env
ZHIHU_ACCESS_SECRET=your_access_secret_here
ZHIHU_OPENAPI_BASE_URL=https://developer.zhihu.com
ZHIHU_ZHIHU_SEARCH_URL=
ZHIHU_GLOBAL_SEARCH_URL=
ZHIHU_SEARCH_TIMEOUT_SECONDS=5
```

Notes:

- `ZHIHU_ACCESS_SECRET` is required.
- `ZHIHU_SEARCH_TIMEOUT_SECONDS` is clamped to `1-60`.
- You can override either endpoint directly if Zhihu changes its API paths.

## Tool Usage

### `zhihu_site_search`

```json
{
  "query": "AI Agent 应用实践",
  "count": 5
}
```

### `zhihu_global_search`

```json
{
  "query": "如何理解 rave 文化",
  "count": 8
}
```

## Output Shape

Both tools return JSON with fields like:

- `search_type`
- `code`
- `api_message`
- `item_count`
- `items`
- `sources`
- `content`
- `message`

## Testing

Quick syntax check:

```bash
python3 -m compileall marketplace/plugins/zhihu-search/zhihu_search.py
```

Project validation:

```bash
python3 marketplace/scripts/build-registry.py
python3 marketplace/scripts/validate-index.py
```
