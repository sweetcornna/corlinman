# v-search

Concurrent semantic research plugin for corlinman. It exposes:

- `v_search_research`

Author: `Skye`
Version: `1.0.0`

## What It Does

- Runs concurrent keyword research for one topic
- Supports `grounding`, `grok`, `tavily`, and `kimisearch` modes
- Can synthesize multi-result output into a research-style summary

## Important Runtime Note

This plugin depends on npm packages and they are not bundled into the marketplace tarball automatically.

Before enabling it, install dependencies in the plugin directory:

```bash
cd marketplace/plugins/v-search
npm install
```

If dependencies are missing, the plugin returns a clear runtime error instead of crashing.

## Configuration

Common settings:

```env
SearchMode=kimisearch
MaxConcurrent=5
VSearchMaxToken=50000
```

Grounding / Grok mode:

```env
VSearchKey=sk-YourAPIKeyHere
VSearchUrl=http://YourApiServerUrl/v1/chat/completions
VSearchModel=gemini-2.5-flash-lite-preview-09-2025-thinking
GrokModel=grok-4.20-beta
```

Tavily mode:

```env
TavilyKey=tvly-YourTavilyKeyHere
TavilyModel=gpt-5.4
```

Kimi Search mode:

```env
KimiSearchUrl=https://api.kimi.com/coding/v1
KimiSearchKey=sk-YourKimiKeyHere
KimiSearchMaxResults=5
KimiSearchIncludeContent=false
```

Optional summary-model override:

```env
SummaryKey=sk-YourSummaryAPIKey
SummaryUrl=https://api.openai.com/v1/chat/completions
SummaryModel=gpt-5.4
```

Optional proxy:

```env
HTTP_PROXY=http://127.0.0.1:7890
```

## Tool Usage

```json
{
  "SearchTopic": "探讨 AI 在医疗诊断中的最新应用",
  "Keywords": "AI medical diagnosis 2024, 深度学习 医疗影像 突破, transformer models in healthcare",
  "SearchMode": "kimisearch",
  "ShowURL": false
}
```

## Testing

Quick syntax check:

```bash
node --check marketplace/plugins/v-search/v_search.js
```

Install deps and validate:

```bash
cd marketplace/plugins/v-search
npm install
cd /Users/Zhuanz/project/corlinman
python3 marketplace/scripts/build-registry.py
python3 marketplace/scripts/validate-index.py
```
