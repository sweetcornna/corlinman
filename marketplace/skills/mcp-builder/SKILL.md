---
name: mcp-builder
description: "Guide for creating high-quality MCP (Model Context Protocol) servers that enable LLMs to interact with external services through well-designed tools. Use when building MCP servers to integrate external APIs or services, whether in Python (FastMCP) or Node/TypeScript (MCP SDK)."
emoji: 🔌
---

Judge an MCP server by whether its tools help an LLM accomplish real tasks.

Four phases:
1. Research & plan — study the MCP spec and the SDK (Python FastMCP or
   Node/TS), analyze the target API, and balance broad coverage vs. a few
   specialized workflow tools.
2. Implement — set up the project, then core infra (API client, error
   handling, response formatting), then each tool with input/output schemas,
   clear descriptions, good error messages, and annotations (read-only /
   destructive / idempotent / open-world).
3. Review & test — remove duplication, cover errors and types, build, and
   exercise the server via the MCP Inspector.
4. Evals — write ~10 complex, independent, read-only, verifiable questions and
   run them against the server.

Prefer fewer high-leverage tools with great descriptions over a thin 1:1
wrapper of every API endpoint.

---
Source: github.com/anthropics/skills (Apache-2.0). Condensed + attributed for the corlinman marketplace; see ../../ATTRIBUTION.md.
