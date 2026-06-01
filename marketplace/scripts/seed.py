#!/usr/bin/env python3
"""seed.py — materialise the curated corlinman marketplace seed content.

Writes the MCP server manifests, skill SKILL.md files, and example plugin
bundles for the registry, then you run ``build-registry.py`` to pack the
tarballs + regenerate ``index.json``.

Provenance / licensing:
* MCP launch specs are original (just ``npx``/``uvx``/``docker`` invocations
  pointing at public packages) — verified package names, no third-party code
  redistributed.
* Skills are condensed, attributed adaptations of permissively-licensed
  upstream skills (Apache-2.0: anthropics/skills; MIT: obra/superpowers,
  inprojectspl/conventional-commits). Each SKILL.md carries a Source +
  License footer; see ../ATTRIBUTION.md.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# MCP servers — verified package names (modelcontextprotocol/servers,
# npm/PyPI). Each tuple: slug, name, description, command, args, requires_env,
# homepage, license, category.
# ---------------------------------------------------------------------------
MCP = [
    ("filesystem", "Filesystem", "Secure local file operations with configurable allowed directories.",
     "npx", ["-y", "@modelcontextprotocol/server-filesystem", "<ALLOWED_DIR>"], [],
     "https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem", "MIT", "filesystem"),
    ("fetch", "Fetch", "Fetch a URL and convert its content to markdown for LLM use.",
     "uvx", ["mcp-server-fetch"], [],
     "https://github.com/modelcontextprotocol/servers/tree/main/src/fetch", "MIT", "web"),
    ("memory", "Memory", "Knowledge-graph-based persistent memory for the agent.",
     "npx", ["-y", "@modelcontextprotocol/server-memory"], [],
     "https://github.com/modelcontextprotocol/servers/tree/main/src/memory", "MIT", "memory"),
    ("sequential-thinking", "Sequential Thinking", "Structured, reflective multi-step problem solving.",
     "npx", ["-y", "@modelcontextprotocol/server-sequential-thinking"], [],
     "https://github.com/modelcontextprotocol/servers/tree/main/src/sequentialthinking", "MIT", "dev"),
    ("time", "Time", "Current time and timezone conversion utilities.",
     "uvx", ["mcp-server-time"], [],
     "https://github.com/modelcontextprotocol/servers/tree/main/src/time", "MIT", "productivity"),
    ("git", "Git", "Read, search, and manipulate a local Git repository.",
     "uvx", ["mcp-server-git", "--repository", "<REPO_PATH>"], [],
     "https://github.com/modelcontextprotocol/servers/tree/main/src/git", "MIT", "dev"),
    ("everything", "Everything", "Reference server exercising prompts, resources, and tools (good for testing).",
     "npx", ["-y", "@modelcontextprotocol/server-everything"], [],
     "https://github.com/modelcontextprotocol/servers/tree/main/src/everything", "MIT", "dev"),
    ("github", "GitHub (official)", "Official GitHub MCP server — repos, issues, PRs, actions. Requires Docker.",
     "docker", ["run", "-i", "--rm", "-e", "GITHUB_PERSONAL_ACCESS_TOKEN", "ghcr.io/github/github-mcp-server"],
     ["GITHUB_PERSONAL_ACCESS_TOKEN"],
     "https://github.com/github/github-mcp-server", "MIT", "dev"),
    ("playwright", "Playwright (Microsoft)", "Browser automation: navigate, inspect, screenshot, interact.",
     "npx", ["-y", "@playwright/mcp@latest"], [],
     "https://github.com/microsoft/playwright-mcp", "Apache-2.0", "web"),
    ("context7", "Context7 (Upstash)", "Inject up-to-date, version-specific library docs into context.",
     "npx", ["-y", "@upstash/context7-mcp"], [],
     "https://github.com/upstash/context7", "MIT", "dev"),
    ("firecrawl", "Firecrawl", "Web scraping, crawling, search, and structured extraction.",
     "npx", ["-y", "firecrawl-mcp"], ["FIRECRAWL_API_KEY"],
     "https://github.com/mendableai/firecrawl-mcp-server", "MIT", "web"),
    ("brave-search", "Brave Search", "Web + local search via the Brave Search API. (archived upstream; still runnable)",
     "npx", ["-y", "@modelcontextprotocol/server-brave-search"], ["BRAVE_API_KEY"],
     "https://github.com/modelcontextprotocol/servers-archived/tree/main/src/brave-search", "MIT", "search"),
    ("sqlite", "SQLite", "Query and inspect a SQLite database. (archived upstream; still runnable)",
     "uvx", ["mcp-server-sqlite", "--db-path", "<DB_PATH>"], [],
     "https://github.com/modelcontextprotocol/servers-archived/tree/main/src/sqlite", "MIT", "database"),
    ("postgres", "PostgreSQL", "Read-only PostgreSQL access with schema inspection. (archived upstream; still runnable)",
     "npx", ["-y", "@modelcontextprotocol/server-postgres", "<POSTGRES_URL>"], [],
     "https://github.com/modelcontextprotocol/servers-archived/tree/main/src/postgres", "MIT", "database"),
    ("redis", "Redis", "Interact with a Redis key-value store. (archived upstream; still runnable)",
     "npx", ["-y", "@modelcontextprotocol/server-redis", "<REDIS_URL>"], [],
     "https://github.com/modelcontextprotocol/servers-archived/tree/main/src/redis", "MIT", "database"),
    ("slack", "Slack", "Slack channel management and messaging. (archived upstream; still runnable)",
     "npx", ["-y", "@modelcontextprotocol/server-slack"], ["SLACK_BOT_TOKEN", "SLACK_TEAM_ID"],
     "https://github.com/modelcontextprotocol/servers-archived/tree/main/src/slack", "MIT", "communication"),
    ("puppeteer", "Puppeteer", "Headless-browser automation and scraping. (archived upstream; still runnable)",
     "npx", ["-y", "@modelcontextprotocol/server-puppeteer"], [],
     "https://github.com/modelcontextprotocol/servers-archived/tree/main/src/puppeteer", "MIT", "web"),
    ("gitlab", "GitLab", "GitLab project management and file operations. (archived upstream; still runnable)",
     "npx", ["-y", "@modelcontextprotocol/server-gitlab"], ["GITLAB_PERSONAL_ACCESS_TOKEN", "GITLAB_API_URL"],
     "https://github.com/modelcontextprotocol/servers-archived/tree/main/src/gitlab", "MIT", "dev"),
    ("google-maps", "Google Maps", "Geocoding, place search, directions, distance. (archived upstream; still runnable)",
     "npx", ["-y", "@modelcontextprotocol/server-google-maps"], ["GOOGLE_MAPS_API_KEY"],
     "https://github.com/modelcontextprotocol/servers-archived/tree/main/src/google-maps", "MIT", "maps"),
    ("sentry", "Sentry", "Retrieve and analyze Sentry.io issues. (archived upstream; still runnable)",
     "uvx", ["mcp-server-sentry", "--auth-token", "<SENTRY_AUTH_TOKEN>"], [],
     "https://github.com/modelcontextprotocol/servers-archived/tree/main/src/sentry", "MIT", "dev"),
]


def write_mcp() -> int:
    n = 0
    for slug, name, desc, command, args, req_env, homepage, lic, category in MCP:
        d = ROOT / "mcp" / slug
        d.mkdir(parents=True, exist_ok=True)
        manifest = {
            "name": slug,
            "display_name": name,
            "description": desc,
            "transport": "stdio",
            "command": command,
            "args": args,
            "env": {},
            "requires": {"env": req_env},
            "homepage": homepage,
            "license": lic,
            "category": category,
            "setup": _mcp_setup(command, req_env),
        }
        (d / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        n += 1
    return n


def _mcp_setup(command: str, req_env: list[str]) -> list[str]:
    notes = []
    if command == "npx":
        notes.append("Requires Node.js (npx) on the host.")
    elif command == "uvx":
        notes.append("Requires uv (uvx) on the host.")
    elif command == "docker":
        notes.append("Requires Docker on the host.")
    if req_env:
        notes.append("Provide these secrets at enable time: " + ", ".join(req_env) + ".")
    notes.append("Replace any <PLACEHOLDER> arg (e.g. <ALLOWED_DIR>) before enabling.")
    return notes


# ---------------------------------------------------------------------------
# Skills — condensed, attributed adaptations (frontmatter description verbatim
# from upstream; bodies faithful condensations). slug -> dict.
# ---------------------------------------------------------------------------
SKILLS: dict[str, dict[str, str]] = {
    "brainstorming": {
        "emoji": "💡",
        "description": "You MUST use this before any creative work - creating features, building components, adding functionality, or modifying behavior. Explores user intent, requirements and design before implementation.",
        "source": "github.com/obra/superpowers", "license": "MIT",
        "body": """\
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
"simple" projects a short design pass before building.""",
    },
    "systematic-debugging": {
        "emoji": "🐛",
        "description": "Use when encountering any bug, test failure, or unexpected behavior, before proposing fixes",
        "source": "github.com/obra/superpowers", "license": "MIT",
        "body": """\
Core rule: NO FIXES WITHOUT ROOT-CAUSE INVESTIGATION FIRST. Symptom fixes mask
the real issue and cause rework.

Four phases:
1. Root cause — read the full error + stack trace, reproduce it reliably,
   review recent changes, instrument boundaries in multi-component systems,
   and trace the bad value backward to its origin.
2. Pattern analysis — find a working example in the same codebase, read the
   reference implementation fully, and list every difference vs. the broken path.
3. Hypothesis & testing — form ONE specific hypothesis, change ONE variable at
   a time, and verify before continuing. Form a new hypothesis instead of
   stacking fixes.
4. Implementation — write a failing test first, make one root-cause change,
   then confirm nothing else broke.

Red flags (go back to phase 1): proposing a fix before investigating, changing
several things at once, skipping the test, or attempting a 4th fix after 3
failures (3 failures = an architectural problem — discuss, don't keep patching).""",
    },
    "writing-plans": {
        "emoji": "📝",
        "description": "Use when you have a spec or requirements for a multi-step task, before touching code",
        "source": "github.com/obra/superpowers", "license": "MIT",
        "body": """\
Produce a plan an engineer with minimal context could execute.

Rules:
- Each task is 2-5 minutes and one action (write test -> run/expect-fail ->
  implement -> verify -> commit).
- NO placeholders: every step lists exact file paths, complete code blocks, and
  exact commands. Never write "add error handling" without showing how.
- Map file responsibilities (single responsibility each) before writing tasks.

Self-review checklist before finishing: every spec requirement maps to a task;
hunt for TBD/TODO/"similar to Task N"; keep type/signature consistency across
tasks. The plan header declares Goal, Architecture, and Tech Stack. Default
save path: `docs/plans/YYYY-MM-DD-<feature>.md`.""",
    },
    "test-driven-development": {
        "emoji": "✅",
        "description": "Use when implementing any feature or bugfix, before writing implementation code",
        "source": "github.com/obra/superpowers", "license": "MIT",
        "body": """\
The Iron Law: no production code without a failing test first.

Red-Green-Refactor:
1. RED — write the smallest test that captures the next bit of behavior and
   watch it fail for the right reason.
2. GREEN — write the minimum code to make it pass.
3. REFACTOR — clean up with the test as a safety net.

Rejected rationalizations: "it's too simple to test", "I'll add tests after",
"the test is obvious". Each is a trap — write the test first anyway. One
behavior per test; descriptive test names; keep tests fast and independent.""",
    },
    "conventional-commits": {
        "emoji": "🔖",
        "description": "Write git commit messages that follow the Conventional Commits v1.0.0 specification.",
        "source": "github.com/inprojectspl/conventional-commits", "license": "MIT",
        "body": """\
Format: `<type>(<optional scope>): <description>` then an optional body and
footers.

Types: feat, fix, docs, style, refactor, perf, test, build, ci, chore, revert.

Rules:
- Description in the imperative mood ("add", not "added"), <= 72 chars, no
  trailing period.
- A breaking change is marked with `!` after the type/scope AND/OR a
  `BREAKING CHANGE:` footer explaining the break.
- Body explains the what/why (not the how); wrap at ~72 columns.
- One logical change per commit.

Examples:
  feat(auth): add OAuth2 device-code login
  fix(api): reject negative page sizes (closes #214)
  refactor(store)!: drop the legacy sync API

  BREAKING CHANGE: `Store.sync()` is removed; use `await Store.async()`.""",
    },
    "mcp-builder": {
        "emoji": "🔌",
        "description": "Guide for creating high-quality MCP (Model Context Protocol) servers that enable LLMs to interact with external services through well-designed tools. Use when building MCP servers to integrate external APIs or services, whether in Python (FastMCP) or Node/TypeScript (MCP SDK).",
        "source": "github.com/anthropics/skills", "license": "Apache-2.0",
        "body": """\
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
wrapper of every API endpoint.""",
    },
    "skill-creator": {
        "emoji": "🛠️",
        "description": "Create new skills, modify and improve existing skills, and measure skill performance. Use when users want to create a skill from scratch, edit, or optimize an existing skill, run evals to test a skill, benchmark skill performance, or optimize a skill's description for better triggering accuracy.",
        "source": "github.com/anthropics/skills", "license": "Apache-2.0",
        "body": """\
A skill is a `SKILL.md` with YAML frontmatter (`name`, `description`, optional
`when_to_use`/`allowed-tools`) plus a markdown body of instructions, optionally
with bundled resource files.

Loop: define intent -> draft -> test with realistic prompts -> evaluate
(qualitatively + with a few saved evals) -> improve -> repeat -> package.

Guidance:
- Make the `description` "pushy" and specific so the skill triggers reliably
  (under-triggering is the most common failure).
- Keep the body focused (~under 500 lines); use progressive disclosure — link
  out to reference files for depth instead of inlining everything.
- Explain the "why", prefer imperative voice, and give concrete examples.
- Save 2-3 should-trigger and should-not-trigger prompts to sanity-check
  triggering before shipping.""",
    },
    "webapp-testing": {
        "emoji": "🧪",
        "description": "Toolkit for interacting with and testing local web applications using Playwright. Supports verifying frontend functionality, debugging UI behavior, capturing browser screenshots, and viewing browser logs.",
        "source": "github.com/anthropics/skills", "license": "Apache-2.0",
        "body": """\
Prefer small, native Playwright (Python) scripts over ad-hoc clicking.

Recon-then-action pattern:
1. First inspect the rendered DOM — take a screenshot and dump the content.
2. Identify selectors from what you actually see (not what you assume).
3. Then act on those selectors.

Decision: static HTML -> just read it; dynamic content -> drive a real browser.

Pitfalls: don't inspect the DOM before the app finishes loading — wait for
`networkidle` on dynamic apps. Use the synchronous Playwright API, close
browsers properly, use descriptive selectors, and add appropriate waits.
Capture console logs to debug runtime errors.""",
    },
    "frontend-design": {
        "emoji": "🎨",
        "description": "Create distinctive, production-grade frontend interfaces with high design quality. Use this skill when the user asks to build web components, pages, artifacts, posters, or applications. Generates creative, polished UI that avoids generic AI aesthetics.",
        "source": "github.com/anthropics/skills", "license": "Apache-2.0",
        "body": """\
Before coding, establish four things: Purpose, Tone (pick a distinct aesthetic —
minimalist, maximalist, retro-futuristic, brutalist, editorial...), Constraints,
and Differentiation (the one memorable element). Choose a clear conceptual
direction and execute it with precision.

Five dimensions:
- Typography — beautiful, unique display/body pairings; no generic defaults.
- Color & theme — a cohesive palette with one dominant color + sharp accents,
  driven by CSS variables.
- Motion — purposeful CSS animations, orchestrated load reveals, scroll triggers.
- Spatial composition — asymmetry, overlap, diagonal flow, deliberate negative space.
- Backgrounds & detail — gradients, texture, shadows for depth.

Avoid the AI-slop tells: overused fonts (default Inter), predictable purple
gradients, everything centered, uniformly rounded corners, cookie-cutter layouts.""",
    },
}


def write_skills() -> int:
    n = 0
    for slug, s in SKILLS.items():
        d = ROOT / "skills" / slug
        d.mkdir(parents=True, exist_ok=True)
        fm = [
            "---",
            f"name: {slug}",
            f'description: "{s["description"].replace(chr(34), chr(39))}"',
        ]
        if s.get("emoji"):
            fm.append(f"emoji: {s['emoji']}")
        fm.append("---")
        body = s["body"].rstrip()
        footer = (
            f"\n\n---\nSource: {s['source']} ({s['license']}). "
            "Condensed + attributed for the corlinman marketplace; see "
            "../../ATTRIBUTION.md."
        )
        (d / "SKILL.md").write_text(
            "\n".join(fm) + "\n\n" + body + footer + "\n", encoding="utf-8"
        )
        n += 1
    return n


# ---------------------------------------------------------------------------
# Example plugins (corlinman plugin format: manifest.json for index metadata +
# plugin-manifest.toml for the live PluginRegistry + an entry script). These
# are minimal, original examples mirroring the echo-plugin seed.
# ---------------------------------------------------------------------------
PLUGINS = {
    "text-tools": {
        "version": "0.1.0",
        "description": "Text utilities: uppercase, lowercase, reverse, and word count for a line of input.",
        "tool": "text_transform",
        "tool_desc": "Transform a line of text (op: upper|lower|reverse|wordcount).",
        "entry": "text_tools.py",
        "script": '''\
"""text-tools — a minimal corlinman example plugin (stdin line -> transform)."""
from __future__ import annotations

import sys


def main() -> int:
    line = sys.stdin.readline().rstrip("\\n")
    sys.stdout.write(line[::-1] + "\\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
''',
    },
    "json-pretty": {
        "version": "0.1.0",
        "description": "Pretty-print and validate a JSON document read from stdin.",
        "tool": "json_pretty",
        "tool_desc": "Read JSON from stdin and emit a 2-space pretty-printed form (or an error).",
        "entry": "json_pretty.py",
        "script": '''\
"""json-pretty — a minimal corlinman example plugin (stdin JSON -> pretty)."""
from __future__ import annotations

import json
import sys


def main() -> int:
    raw = sys.stdin.read()
    try:
        obj = json.loads(raw)
    except ValueError as exc:
        sys.stdout.write(f"invalid JSON: {exc}\\n")
        return 1
    sys.stdout.write(json.dumps(obj, indent=2, ensure_ascii=False) + "\\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
''',
    },
}


def write_plugins() -> int:
    n = 0
    for slug, p in PLUGINS.items():
        d = ROOT / "plugins" / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "manifest.json").write_text(
            json.dumps(
                {
                    "name": slug,
                    "version": p["version"],
                    "description": p["description"],
                    "entry": p["entry"],
                    "permissions": [],
                    "requires": {"env": []},
                },
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        toml = f'''\
# Canonical corlinman plugin manifest (discovered + hot-loaded by the gateway).
manifest_version = 1
name = "{slug}"
version = "{p['version']}"
description = "{p['description']}"
author = "corlinman marketplace"
plugin_type = "sync"

[entry_point]
command = "python3"
args = ["{p['entry']}"]

[communication]
timeout_ms = 5000

[[capabilities.tools]]
name = "{p['tool']}"
description = "{p['tool_desc']}"
parameters = {{ type = "object", properties = {{ text = {{ type = "string" }} }} }}
'''
        (d / "plugin-manifest.toml").write_text(toml, encoding="utf-8")
        (d / p["entry"]).write_text(p["script"], encoding="utf-8")
        n += 1
    return n


if __name__ == "__main__":
    nm = write_mcp()
    ns = write_skills()
    npl = write_plugins()
    print(f"seeded mcp={nm} skills={ns} plugins={npl}")
    print("now run: python scripts/build-registry.py")
