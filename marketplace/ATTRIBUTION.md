# Attribution & licenses (seed content)

The corlinman marketplace is seeded with content drawn from the open agent
ecosystem. We credit the upstreams here and keep each item's license intact.

## Skills

Each seeded `SKILL.md` is a **condensed, attributed adaptation** of an
upstream skill (frontmatter `description` reproduced verbatim; body faithfully
condensed). Per-file footers link back here.

| Skill | Upstream | License |
|---|---|---|
| brainstorming, systematic-debugging, writing-plans, test-driven-development | [obra/superpowers](https://github.com/obra/superpowers) | MIT |
| conventional-commits | [inprojectspl/conventional-commits](https://github.com/inprojectspl/conventional-commits) | MIT |
| mcp-builder, skill-creator, webapp-testing, frontend-design | [anthropics/skills](https://github.com/anthropics/skills) | Apache-2.0 |

> Note: Anthropic's document skills (pdf/docx/pptx/xlsx) are *source-available
> but proprietary* (`license: Proprietary` in their LICENSE.txt), so they are
> **not** redistributed here. Install them directly from
> [anthropics/skills](https://github.com/anthropics/skills) if you need them.

## MCP servers

The MCP entries are **launch specs only** — `npx`/`uvx`/`docker` invocations
that point at publicly-published packages. No third-party server code is
redistributed; each manifest links to its upstream `homepage` and carries the
upstream `license`. The canonical reference servers come from
[modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers)
(MIT); community servers (Playwright, Context7, Firecrawl) carry their own
licenses. Servers marked "archived upstream" still install + run but are no
longer maintained by their authors — prefer maintained alternatives.

## Plugins

The example plugins (`echo-plugin`, `text-tools`, `json-pretty`) are original,
minimal corlinman plugins authored for this registry (MIT).

## Ecosystem credit

The skill format and much of the community catalog trace back to the
**openclaw / clawhub** ecosystem ([openclaw/openclaw](https://github.com/openclaw/openclaw),
[openclaw/clawhub](https://github.com/openclaw/clawhub)) and Anthropic's Agent
Skills. corlinman's marketplace is compatible in spirit with those formats.

If you are a listed author and want a change to attribution or removal of an
item, open an issue or PR.
