<p align="center">
  <img src="docs/assets/eclipse-pearl.svg" alt="corlinman — the eclipse pearl" width="160" />
</p>

# corlinman

[![CI](https://img.shields.io/github/actions/workflow/status/sweetcornna/corlinman/ci.yml?branch=main&label=CI)](https://github.com/sweetcornna/corlinman/actions)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/sweetcornna/corlinman?label=release)](https://github.com/sweetcornna/corlinman/releases)
[![Docs](https://img.shields.io/badge/docs-architecture-informational)](docs/architecture.md)

**A self-hosted intelligent-agent platform.** Give a language model durable
memory, real tools, multiple chat channels, and an operations plane — all in
one stack you run on your own hardware, audit end-to-end, and govern with
human-in-the-loop approvals.

> Live demo: <https://corlinman.cornna.xyz> · 中文说明见文末 [中文速览](#中文速览) · full history in [`CHANGELOG.md`](CHANGELOG.md).

---

## Install

```bash
# Fresh install — preflight → pull image → boot → wait for /health → print URL
curl -fsSL https://raw.githubusercontent.com/sweetcornna/corlinman/main/deploy/install.sh | bash

# Upgrade in place (never touches your data dir)
bash deploy/install.sh --upgrade
```

Then open `http://localhost:6005/login`, sign in with `admin` / `root`, and
rotate the password on the **Account & Security** page you're redirected to.
Want a real LLM? Walk `/onboard` in the UI, or run `corlinman init` on a
headless server. Want to chat immediately? Hit **Skip** to use the bundled
mock provider.

| Flag | Effect |
| --- | --- |
| `--mode docker` *(default)* | Pull `ghcr.io/sweetcornna/corlinman:latest` (multi-arch), fall back to a local build. Needs Docker 24+ / compose v2. |
| `--mode native` | Install `uv`, clone to `/opt/corlinman/repo`, register a systemd unit. No container runtime. |
| `--china` | PyPI→Tsinghua, Docker Hub→DaoCloud, github→gh-proxy, npm→npmmirror. Auto-enabled when `pypi.org` is slow. |
| `--with-qq` | Layer a NapCat (OneBot v11) sidecar for the QQ channel (docker mode). |
| `--upgrade` | Auto-detect docker vs native, pull/rebuild, restart, re-probe `/health`. |

Every fresh install runs a **preflight** (disk ≥ 5 GB, RAM ≥ 1 GB, port 6005
free, tooling on PATH) and ends with a **health gate** that polls `/health`
until 200 — the URL it prints is guaranteed to respond. AI agents deploying
corlinman should read [`deploy/AI_DEPLOY.md`](deploy/AI_DEPLOY.md). Full
walkthrough: [`docs/quickstart.md`](docs/quickstart.md).

---

## Why corlinman

Most LLM infrastructure is either a **thin API wrapper** or a **drag-and-drop
workflow toolbox**. corlinman takes a third stance: **the agent is the
product** — the reasoning loop, its tools, its memory, its channels, and the
operator surface that governs it all live in one coherent, auditable system.

- **One agent loop, many providers.** OpenAI tool-call semantics over
  Anthropic, OpenAI, Google, DeepSeek, Qwen, or GLM — per-model aliases and
  hot-swap without restart.
- **Tools are real plugins, not prompt templates.** Sync, async, and
  long-lived "service" tools over JSON-RPC 2.0 / gRPC, with optional Docker
  sandboxing and a human-approval gate for dangerous actions.
- **Memory that survives conversations.** Per-session history in SQLite plus a
  SQLite FTS5 (BM25) knowledge base, every chunk traceable to its source.
- **Channels are first-class agent I/O.** Production adapters for QQ, Telegram,
  Discord, Slack, Feishu, a cron scheduler, and an OpenAI-compatible HTTP/SSE
  endpoint — all sharing the same loop.
- **An operations plane that treats ops seriously.** The **Tidepool** glass
  admin console (day + night) for plugins, RAG, live logs, approvals, config
  live-reload, and model routing — plus OTel traces, Prometheus metrics, and a
  `corlinman doctor` smoke check.

---

## Architecture at a glance

```
   HTTP + SSE ──────▶ ┌───────────────────────────────┐ ◀─── Next.js admin UI
   (clients, UI,      │       corlinman-gateway        │      (static export)
    channels)         │  Python · FastAPI · uvicorn    │
                      │  :6005 → /v1 /admin /health    │
                      │  /metrics /plugin-cb /v1/voice │
                      └──┬──────────┬──────────┬───────┘
                in-proc  │  in-proc │   JSON-RPC / gRPC │
                         ▼          ▼          ▼
                    ┌────────┐  ┌────┐   ┌────────────┐
                    │ agent  │  │ emb│   │  plugin    │
                    │ loop + │  │(py)│   │  runtimes  │
                    │ LLM SDK│  └────┘   │ py/node/sh │
                    └───┬────┘           │ + docker   │
                        ▼                └────────────┘
              ┌────────────────────────┐
              │ providers: Anthropic · │   Side-bus:
              │ OpenAI · Google ·      │    • channels ── QQ / Telegram / ... ──▶ ChatRequest
              │ DeepSeek · Qwen · GLM  │    • scheduler ── croniter ──▶ gateway
              └────────────────────────┘    • embedding ── SQLite FTS5 (BM25) ──▶ /admin/rag
```

**One language, one process — a pure Python stack:** FastAPI + uvicorn
gateway, `grpc.aio` agent sidecar, `docker-py` plugin sandboxes,
`watchdog`-driven hot reload, SQLite FTS5 search, `traceparent` propagation
end-to-end. The reasoning loop, provider SDKs, embedding, plugin runtime,
channel adapters, CLI, and gateway all live under `python/packages/` and share
one venv. Deep dive: [`docs/architecture.md`](docs/architecture.md).

---

## Core concepts

**Agents** are frontmatter-headed Markdown (`~/.corlinman/agents/<name>.md`),
hot-editable from the admin UI and routed by their `model` field or a
per-channel binding. Each wraps a `reasoning_loop` that emits tokens + tool
calls and iterates until the model signals stop.

**Tools (plugins)** are real programs, not prompts. Each runs in its own
sandbox, speaks JSON-RPC 2.0 / gRPC + JSON, and publishes a JSON Schema the
agent sees via OpenAI tool_call semantics:

| Type | Transport | Lifetime | Use case |
| --- | --- | --- | --- |
| `sync` | JSON-RPC stdio | per call | calculator, HTTP fetch, shell one-shots |
| `async` | stdio + `/plugin-callback` | spawn → task_id → webhook | long jobs (image gen, LLM sub-calls) |
| `service` | gRPC over UDS | long-lived supervised child | stateful integrations (DB pools, Git) |

Plugins can be written in **any language** — the contract is stdio/gRPC + JSON.
Optional Docker sandboxing enforces memory/CPU/network limits and capability
drops; untrusted plugins can demand human approval per call. Authoring guide:
[`docs/plugin-authoring.md`](docs/plugin-authoring.md).

**Memory** has two auditable layers: per-session message history in SQLite, and
a SQLite FTS5 (BM25) knowledge base exposed through `/admin/rag` (dense
vectors + rerank are on the [roadmap](docs/PLAN_PORT_COMPLETION.md)).

**Governance** — per-tool approvals (`allow`/`deny`/`prompt`, prompt parks the
call for a human click or auto-denies after 5 min), per-channel rate limits,
atomic config live-reload via `POST /admin/config`, OTel + Prometheus
observability, and `corlinman doctor` (9 local health checks, CI-friendly
`--json`).

---

## Providers

| Provider | Chat | Streaming | Tool calls | Embeddings |
| --- | :--: | :--: | :--: | :--: |
| Anthropic | ✅ | ✅ | ✅ | — |
| OpenAI | ✅ | ✅ | ✅ | ✅ |
| Google | ✅ | ✅ | ✅ | ✅ |
| DeepSeek / Qwen / GLM | ✅ | ✅ | ✅ | — |
| _OpenAI-compatible_ (vLLM, Ollama, SiliconFlow, any spec-compliant gateway) | ✅ | ✅ | ✅ | ✅ |

Need a CN endpoint or a niche aggregator? Add an `openai_compatible` entry with
a chosen name — no code changes. A fully custom provider is a ~200-line Python
class. Full reference (14 `kind`s + recipes): [`docs/providers.md`](docs/providers.md).

---

## Admin UI

A Next.js static-export console (**Tidepool** — warm-amber glass, day + night
themes, `⌘K` palette, live SSE dashboards) covering the full control plane:
Dashboard, Plugins, Agents, RAG, Channels, Scheduler, Approvals, Models,
Config, and Logs. The headline surface is the in-app **`/chat`** — a
Claude.ai-grade window driven by the same agent backend as every channel, with
streaming reasoning/tool-call cards, inline approvals, an artifact panel, a
token+cost meter, and resumable sessions. WCAG-AA in both themes, zh-CN / en.

---

## Configuration

corlinman boots from `$CORLINMAN_DATA_DIR/config.toml` (data defaults to
`~/.corlinman/`). Everything is hot-reloadable via the **Config** page or
`POST /admin/config`; restart-required fields return `requires_restart: true`.

```toml
[server]
port = 6005
bind = "0.0.0.0"

[admin]
username = "admin"
password_hash = "$argon2id$v=19$m=32768,t=2,p=1$..."   # set via /onboard

[providers.openai]
kind = "openai"
api_key = { env = "OPENAI_API_KEY" }
enabled = true

[models]
default = "gpt-4o-mini"
[models.aliases]
smart = "claude-opus-4-8"
cheap = "gpt-4o-mini"
```

Annotated reference: [`docs/config.example.toml`](docs/config.example.toml).

---

## Documentation

- [Quickstart](docs/quickstart.md) — 60-second boot + first login
- [Profiles](docs/profiles.md) — isolated agent instances (persona + memory + skills)
- [Credentials](docs/credentials.md) — provider keys + EnvPage UI
- [Providers](docs/providers.md) — 14 `kind`s + recipes (Ollama / OpenRouter / SiliconFlow / Groq)
- [Plugin authoring](docs/plugin-authoring.md) — write a sync / async / service plugin
- [Skills & agents](docs/guides/skills-and-agents.md) — author `skills/`, `agents/`, `TVStxt/*`
- [Architecture](docs/architecture.md) — message flow, package graph, gRPC bus
- [Evolution & curator](docs/evolution-curator.md) — the self-evolution loop
- [Runbook](docs/runbook.md) — production deploy (nginx + acme.sh + Cloudflare) + incidents
- [Roadmap](docs/roadmap.md) · [Milestones](docs/milestones.md) · [Changelog](CHANGELOG.md)

---

## Development

```bash
./scripts/dev-setup.sh          # hooks, deps, proto generation
corlinman dev                   # whole stack, hot reload

# Full gate (what CI + pre-commit run)
uv run ruff check .
uv run mypy python/packages/
uv run pytest -m "not live_llm and not live_transport"
pnpm -C ui typecheck && pnpm -C ui lint && pnpm -C ui build

make doctor                     # quick local smoke → uv run corlinman doctor
```

Repository layout: `python/packages/*` (gateway / agent / providers /
embedding / channels / mcp / cli / …), `proto/` (gRPC IDL), `ui/` (Next.js
console), `docker/` (sandbox profiles), `docs/`, `ops/` (Grafana + observ.).
Conventions, test lanes, and architecture invariants live in
[`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## Contributing & License

Contributions welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md) and open GitHub
issues for bugs / features. MIT licensed ([`LICENSE`](LICENSE)).

---

## 中文速览

**corlinman 是一个可自托管的智能体平台。** 不是 LLM 的 API 代理，也不是拖拽工作流的工具箱——而是一套有主张的运行时：让语言模型拥有**持久记忆**、**真实工具**、**多通道接入**、**可审计的运维面板**，全部跑在你自己的机器上。

```bash
# 一行安装：preflight → 拉镜像 → 启动 → 等 /health 200 → 打印 URL
curl -fsSL https://raw.githubusercontent.com/sweetcornna/corlinman/main/deploy/install.sh | bash

# 国内网络加 --china（清华 PyPI / gh-proxy / DaoCloud），TTFB 慢时自动开启
# 顺带起 NapCat QQ 机器人再加 --with-qq（docker 模式）
# 升级（不动数据目录）：bash deploy/install.sh --upgrade
```

装完打开 `http://<服务器>:6005/login`，用 `admin` / `root` 登录后强制跳转 **账户安全** 改密码；想接真 LLM 走 UI 的 `/onboard` 或服务器上 `corlinman init`（免浏览器）。

**核心能力**：一个 agent 循环跑多家 provider（Anthropic / OpenAI / Google / DeepSeek / Qwen / GLM，配置热重载、按别名路由）；真工具而非 prompt 模板（同步/异步/常驻三种插件 + 可选 Docker 沙箱 + 人工审批）；跨会话记忆（SQLite 历史 + FTS5/BM25 检索）；通道一等公民（QQ / Telegram / Discord / Slack / Feishu / 定时任务 / OpenAI 兼容 HTTP/SSE）；**Tidepool** 暖橙玻璃后台（日夜双主题 + OTel/Prometheus 埋点 + 9 项 doctor 体检）。

**架构**：纯 Python 单语言栈——FastAPI/uvicorn gateway + grpc.aio agent sidecar + 插件 runtime + SQLite FTS5 检索 + CLI + provider SDK 全在 `python/packages/` 下共享一个 venv。在线 demo：<https://corlinman.cornna.xyz>，架构细节见 [`docs/architecture.md`](docs/architecture.md)，生产部署见 [`docs/runbook.md`](docs/runbook.md)。
