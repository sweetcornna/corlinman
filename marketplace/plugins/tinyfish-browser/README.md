# tinyfish-browser

TinyFish search and rendered-page fetch plugin for corlinman. It exposes:

- `tinyfish_search`
- `tinyfish_fetch`

Author: `Skye`
Version: `1.0.0`

## What It Does

- Web search with structured results
- Rendered page fetching for JavaScript-heavy sites
- Multi-URL fetch for up to 10 pages
- Optional extraction of page links and image links

## Configuration

Set these environment variables before enabling the plugin:

```env
TINYFISH_API_KEY=your_api_key_here
DebugMode=false
```

Notes:

- `TINYFISH_API_KEY` is required.
- `DebugMode=true` will print extra logs to stderr.

## Tool Usage

### `tinyfish_search`

```json
{
  "query": "AI agent tools 2026",
  "location": "US",
  "language": "en",
  "page": 0,
  "thumbnails": false
}
```

### `tinyfish_fetch`

```json
{
  "urls": ["https://example.com", "https://example.org"],
  "format": "markdown",
  "links": true,
  "image_links": false,
  "include_html_head": false
}
```

## Testing

Quick syntax check:

```bash
node --check marketplace/plugins/tinyfish-browser/tinyfish_browser.js
```

Project validation:

```bash
python3 marketplace/scripts/build-registry.py
python3 marketplace/scripts/validate-index.py
```
