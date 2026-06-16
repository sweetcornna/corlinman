<p align="center">
  <img src="docs/assets/logo.png" alt="corlinman mascot" width="140" />
</p>

# corlinman

[![CI](https://img.shields.io/github/actions/workflow/status/sweetcornna/corlinman/ci.yml?branch=main&label=CI)](https://github.com/sweetcornna/corlinman/actions)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-1.21.8-brightgreen)](CHANGELOG.md)
[![Docs](https://img.shields.io/badge/docs-architecture-informational)](docs/architecture.md)

**A self-hosted intelligent-agent platform.** Give a language model durable
memory, real tools, multiple chat channels, and an operations plane — all
in one binary you can run on your own hardware, audit end-to-end, and
govern with human-in-the-loop approvals.

![corlinman — 60-second product tour: five pillars, multi-provider agent loop, sandboxed plugins, human-in-the-loop approvals, hybrid RAG memory, first-class channels, Tidepool admin day & night, and a one-second doctor check](docs/assets/tidepool-hero.gif)

> _Live deployment reference: <https://corlinman.cornna.xyz>._
> _中文介绍章节见文末 ["中文速览"](#中文速览)。_
>
> **What's new in 1.21.8** — the Codex provider Test button no longer false
> fails with `codex: HTTP 400` after a successful OAuth login when the live
> ChatGPT Codex model catalog probe is unavailable. See
> [`CHANGELOG.md`](CHANGELOG.md).
> _1.21.8 修复 Codex OAuth 登录成功后点击提供商测试仍弹出 `codex: HTTP 400`
> 的误报；测试按钮现在验证 OAuth 凭据可用性，不再把模型目录探测 400 当作登录失败。_

---

## 🚀 一键安装最新版本

```bash
curl -fsSL https://raw.githubusercontent.com/sweetcornna/corlinman/main/deploy/install.sh | bash
```

升级到最新版本（保留所有数据）：

```bash
bash deploy/install.sh --upgrade
```

完整说明（preflight、health gate、China mirrors、QQ sidecar、native vs docker）见
下面的 [Quickstart](#quickstart-60-seconds) 与 [Install paths](#install-paths) 两节。

---

## Why corlinman

Most LLM infrastructure today is either a **thin API wrapper** (you send
prompts, you read tokens, you integrate nothing) or a **workflow toolbox**
(drag and drop nodes, marketplace plugins, zero opinion on how they compose).

corlinman takes a third stance: **the agent is the product.** The reasoning
loop, the tools it calls, the memory it retains across turns, the channels
it hears from, and the operator surface that governs it — all live in one
coherent system that is opinionated about correctness, observability, and
safety.

What you get out of the box:

- **One agent loop, many providers.** OpenAI tool-call semantics on top of
  Anthropic, OpenAI, Google, DeepSeek, Qwen, or GLM — with per-model aliases
  and hot-swap without restart.
- **Tools are real plugins, not prompt templates.** Sync, async, and
  long-lived "service" tools over JSON-RPC 2.0 stdio or gRPC, with optional
  Docker sandboxing for untrusted code and a human-approval gate for
  dangerous actions.
- **Memory that survives conversations.** Per-session message history in
  SQLite; a SQLite FTS5 (BM25) knowledge base today, with HNSW dense
  vectors + RRF fusion + cross-encoder rerank on the roadmap
  (see [`docs/PLAN_PORT_COMPLETION.md`](docs/PLAN_PORT_COMPLETION.md)).
- **Channels are first-class agent I/O.** Production adapters for QQ
  (OneBot v11) and Telegram, a scheduler for cron-driven tasks, an
  OpenAI-compatible HTTP/SSE endpoint for your own clients.
- **An admin plane that treats operations seriously.** A warm-amber
  glass web console (**Tidepool** design system, day + night themes) for
  plugin management, RAG inspection, live log streaming, approval
  queues, config live-reload, and model routing — plus OTel traces,
  Prometheus metrics, and a `corlinman doctor` smoke command.

If you want something you can hand your teammate a URL to, then audit on
Sunday morning without reverse-engineering twenty repos — that's corlinman.

---

## Quickstart (60 seconds)

Use the [one-line installer at the top of this README](#-一键安装最新版本).
Behind the scenes that single command does the rest:

1. **Preflight** — checks disk (≥ 5 GB), RAM (≥ 1 GB), port `6005`, docker version, required tools. Bails early with a clear `✗ port 6005 held by PID …` if anything's off.
2. **Image** — `docker pull ghcr.io/sweetcornna/corlinman:latest` (multi-arch amd64/arm64, ~30 s). Falls back to a local `docker buildx build` if the registry is unreachable.
3. **Boot** — `docker compose up -d` with the bundled compose file.
4. **Health gate** — polls `/health` until 200 (≤ 60 s; override with `CORLINMAN_HEALTH_TIMEOUT`).
5. **Done** — prints the URL to open and the seed credentials:

```
✅ corlinman is live: http://localhost:6005/login
   default login:  admin / root   ← change immediately at /account/security
   data dir:       /opt/corlinman/data
   upgrade later:  bash deploy/install.sh --upgrade
```

Sign in with `admin` / `root`, get redirected to **Account & Security**,
rotate the password — done. Want a real LLM? Walk `/onboard` from the UI
or run `corlinman init` (works headless on a server without a browser).
Want to start chatting immediately on the bundled mock provider? Hit
**Skip** in `/onboard`.

> **Security**: first-boot credentials are `admin` / `root` and are explicitly intended for local development. The UI forces a password rotation on first login and stamps a banner until you change them; `corlinman doctor` will keep warning until the default is gone.

**Upgrade later**: `bash deploy/install.sh --upgrade` — auto-detects
docker vs native, pulls the new image (or re-syncs the venv), restarts
the service, runs a fresh `/health` probe, never touches the data dir.

For multi-agent setups, deeper provider config, and the self-evolution curator, see:

- [Profiles](docs/profiles.md) — isolated agent instances with their own persona/memory/skills
- [Credentials](docs/credentials.md) — provider keys + EnvPage UI
- [Evolution & Curator](docs/evolution-curator.md) — how the agent grows with you

---

## Architecture at a glance

```
                      ┌────────────────────────────────────┐
   HTTP + SSE ──────▶ │        corlinman-gateway           │ ◀─── Next.js admin UI
   (clients, UI,      │   Python · FastAPI · uvicorn ·     │     (static export,
    channels)         │   listens on :6005; routes /v1,    │      served by nginx)
                      │   /admin, /health, /metrics,       │
                      │   /plugin-cb, /v1/voice WS)        │
                      └──┬──────────┬──────────┬──────────┘
                         │          │          │
              in-process │ in-proc  │ in-proc  │ JSON-RPC / gRPC
                         ▼          ▼          ▼
                    ┌────────┐  ┌────┐    ┌───────────┐
                    │ agent  │  │emb │    │ plugin    │
                    │ Python │  │(py)│    │ runtimes  │
                    │ loop   │  └────┘    │ (py / node/
                    │ + LLM  │            │  bash +   │
                    │ SDKs   │            │  docker)  │
                    └───┬────┘            └───────────┘
                        ▼
              ┌──────────────────────┐
              │ upstream providers    │
              │ Anthropic · OpenAI ·  │
              │ Google · DeepSeek ·   │
              │ Qwen · GLM · custom   │
              └──────────────────────┘

   Side-bus:
     • corlinman-channels ── QQ / OneBot v11 · Telegram ──▶ internal ChatRequest
     • corlinman-server.scheduler ── croniter ───────────▶ gateway AppState
     • corlinman-embedding.vector ─ SQLite FTS5 (BM25) ──────────▶ /admin/rag
```

One language, one process. **corlinman is a pure Python stack:** FastAPI +
uvicorn gateway, `grpc.aio` for the agent sidecar, `docker-py` for plugin
sandboxes, `watchdog`-driven hot reload, SQLite FTS5 (BM25) for keyword
search, structlog, signal-correct shutdown. The agent reasoning loop, all
LLM provider SDKs (anthropic, openai, google-genai, etc.), embedding,
plugin runtime, channel adapters, CLI and gateway all live under
`python/packages/` and share one venv.

Deep dive: [`docs/architecture.md`](docs/architecture.md).

---

## Install paths

Two ways in. **Humans pick the one-liner;** **AI agents read [`deploy/AI_DEPLOY.md`](deploy/AI_DEPLOY.md).**

### For humans — one-line installer

| Path | One-liner | Notes |
| --- | --- | --- |
| **Docker (recommended)** | `curl -fsSL https://raw.githubusercontent.com/sweetcornna/corlinman/main/deploy/install.sh \| bash -s -- --mode docker` | Pulls `ghcr.io/sweetcornna/corlinman:latest` (multi-arch amd64+arm64), falls back to a local build if the registry is unreachable. Needs Docker Engine 24+ with the compose v2 plugin. |
| **Native (uv + systemd)** | `curl -fsSL https://raw.githubusercontent.com/sweetcornna/corlinman/main/deploy/install.sh \| bash -s -- --mode native` | Installs `uv`, clones the repo to `/opt/corlinman/repo`, syncs the workspace, registers a systemd unit. No container runtime needed. |
| **In-place upgrade** | `bash deploy/install.sh --upgrade` (any mode) | Auto-detects docker vs native, pulls/rebuilds the new image or re-syncs the venv, restarts the service, re-probes `/health`. Never touches `$DATA_DIR`. Re-run with `--version vX.Y.Z` to pin a specific release tag. |
| **🇨🇳 China network** | append ` --china` to either fresh-install command above | Switches PyPI → Tsinghua, Docker Hub → DaoCloud, github.com → gh-proxy.com, npm → npmmirror. Auto-enabled when `pypi.org` TTFB > 3s. See [China-region deployment](#-china-region-deployment) below. |
| **🤖 QQ bot sidecar** | append ` --with-qq` to the docker fresh-install command | Layers `docker-compose.qq.yml` so NapCat (OneBot v11) boots alongside corlinman. The installer materialises `.env` from `deploy/.env.template` on first run and prompts you to fill in `QQ_*` / `OPENAI_API_KEY` before re-running. Docker mode only — NapCat is a container. |

Every fresh install starts with a **preflight** (disk ≥ 5 GB, RAM ≥ 1 GB,
port 6005 free, docker/curl/git on PATH, supported OS), then ends with a
**health gate** that polls `/health` until 200 before printing success
— so the URL you click is guaranteed to respond.

Both paths converge on `http://localhost:6005/login`. Sign in with
`admin` / `root`, rotate the password on the **Account & Security** page
you're redirected to, then optionally walk `/onboard` to wire a real LLM
provider (or skip and use the bundled mock provider). After that the
admin UI lives at `http://localhost:6005/admin`.

For headless servers without a browser, `corlinman init` is the
interactive CLI equivalent — walks the same admin password change +
provider key paste + model alias write that the web wizard does, then
restarts the gateway.

### 🇨🇳 China-region deployment

中国大陆部署的瓶颈是 PyPI / Docker Hub / raw.githubusercontent.com 的跨境
延迟。`--china` 自动切换到一组 2026-04 实测仍稳定的镜像：

| 用途 | 镜像 | 实测 TTFB (Tencent Cloud Tianjin) |
| --- | --- | --- |
| PyPI | `https://pypi.tuna.tsinghua.edu.cn/simple` (清华 TUNA) | 0.24s |
| PyPI 备 | `https://mirrors.aliyun.com/pypi/simple/` (阿里云) | 0.08s TTFB / 5s 全量 |
| GitHub clone | `https://gh-proxy.com/https://github.com/...` | 0.53s |
| GitHub raw | `https://gh-proxy.com/https://raw.githubusercontent.com/...` | 0.53s |
| Docker Hub | `https://docker.m.daocloud.io` (DaoCloud) | 0.12s |
| Docker Hub 备 | `https://docker.1ms.run` | 0.17s |
| npm | `https://registry.npmmirror.com` (前 taobao) | 0.91s |
| Debian apt | `mirrors.tuna.tsinghua.edu.cn` | < 0.1s |

**部分 BGP 网络（如腾讯云 Tianjin）反而能直连 `github.com`**——`--china`
模式会先尝试代理 URL，失败时自动回落到直连，二选一不需要操作员判断。

**已停用 / 移除（曾被推荐但 2026 已死或限速严重）**：`ghproxy.com` /
`mirror.ghproxy.com` / `github.moeyy.xyz` / `dockerhub.icu` /
`docker.kubesphere.io` / jsdelivr CDN 对 raw.github 的代理。

**手动覆盖单项镜像**（不需要重写整个 `--china`）：

```bash
# 例：用阿里云 PyPI + 自己自建的 docker registry mirror
CN_PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/ \
CN_DOCKER_MIRROR=https://your.mirror/ \
  curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/sweetcornna/corlinman/main/deploy/install.sh \
    | bash -s -- --mode docker --china
```

可覆盖的变量：`CN_PIP_INDEX` / `CN_GH_PROXY`（设为空字符串关闭 GitHub
代理） / `CN_DOCKER_MIRROR`。

**已经装好之后想换镜像？** PyPI 在 `~/.config/uv/uv.toml`（或环境变量
`UV_INDEX_URL`），Docker 在 `/etc/docker/daemon.json` 的 `registry-mirrors`
数组。改完 `systemctl restart corlinman` / `systemctl restart docker` 即可。

**真离线场景**（VPS 没有外网）：先在能联网的机器上 `docker save` 镜像 +
`uv pip download` 全部 wheel 到本地仓库，scp 过去再装。`docker save
ghcr.io/sweetcornna/corlinman:dev | ssh vps "docker load"` 是最快的搬运姿势。

### For AI agents — prompt-driven deploy

Paste [`deploy/AI_DEPLOY.md`](deploy/AI_DEPLOY.md) into Claude Code / Cursor /
Aider and tell it your VPS host + mode. The prompt covers 7 phases
(inventory → stop old → install → restore config → verify → upgrade →
cleanup) with explicit stop conditions. The install.sh that the AI
invokes already runs preflight + health gate on its own, so the AI's job
is mostly orchestration + verification, not babysitting the bash.

### Environment overrides

`CORLINMAN_VERSION` (git ref / branch, default `main`),
`CORLINMAN_PREFIX` (install root, default `/opt/corlinman`),
`CORLINMAN_DATA_DIR` (data dir, default `$CORLINMAN_PREFIX/data`),
`CORLINMAN_PORT` (gateway port, default `6005`),
`CORLINMAN_HEALTH_TIMEOUT` (post-boot `/health` poll cap in seconds, default `60`),
`CORLINMAN_TAG` (compose image tag, default `latest` — pin to `vX.Y.Z` for prod).

### From source

```bash
# Container path — pull the prebuilt image (or set CORLINMAN_TAG=local
# to force a local build via the bundled compose file).
git clone https://github.com/sweetcornna/corlinman && cd corlinman
docker compose -f docker/compose/docker-compose.yml pull
docker compose -f docker/compose/docker-compose.yml up -d

# Optional: enable Docker-backed plugin sandboxing on trusted hosts.
docker compose -f docker/compose/docker-compose.yml \
  -f docker/compose/docker-compose.sandbox.yml up -d

# Visit http://127.0.0.1:6005/health then http://127.0.0.1:6005/login
# (default admin / root — change on first login). For LLM provider setup,
# either click through the in-UI /onboard wizard or run the CLI:
docker exec -it corlinman corlinman init
```

### Native (build from source)

Requirements: Python 3.12, `uv`, Node 20+, `pnpm`, `protoc`. The
`dev-setup.sh` script now checks all of these up front and prints a
per-platform install hint if anything's missing.

```bash
./scripts/dev-setup.sh                              # prereq check + deps + proto + hooks + doctor smoke
uv sync --all-packages --frozen
pnpm -C ui install && pnpm -C ui build

uv run corlinman init                               # interactive setup wizard (recommended)
uv run corlinman-gateway                            # gateway (FastAPI/uvicorn)
uv run corlinman-python-server                      # agent sidecar (grpc.aio)

uv run corlinman doctor                             # 9-check smoke (config, providers, port, etc.)
```

Data lives in `~/.corlinman/` by default; override with `--data-dir` or
`CORLINMAN_DATA_DIR`. See [`docs/runbook.md`](docs/runbook.md) for the
production deployment playbook (nginx reverse proxy + DNS-01 TLS via
acme.sh + systemd or docker-compose).

---

## Core concepts

### The agent

An agent in corlinman is a Python `reasoning_loop` wrapped around a
provider SDK. It takes a message history, emits tokens + tool calls,
consumes tool results, and iterates until the model signals `stop`. The
Python gateway owns the transport, multiplexes channels onto it, persists
sessions, and enforces governance (rate limits, approvals, timeouts).

Agents are defined as **frontmatter-headed Markdown**
(`~/.corlinman/agents/<name>.md`), hot-editable from the admin UI's
Monaco editor, and routed by the `model` field or per-channel binding.

### Tools (plugins)

Tools are not prompts-in-a-template. Every tool corlinman exposes is a
real program that runs in its own sandbox, communicates over
JSON-RPC 2.0 on stdio (or gRPC for long-lived "service" plugins), and
publishes a JSON Schema that the agent sees directly via OpenAI
tool_call semantics. Three plugin types:

| Type      | Transport                         | Lifetime                               | Use case                                |
| --------- | --------------------------------- | -------------------------------------- | --------------------------------------- |
| `sync`    | JSON-RPC stdio                    | Spawned per call                       | Calculator, HTTP fetch, shell one-shots |
| `async`   | JSON-RPC stdio + `/plugin-callback` | Spawn → return task_id → webhook back | Long jobs (image gen, LLM sub-calls)    |
| `service` | gRPC over UDS                     | Long-lived supervised child            | Stateful integrations (DB pools, Git)   |

Plugins can be written in **any language** (Python, Node, Go, Rust, bash,
…) because the contract is stdio/gRPC + JSON, not a Python import hook.
Optional Docker sandboxing (`docker-py`-driven) enforces memory, CPU,
read-only root, network isolation, and capability drops. Untrusted
plugins can demand a human approval before every call.

Full authoring guide: [`docs/plugin-authoring.md`](docs/plugin-authoring.md).

### Memory

Two layers of persistence, both auditable:

- **Conversation memory.** Per-session append-only message history in
  SQLite (`sessions.sqlite`), trimmed to a configurable message cap,
  keyed by channel binding or client-supplied `session_key`.
- **Knowledge memory (RAG).** Retrieval today: SQLite FTS5 (BM25)
  keyword search via `/admin/rag` (stats, debug-query, FTS5 rebuild;
  see `corlinman_server/gateway/routes_admin_b/rag.py`). Filter by tag,
  debug-query from the admin UI, rebuild from source via the CLI. HNSW
  dense vectors + Reciprocal Rank Fusion + cross-encoder rerank
  (`bge-reranker-v2-m3`) are on the roadmap — see
  [`docs/PLAN_PORT_COMPLETION.md`](docs/PLAN_PORT_COMPLETION.md).

Neither is a black box: every chunk has a `source_path`, every message
has a timestamp, every retrieval scores through the UI.

### Channels

A channel is any producer of `ChatRequest`. corlinman ships with:

- **HTTP + SSE** — OpenAI-compatible `/v1/chat/completions` (stream and
  non-stream), `/v1/embeddings`, `/v1/models`.
- **QQ (OneBot v11)** — forward WebSocket bridge with image/audio
  multimodal forwarding, keyword filtering, per-group / per-sender
  rate limits, NapCat "正在输入..." indicator + heartbeat watcher, file
  uploads via NapCat extension actions, durable inbox so a crash mid-
  reply leaves a breadcrumb.
- **Telegram** — long-poll bot adapter for private + group chats with a
  real-time "is typing…" indicator, a **mutable spinner placeholder**
  that edits in place as tool calls land (`🧠 思考中... → 🔧 调用工具:
  write_file → 📎 已发送文件 → ✍️ 生成回复中... → final reply`), and
  the `send_attachment` builtin tool so the agent can reply with files
  (HTML / PDF / images / voice) instead of dumping raw text.
- **Discord / Slack / Feishu** — text channels with the same routing +
  rate-limit + chat-service plumbing as QQ + Telegram (no status
  spinner yet — Telegram only).
- **Corlinman (in-app `/chat`)** — Claude.ai-grade conversation window
  at `/admin/chat` driven by the same hermes loop as every other
  channel. Implemented as a first-class `Channel` Protocol member
  (`corlinman_channels.corlinman.CorlinmanChannel`, id `"corlinman"`),
  gated by `CORLINMAN_CHANNEL_ENABLED=1` so existing telegram/qq
  deployments stay bit-for-bit identical until you flip it on. Owns
  per-session `asyncio.Queue` so a browser `POST` and an assistant
  token stream meet on the same thread. Exposes
  `/api/channels/corlinman/{send,events,typing,edit,delete,react}`.
- **Scheduler** — `croniter`-driven cron runner that fires an agent at a
  cron expression with a canned prompt template (for daily digests,
  alerting bots, etc.).

Each channel shares the same agent loop — switch models mid-flight with
a config reload, no channel restart. Every channel exposes a unified
`ChannelBinding` to the reasoning loop so per-turn resume, audit logs,
and the permission gate all key on the same `session_key`.

### Governance

- **Approvals.** Configurable per tool: `allow` / `deny` / `prompt`.
  `prompt` parks the tool call, pushes a notification via the SSE
  broadcast, and waits for a human click in the admin UI (or a 5-min
  timeout → auto-deny).
- **Rate limits.** Per-group and per-sender token buckets on channel
  adapters.
- **Config live-reload.** `POST /admin/config` accepts a TOML body,
  validates it, and atomically swaps in the new config without restart
  (restart-required fields are flagged in the response).
- **Observability.** OTel OTLP export (traces + logs with `traceparent`
  propagation across gateway / agent / plugins), Prometheus `/metrics` with 7 metric families
  covering QPS, latency, tool-call rate, backoff, stream inflight,
  RAG stage timings, and plugin execution duration. A bundled
  Grafana dashboard lives in `ops/dashboards/corlinman.json`.
- **Doctor.** `corlinman doctor` (also `make doctor`) runs 9 local
  checks — data dir writability, config TOML parseability, Python
  version, required packages, runtime config loader, provider registry,
  runtime wiring (P1+P2 boot sim), `must_change_password` (warns until
  you rotate the seed `admin/root`), and `port_bindable` (catches "port
  6005 already held" before the gateway tries to start). `--json` mode
  is CI-friendly: every check returns `ok`/`warn`, never `fail`.

---

## Providers

| Provider   | Chat | Streaming | Tool calls | Embeddings | Status       |
| ---------- | :--: | :-------: | :--------: | :--------: | ------------ |
| Anthropic  |  ✅  |    ✅     |     ✅     |    n/a     | production   |
| OpenAI     |  ✅  |    ✅     |     ✅     |     ✅     | production   |
| Google     |  ✅  |    ✅     |     ✅     |     ✅     | production   |
| DeepSeek   |  ✅  |    ✅     |     ✅     |    n/a     | production   |
| Qwen       |  ✅  |    ✅     |     ✅     |    n/a     | production   |
| GLM        |  ✅  |    ✅     |     ✅     |    n/a     | production   |
| _OpenAI-compatible_ (local vLLM, Ollama, SiliconFlow, any gateway speaking the spec) |  ✅  | ✅ | ✅ | ✅ | works via `providers.openai.base_url` |

> **Migrating from newapi**: existing `kind: newapi` entries are silently
> routed through the `openai_compatible` adapter (migration shim in
> `corlinman_providers/specs.py`); set `kind: openai_compatible` in new
> configs. The dedicated `/admin/newapi` surface and onboard wizard
> integration have been removed.

Custom providers are a ~200-line Python class: subclass
`corlinman_providers.base.CorlinmanProvider`, register a model-name
prefix in `registry.py`, and you're in the agent loop. See
[`python/packages/corlinman-providers/`](python/packages/corlinman-providers/).

---

## Admin UI

### In-app `/chat`

The headline operator surface (added in 1.8.0). Renders a Claude.ai-grade
conversation window driven by the existing hermes agent backend:

- **Streaming with full loop visibility** — token-by-token assistant
  text, collapsible Claude-style reasoning blocks, tool-call cards
  (running / ok / error with args + result panes), nested sub-agent
  cards, inline approval prompts (Deny / Approve once / Always-session).
- **Composer** — multiline auto-grow textarea, Enter to send / Shift+Enter
  newline, drag-drop + paste file attachments (50 MB cap), `/` slash
  commands (`/clear`, `/reset`, `/model`, `/persona`), `@`-mention
  picker for agents and skills, reply-with-quote chip above the
  textarea, model + persona pills.
- **Conversation sidebar** — time-grouped list (Pinned / Today /
  Yesterday / Previous 7 / 30 / Older / Archived), fuzzy search,
  rename / pin / archive / delete-with-undo.
- **Artifact panel** — code blocks (≥ 25 lines or `html`/`svg`/
  `mermaid`/`markdown`) surface in a resizable side panel with
  sandboxed iframe preview for HTML, inline SVG render, source view,
  version history, copy + download.
- **Message-level actions** — copy, regenerate, edit-in-place for user
  messages (re-runs the turn after truncating history), branch fork
  into a new session pre-loaded with the slice up to that point,
  reply-quote, jump-to-message.
- **Token + cost meter** — header chip aggregates input/output tokens +
  estimated cost across the entire session.
- **In-conversation search** — Cmd / Ctrl + F overlay walks matches with
  Enter / Shift+Enter.
- **Resume any session** — `/admin/sessions` now exposes a "Continue"
  button per row that routes to `/admin/chat/{sessionKey}` and
  auto-hydrates the full historical transcript via `replaySession()`
  before the composer accepts input. Telegram / qq / scheduled
  persona runs are all resumable in the browser.


A Next.js 15 static-export bundle served by nginx (or directly from the
gateway at `/`). **Tidepool** design system — warm-amber glass with
day + night themes (sun/moon pill in the top nav), Instrument Serif
hero display over Geist sans/mono, `⌘K` command palette, framer-motion
page transitions, live SSE dashboards.

Ten pages covering the full control plane:

- **Dashboard** (`/`) — stat cards + live activity feed (SSE from
  `/admin/logs/stream`) + 7-check system health panel.
- **Plugins** — list with status dots, detail with a schema-driven
  "Test invoke" form that hits `POST /admin/plugins/:name/invoke`.
- **Agents** — list + Monaco editor for agent Markdown with
  frontmatter validation.
- **RAG** — stats cards, debug query box with score bars, confirm-gated
  rebuild trigger.
- **Channels** — per-adapter status lights, connection reset button,
  inline keyword editor, recent-message transcript.
- **Scheduler** — job table with live next-trigger countdown, manual
  trigger button, execution history modal.
- **Approvals** — pending tab (SSE live) + history tab.
- **Models** — provider cards with enabled toggle, inline alias CRUD.
- **Config** — Monaco TOML editor with section nav, JSON-schema hints,
  validation issues panel sliding in from the bottom.
- **Logs** — virtualized list of SSE events with level + subsystem
  filters and a pause-stream toggle.

Every page plays nice with the keyboard and passes WCAG AA contrast
in both themes. Internationalisation (zh-CN / en) ships in 0.1.3.

---

## Configuration

corlinman boots from `$CORLINMAN_DATA_DIR/config.toml` (annotated
example: [`docs/config.example.toml`](docs/config.example.toml)). A
minimum production config looks like:

```toml
[server]
port = 6005
bind = "0.0.0.0"
data_dir = "/data"

[admin]
username = "admin"
# Generate via the onboard wizard, or:
# echo -n 'your-password' | argon2 "$(openssl rand -hex 8)" -id -m 15 -t 2 -p 1 -l 32 -e
password_hash = "$argon2id$v=19$m=32768,t=2,p=1$..."

# Providers are a free-form `BTreeMap<String, ProviderEntry>`. The table
# key is operator-chosen; `kind` selects the wire shape. Full reference:
# docs/providers.md (14 supported kinds + recipes).
[providers.openai]
kind = "openai"
api_key = { env = "OPENAI_API_KEY" }
base_url = "https://api.openai.com/v1"
enabled = true

[providers.anthropic]
kind = "anthropic"
api_key = { env = "ANTHROPIC_API_KEY" }
enabled = true

# Need a CN endpoint or a niche aggregator? Add an OpenAI-compat entry
# with a chosen name — no code changes required. See docs/providers.md §3.
# [providers.openrouter]
# kind = "openai_compatible"
# api_key = { env = "OPENROUTER_API_KEY" }
# base_url = "https://openrouter.ai/api/v1"
# enabled = true

[models]
default = "gpt-4o-mini"

[models.aliases]
smart = "claude-opus-4-7"
cheap = "gpt-4o-mini"

# Optional: QQ bot channel
# [channels.qq]
# enabled = true
# forward_ws_url = "ws://127.0.0.1:6700"
# group_allowlist = [123456789]
```

Everything is hot-reloadable via `POST /admin/config` or the **Config**
page in the admin UI. Restart-required fields (bind address, port,
channel enablement) return `requires_restart: true` and are flagged in
the response.

---

## Production deployment

The deployment reference setup, as used in the hosted demo:

```
Internet ──[HTTPS]──▶ Cloudflare (CDN + edge TLS + DDoS)
                          │
                          ▼
              ┌─────────────────────────┐
              │   nginx on the VM        │
              │  TLS: LE ECC via acme.sh │
              │  DNS-01 (no port 80      │
              │  exposed to ACME)        │
              │                          │
              │  location /admin|/v1...  │── 127.0.0.1:6005 ──▶ corlinman gateway
              │  location /              │── /opt/corlinman/ui-static/ (static files)
              └─────────────────────────┘
```

- **TLS** lives at both the Cloudflare edge (universal SSL) and the
  origin (Let's Encrypt ECC, auto-renewed by acme.sh via a Cloudflare
  API token — DNS-01 challenge, so you never need to punch port 80 out
  for HTTP-01).
- **Static bundle** is served directly by nginx from
  `/opt/corlinman/ui-static/` (rsync target from `ui/out/` on the build
  host). The gateway never fights nginx for static bytes.
- **Hosted demo runtime** is a native systemd deployment: `corlinman.service`
  for the gateway, `corlinman-agent.service` for the Python agent, and a
  Docker `corlinman-napcat` sidecar for QQ.
- **Hosted demo upgrades** pin an explicit release tag, sync the native venv,
  rebuild `ui/out/`, rsync it to `/opt/corlinman/ui-static/`, then restart the
  agent and gateway services. See
  [`docs/RUNBOOK_VPS_PROD_UPDATE.md`](docs/RUNBOOK_VPS_PROD_UPDATE.md).
- **Docker installs** should use the tagged GHCR image and compose upgrade
  path from [`deploy/AI_DEPLOY.md`](deploy/AI_DEPLOY.md).

General troubleshooting, healthcheck wiring, and rollback notes:
[`docs/runbook.md`](docs/runbook.md).

---

## Development workflow

```bash
# Clone + set up hooks, deps, and proto generation.
./scripts/dev-setup.sh

# Run the whole stack in dev mode with hot reload.
corlinman dev

# Full gate (what CI + pre-commit run).
uv run ruff check .
uv run mypy python/packages/
uv run pytest -m "not live_llm and not live_transport"
pnpm -C ui typecheck
pnpm -C ui lint
pnpm -C ui build
bash scripts/gen-proto.sh && git diff --exit-code python/packages/corlinman-grpc/src/corlinman_grpc/_generated/

# Quick local smoke (config + providers + port-bindable + must_change_password).
make doctor                                         # → uv run corlinman doctor
```

Coding expectations, branch + commit conventions, live-lane tests, and
boundary checks all live in [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## Repository layout

```
python/packages/*   Python packages (gateway / agent / providers / embedding / vector / channels / mcp / canvas / plugins / cli / ...)
proto/              Protocol Buffers (gRPC IDL between agent sidecar and gateway)
ui/                 Next.js admin console (static export)
qa/scenarios/       Executable YAML test scenarios
docker/             Compose profiles (sandbox Dockerfile for plugins)
docs/               Architecture / plugin authoring / runbook / roadmap
.git-hooks/         pre-commit (FAST_COMMIT=1 escape hatch)
scripts/            dev-setup.sh, gen-proto.sh
ops/                Grafana dashboard + observability compose
```

---

## Documentation map

- [Quickstart](docs/quickstart.md) — 60-second boot + first-login + skip-to-mock walkthrough
- [Profiles](docs/profiles.md) — isolated agent instances (persona + memory + skills)
- [Credentials](docs/credentials.md) — provider keys + EnvPage UI
- [Evolution & curator](docs/evolution-curator.md) — self-evolution loop ported from hermes-agent
- [Architecture](docs/architecture.md) — message flow, crate/package graph, gRPC bus
- [Providers reference](docs/providers.md) — 14 supported `kind`s + recipes (Ollama / OpenRouter / SiliconFlow / Groq)
- [Plugin authoring](docs/plugin-authoring.md) — write your own sync / async / service plugin
- [Skills, agents & the variable cascade](docs/guides/skills-and-agents.md) — author `skills/`, `agents/`, `TVStxt/*`
- [v0.1 → v0.2 migration](docs/migration/v1-to-v2.md) — manifest v2, vector v6, new config sections, block protocol
- [Runbook](docs/runbook.md) — production deployment + incident handling
- [Milestones](docs/milestones.md) — per-milestone status
- [Roadmap](docs/roadmap.md) — sprint-level plan beyond 1.0
- [Changelog](CHANGELOG.md) — release-by-release
- [Performance baseline](docs/perf-baseline-1.0.md) — p50/p99 numbers

---

## Roadmap + status

**v1.19.1** (current) — one-click upgrade now shows a determinate **progress
bar** to completion, with a clear manual-commands fallback on deployments that
can't self-upgrade. Tagged `v1.19.1`.

**v1.19.0** — **Spatial Glass** admin redesign (visionOS-style glass +
Apple Liquid Glass optics), a Theme Studio for whole-theme colour and glass
opacity, a ChatGPT/Claude-grade chat; plus the 6-hour CI py-test hang fixed and
channel-capability-aware outbound text (native markdown kept on Discord/Slack,
flattened only on plain-text channels). Existing config remains compatible.
Tagged `v1.19.0`. The complete, up-to-date version history (1.1 → 1.19) lives in
[`CHANGELOG.md`](CHANGELOG.md).

**v1.1.0** — channel parity (QQ official bot + WeChat 公众号
land alongside existing channels), Claude-Code-style task UX (live
todo-list view + summary-based context compaction + mid-turn user
message injection), and admin UI simplification (sidebar trimmed to
10 operator pages with a Developer Settings toggle for the rest).
Released 2026-05-24, tagged `v1.1.0`. Full notes in
[`CHANGELOG.md`](CHANGELOG.md#110--2026-05-24--channel-parity--claude-code-style-task-ux).

**v1.0.0** — full Python port, multi-channel chat with status streaming
+ file replies, multi-gateway HA via shared Postgres journal, hook
event bus, context-aware permissions, on-demand skill reload,
production-hardened security (SSRF + sandbox + symlink escape) and
reliability (reactive 401 refresh across every provider). Released
2026-05-24, tagged `v1.0.0`. Post-1.0 work is tracked in
[`docs/milestones.md`](docs/milestones.md) and
[`docs/roadmap.md`](docs/roadmap.md).

Shipped in 1.1 (on top of 1.0):

- ✅ QQ 官方机器人 channel + 微信公众号 channel
- ✅ Discord / Slack / Feishu mutable-spinner parity (the four
  edit-capable channels now share Telegram's `_status.py` core)
- ✅ Live task-list rendering (`📋 任务清单 ☑/▣/☐`) from the
  `todo_write` builtin
- ✅ Claude-Code-style summary-based context compaction (≥ 95 %
  budget triggers a sub-call summary; failure degrades to elision)
- ✅ Mid-turn user-message injection
  (`ReasoningLoop.inject_user_message`) — a follow-up message to a
  busy session merges into the live turn instead of queueing
- ✅ Sessions admin page wired to the journal + Delete /
  Clear-all controls
- ✅ Admin sidebar trimmed to 10 operator items with a Dev
  Settings toggle

Shipped in 1.0 (vs the 0.6.x line):

- ✅ Telegram + Discord + Slack + Feishu channels
- ✅ Mutable-spinner status streaming + typing indicator + `send_attachment` tool
- ✅ Multi-gateway HA via Postgres journal (race-safe `ON CONFLICT`)
- ✅ Hook event bus (`UserPromptSubmit` / `PreToolDispatch` / `ToolCalled` / `TurnComplete` / `TurnErrored`)
- ✅ Context-aware permission gate (per tool × model × session × user_id)
- ✅ Dynamic skill reload (`*.md` per turn, no restart)
- ✅ Per-turn journal resume after gateway / agent restart
- ✅ SSRF + run_shell rlimit + symlink escape hardening

Near-term (P1):

- Plugin SDK packages on npm / PyPI / crates.io
- MCP (Model Context Protocol) compatibility layer — expose corlinman
  plugins as MCP tools to Claude Desktop / Cursor
- OIDC login (replace basic auth)
- Discord / Slack / Feishu status streaming parity (today: Telegram-only)
- Redis journal backend (Postgres landed in 1.0)
- 24 h soak test + fuzz corpus in CI

Longer-term (P2):

- Multi-tenant (data_dir per tenant + RBAC)
- External vector DB backends (Qdrant / Milvus)
- Canvas renderer + voice I/O
- VS Code / JetBrains / Tauri desktop clients

---

## Contributing

Contributions welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for the
architecture invariants (e.g. _no Anthropic-specific types leaking
into the provider trait_), test-lane conventions, and the pre-commit
hooks you'll install. Issues on GitHub for bugs / features.

---

## License

MIT. See [`LICENSE`](LICENSE).

---

## 中文速览

> **1.19.1 新特性**：一键升级新增升级进度条（直到完成）+ 不支持一键升级的部署
> 会清晰引导改用手动命令。延续 1.19.0 的「空间玻璃」后台重设计（visionOS 风格玻璃
> + 苹果液态玻璃光效）、主题工作室、媲美 ChatGPT/Claude 的聊天页，以及 6 小时 CI
> 挂死修复与渠道发送整洁。现有配置保持兼容。
> 完整说明见 [更新日志](CHANGELOG.md)。

**corlinman 是一个可自托管的智能体平台。** 不只是 LLM 的 API 代理，也不是拖拽工作流的工具箱——它是一套有主张的运行时：让语言模型拥有**持久记忆**、**真实工具**、**多通道接入**、**可审计的运维面板**，全部跑在你自己的机器上。

**核心能力**：

- **一个 agent 循环，多家 provider**：在 Anthropic / OpenAI / Google / DeepSeek / Qwen / GLM 上跑 OpenAI 标准 tool_call 语义；配置热重载、按模型别名路由。
- **真工具，不是 prompt 模板**：同步 / 异步 / 常驻三种插件类型，统一 JSON-RPC 2.0 stdio 或 gRPC 通信，可选 Docker 沙箱 + 人工审批闸。
- **跨会话的记忆**：SQLite 会话历史 + SQLite FTS5（BM25）关键词检索经 `/admin/rag` 暴露；HNSW 稠密向量 + RRF 融合 + cross-encoder rerank 在路线图上（见 [`docs/PLAN_PORT_COMPLETION.md`](docs/PLAN_PORT_COMPLETION.md)）。
- **通道作为一等公民**：QQ (OneBot v11) / Telegram / Discord / Slack / Feishu / 定时任务 / OpenAI 兼容 HTTP/SSE 并行接入，共享同一 agent 循环。Telegram 端有实时「正在输入...」指示器 + 工具调用流式状态条 + `send_attachment` 文件发送工具；QQ 通过 NapCat 扩展拿到同样的输入状态 + 文件上传能力。
- **严肃的运维面板**：**Tidepool** 暖橙玻璃风格 Next.js 管理界面（日 / 夜双主题，插件 / 知识库 / 日志 / 审批 / 配置 / 调度器 / 模型路由），OTel + Prometheus 埋点，9 项 `doctor` 体检。

**在线 demo**：<https://corlinman.cornna.xyz>

**架构**：纯 Python 单语言栈 —— FastAPI/uvicorn gateway + grpc.aio agent sidecar + 插件 runtime + SQLite FTS5（BM25）检索 + CLI + provider SDK + reasoning loop + embedding 全部在 `python/packages/` 下，共享一个 venv；W3C `traceparent` 贯穿全链路。

**快速开始**（60 秒）：

```bash
# 一行装好，全自动 preflight → 拉镜像 → 启动 → 等 /health 200 → 打印 URL
curl -fsSL https://raw.githubusercontent.com/sweetcornna/corlinman/main/deploy/install.sh | bash

# 国内网络加 --china（清华 PyPI / gh-proxy / DaoCloud），TTFB > 3 秒会自动开
curl -fsSL https://raw.githubusercontent.com/sweetcornna/corlinman/main/deploy/install.sh | bash -s -- --china

# 想顺便起 NapCat QQ 机器人就再加 --with-qq（docker 模式）
curl -fsSL https://raw.githubusercontent.com/sweetcornna/corlinman/main/deploy/install.sh | bash -s -- --china --with-qq
```

装完打开 `http://<服务器>:6005/login`，用 `admin` / `root` 登录后会被
强制跳到 **账户安全** 改密码。想给一个真 LLM？走 UI 里的 `/onboard` 或
在服务器上跑 `corlinman init`（headless 交互式向导，免浏览器）。

**升级**：`bash deploy/install.sh --upgrade`，自动识别 docker 还是
native 模式，拉新镜像/重 sync venv，重启服务，重跑 /health。不会动数据目录。
自 1.10.0 起，native 模式下 gateway 以非特权 `corlinman` 用户运行（此前为
root），升级时会自动重生并重载 systemd unit——无需人工操作；若你定制过该
unit，请把覆盖项放进 systemd drop-in 以免被覆盖。

数据默认落在 `~/.corlinman/`，通过 `CORLINMAN_DATA_DIR` 覆盖。完整生产部署（nginx + acme.sh DNS-01 + Cloudflare）见 [`docs/runbook.md`](docs/runbook.md)，架构细节见 [`docs/architecture.md`](docs/architecture.md)。
