# 为 corlinman 贡献代码

感谢你考虑为 corlinman 贡献代码。本文说清楚三件事：怎么搭开发环境、提交 PR 的规则、代码风格要求。

**前置**：先读 [docs/README.md](docs/README.md) 和 [docs/architecture.md](docs/architecture.md) 了解系统全貌；
模块归属与 owner-area 划分见 [docs/pr-standards.md](docs/pr-standards.md)。Codex PR 评审流程见
[.github/CODEX_REVIEW.md](.github/CODEX_REVIEW.md)。

仓库现在是 **全 Python**（由 `uv` 管理的 workspace）加一个 Node/pnpm 的 `ui/` 前端。没有其他语言平面。

## 1. 开发环境搭建

只需一次：

```bash
git clone https://github.com/<org>/corlinman.git
cd corlinman
bash scripts/dev-setup.sh
```

`dev-setup.sh` 做了这些事（幂等，可重复跑）：

1. 检查 toolchain：Python ≥ 3.12、Node 20、pnpm（建议 `corepack enable`）、`uv`、`protobuf-compiler`。缺哪个给装哪个的 hint。
2. 把 `core.hooksPath` 指到 `.git-hooks/`，安装 pre-commit 钩子。
3. `uv sync --all-packages --dev` 把整个 workspace 装进虚拟环境。
4. `pnpm install` 装前端依赖。
5. 跑一次 `scripts/gen-proto.sh`，生成 Python gRPC stubs。

如果想手动搭，等价命令是：

```bash
uv sync --all-packages --dev        # Python workspace（25 个包）+ dev 工具
pnpm install --frozen-lockfile      # ui/ 前端依赖
bash scripts/gen-proto.sh           # 生成 proto stubs（需要 protobuf-compiler）
```

之后常用命令：

```bash
uv run pytest <path>                                      # 跑指定路径的 Python 测试（推荐）
uv run pytest -m "not live_llm and not live_transport"   # 跑非 live lane 的全套（CI 用的口径）
uv run ruff check .                                       # lint
uv run mypy python/packages/                              # 类型检查
uv run lint-imports                                       # 模块边界（import-linter）检查
pnpm -C ui test                                           # 前端测试（vitest）
pnpm -C ui typecheck                                      # 前端类型检查
```

> **提示**：本地不要随便跑整套 pytest，见下面 §4 关于 `py-test` 偶发挂死的说明。优先用 `uv run pytest <path>` 跑你改到的目标测试。

## 2. 仓库结构

仓库是一个 `uv` workspace：`python/packages/corlinman-*` 下 25 个 Python 包 + `ui/` 前端 + `proto/` 接口。完整的现状模块地图（职责、公开接口、依赖、耦合热点）见 [docs/architecture-modules.md](docs/architecture-modules.md)；模块化路线图见 [docs/modularization-plan.md](docs/modularization-plan.md)。

### 2.1 分层

Python 核心 gRPC 平面自上而下分层（`.importlinter` 强制，高层可 import 低层，反向禁止）：

```
corlinman_server   (顶层 — gRPC 入口 / HTTP+WS 网关 / 管理面)
  └── corlinman_agent       (推理循环 + 工具)
        └── corlinman_providers   (provider 适配 + 插件平台)
              └── corlinman_grpc  (底层 — 生成的 stubs + client 基类)
```

其余包是同层 peer（memory / evolution / channels / 基础设施等），**不互相 import**，只被上层组装。

### 2.2 完整模块与归属（owner-area）

`corlinman-server`（约 85K LOC，gateway 单体）按内部子包划分归属：

| 区域（`corlinman_server/...`） | 职责 | owner-area |
| --- | --- | --- |
| `gateway/lifecycle` `gateway/core` `gateway/middleware` | 启动编排、`AppState`/`AdminState`、中间件 | `@corlinman/gateway-lead` |
| `gateway/routes` `gateway/routes_voice` `gateway/oauth` | 公共 API、语音 WS、OAuth 流 | `@corlinman/voice-chat-platform-team` |
| `gateway/routes_admin_a` | 管理控制面（凭据/会话/租户/persona CRUD） | `@corlinman/admin-control-plane-team` |
| `gateway/routes_admin_b` | 管理后端（config/models/providers/skills/evolution/scheduler 等 27 个子路由） | `@corlinman/admin-backend-team` |
| `gateway/services` `gateway/evolution` `gateway/channels_runtime` `gateway/grpc` `gateway/providers` `gateway/placeholder` `gateway/mcp` `gateway/observability` `gateway_api` | 运行时编排：chat pipeline、演化、channel 接线、gRPC 桥、provider 注册 | `@corlinman/runtime-orchestration-team` |
| `system` | 更新检查、审计日志、marketplace/skill-hub、subagent 宿主 | `@corlinman/system-integration-team` |
| `scheduler` `tenancy` `persona` `profiles` `cli` `tools` `bundled_skills` | 平台服务：调度、多租户、persona、profiles、CLI | `@corlinman/platform-services-team` |

独立包：

| 包 | 职责 | owner-area |
| --- | --- | --- |
| `corlinman-agent` `corlinman-agent-brain` | 多轮 agentic 推理循环、工具套件、子 agent 派生、上下文组装 | `@corlinman/reasoning-agent-team` |
| `corlinman-channels` | 7 个 channel 适配器（QQ/Telegram/Discord/Slack/Feishu/WeChat/LogStream）、入站归一化、限流、状态回写 | `@corlinman/channels-gateway-team` |
| `corlinman-providers`（`specs`/`registry`/`declarative`/`capabilities` + 各 vendor 适配） | 统一 provider 协议、错误归一、配置 specs | `@corlinman/provider-adapters-team` |
| `corlinman-providers/.../plugins` | 插件清单/沙箱/生命周期/审批/发现 | `@corlinman/plugin-platform-team` |
| `corlinman-memory-host` `corlinman-episodes` `corlinman-tagmemo` `corlinman-user-model` `corlinman-replay` | 混合记忆检索、情节、标签记忆、用户模型、会话回放 | `@corlinman/memory-backend-team` |
| `corlinman-evolution-engine` `corlinman-evolution-store` `corlinman-shadow-tester` `corlinman-auto-rollback` | 演化环：信号聚类 → 提案 → 影子测试 → 自动回滚 | `@corlinman/evolution-engine-team` |
| `corlinman-goals` | 目标跟踪、反思评分、证据窗口 | `@corlinman/goals-intelligence-team` |
| `corlinman-persona` `corlinman-identity` `corlinman-grpc` `corlinman-wstool` `corlinman-nodebridge` `corlinman-skills-registry` `corlinman-subagent` `corlinman-hooks` `corlinman-mcp-server` `corlinman-canvas` | 基础设施：会话/身份、gRPC stubs、分布式工具总线、设备桥、skill 注册、子 agent 监督、hook 总线、MCP server、canvas 渲染 | `@corlinman/foundation-infrastructure-team` |
| `ui/` | Next.js 前端（Node 20 + pnpm） | `@corlinman/voice-chat-platform-team` |

> owner-area 与 CODEOWNERS 路由的权威映射在 [docs/pr-standards.md](docs/pr-standards.md) §7。改动跨 owner-area 时需对应团队 review。

### 2.3 其它

- `ui/`：所有前端命令用 `pnpm -C ui <script>`。
- `proto/corlinman/v1/*.proto`：proto 定义。生成的 stubs 在 `python/packages/corlinman-grpc/src/corlinman_grpc/_generated/`，**必须提交**——CI 的 `proto-sync` 会校验无 drift。
- `.importlinter`：Python 平面分层契约，CI 的 `boundary-check` 据此强制模块边界（`uv run lint-imports` 本地自查）。
- `.github/workflows/ci.yml`：合入门禁。

## 3. 开发工作流

### 3.1 起新分支

```bash
git checkout main
git pull
git checkout -b feat/marketplace-search-plugins
```

分支命名：`<type>/<short-slug>`，`type` 和下面 commit 规范里的相同。

### 3.2 写代码

参考 [docs/architecture.md](docs/architecture.md) 和 [docs/pr-standards.md](docs/pr-standards.md) 找你要改的 package / 区域以及它的 owner-area。

**小 PR 优先**：一个 PR 一个关注点。大 refactor 拆成多个 PR（先结构调整，再行为变更）。

**尊重模块边界**：Python 核心 gRPC 平面是分层的（`corlinman_server → corlinman_agent → corlinman_providers → corlinman_grpc`，高层可以 import 低层，反向不行；同层 peer 包之间不互相 import）。契约写在 `.importlinter`，本地用 `uv run lint-imports` 自查。新增的反向 import 会被 `boundary-check` 拦住。

**改 proto 要谨慎**：`proto/corlinman/v1/*.proto` 的字段编号和语义不能向后不兼容。加字段 OK；改字段类型 / 删字段 / 重排编号要先开 issue 讨论。改完务必跑 `bash scripts/gen-proto.sh` 并把重新生成的 stubs 一起提交，否则 `proto-sync` 会失败。

### 3.3 提交前

pre-commit 钩子（`.git-hooks/pre-commit`）会对暂存的文件自动跑 `uv run ruff check`、`uv run mypy`、`pnpm -C ui typecheck`。但合入门禁更严格，最好手动先跑一遍门禁等价命令：

```bash
uv run ruff check .
uv run mypy python/packages/
uv run lint-imports
uv run pytest <你改到的路径>
pnpm -C ui typecheck
pnpm -C ui lint
pnpm -C ui test
```

紧急情况（生产 hotfix，钩子在本地误报但你确定改的部分没问题）可以用逃生舱：

```bash
FAST_COMMIT=1 git commit ...
```

但 CI 上不会有 `FAST_COMMIT`，PR 仍会被门禁拦住。

### 3.4 提 PR

```bash
git push -u origin feat/marketplace-search-plugins
gh pr create
```

PR 模板（[.github/PULL_REQUEST_TEMPLATE.md](.github/PULL_REQUEST_TEMPLATE.md)）会要求你填：**改动摘要**、**类型**、**行为证明（Behavior Proof）**、**风险/回滚**、**关联 issue**。

PR 开出来后会走 Codex 自动评审流程（见 §6）。状态标签由
[.github/workflows/pr-status-labels.yml](.github/workflows/pr-status-labels.yml) 自动打，不用你手动管。

## 4. 合入门禁（CI Gate）

合入 `main` 前，下面 8 个 job 必须全绿（聚合在 `gate (all required checks)` 这一个必需检查里）：

| job | 命令 |
| --- | --- |
| `py-ruff` | `uv run ruff check .` |
| `py-mypy` | `uv run mypy python/packages/` |
| `py-test` | `uv run pytest -m "not live_llm and not live_transport"` |
| `ui-typecheck` | `pnpm -C ui typecheck` |
| `ui-lint` | `pnpm -C ui lint`（eslint，作用域 `ui/`） |
| `ui-test` | `pnpm -C ui test`（vitest，作用域 `ui/`） |
| `boundary-check` | `uv run lint-imports`（import-linter / `.importlinter`） |
| `proto-sync` | `bash scripts/gen-proto.sh`，然后校验 `_generated/` 下的 stubs 已提交、无 drift |

### ⚠️ 已知坑：`py-test` 偶发挂到 6 小时 CI 上限

`py-test` job **会偶尔挂死，一路顶到 6 小时的 CI 上限**。这是一个已知的基础设施 flaky 问题，**`main` 上也会发生**，跟你的改动通常没关系——同一套测试在本地 Python 3.12 / 3.13 上能正常跑过、正常通过。

给贡献者的指引：

- **这不是你的锅**。先别去 debug 你的 diff。
- **直接 rerun 这个 job**。绿色门禁有时候需要一次走运的 rerun，或者由 admin merge。
- **本地别跑整套**。用 `uv run pytest <path>` 跑你改到的目标测试来获得快速反馈，不要本地复现整套挂死。
- 如果某次 rerun 仍然挂死，在 PR 里 ping 维护者，由 admin 走门禁合入。

## 5. 提交信息约定（Conventional Commits）

格式：`<type>(<scope>): <subject>`，正文和 footer 可选。本仓库实际就这么用（跑 `git log --oneline -30` 看真实风格），例如：

```
feat(marketplace): add search plugin bundles and docs
fix(channels): restore replies — tolerate persona_id-less channel requests
fix(gateway/auth): bridge admin session to /v1/chat so in-app chat works
chore: add PR status label automation
docs(pr): document Codex PR review flow
```

允许的 `type`：

| type | 含义 |
| --- | --- |
| `feat` | 新功能（对用户可见） |
| `fix` | bug 修复 |
| `chore` | 构建、依赖、CI、脚本、维护 |
| `docs` | 仅文档 |

`scope` 是受影响的 package / 区域，例如 `channels` / `gateway` / `providers` / `ui` / `marketplace` / `proto` / `docs`，也可以更细（如 `gateway/auth`、`admin/config`、`persona/ui`、`telegram`）。多个 scope 用 `/` 分隔或省略。

**subject 用现在时祈使句**：`add X`、`fix Y`，不是 `added` / `adds`。

## 6. Codex 评审流程

本仓库用一套 PR 状态系统，让评审者从标签就能看出当前状态，而不用从头读时间线。完整说明见 [.github/CODEX_REVIEW.md](.github/CODEX_REVIEW.md)，默认流程：

1. 开一个聚焦的 PR，标题用 `type(scope): concise change`。
2. 对用户可见的改动附上行为证明：测试、截图、视频、日志、curl 输出或 before/after 说明。
3. 让 Codex 自动评审跑起来（建仓配置为：PR 创建时评审，之后每次 push 再评审一次）。
4. 评审结果看起来 stale 时，在 PR 里评论 `@codex review`。
5. 把 Codex 的 `eyes` reaction 当作"已收到"，然后等真正的评审评论或 👍。
6. 从最新的 bot 评论、评审线程和证据来判断 PR 状态——不要只信 stale 标签。

状态标签由 `pr-status-labels.yml` 自动维护，常见的有：`codex:needs-review`、`codex:review-requested`、`codex:reviewed`、`codex:needs-rerun`、`codex:setup-issue`，以及 `status: 🔁 re-review loop`、`status: 🛠️ actively grinding`、`status: 📣 needs proof`、`status: 👀 ready for maintainer look`、`status: ⏳ waiting on author`、`status: ✅ merge-ready`、`status: 🚧 blocked`。证明类标签：`proof: missing` / `proof: supplied` / `proof: sufficient` / `proof: 📸 screenshot` / `proof: 🎥 video`。

## 7. 测试要求

- **新代码必须带测试**。改 bug 必须先写一个能复现的失败测试，再修。
- **Python**：测试用 `pytest`，写在各包的 `tests/` 下。live lane（真打 provider API / 真起 channel 端点）用 marker 标注：`@pytest.mark.live_llm`、`@pytest.mark.live_transport`，CI 默认 `-m "not live_llm and not live_transport"` 跳过。
- **前端**：`ui/` 用 vitest（`pnpm -C ui test`）。
- 本地优先用 `uv run pytest <path>` 跑目标测试（见 §4）。

## 8. PR 合入清单

合入 `main` 前，PR 必须：

- [ ] 8 个门禁 job 全绿（`py-ruff`、`py-mypy`、`py-test`、`ui-typecheck`、`ui-lint`、`ui-test`、`boundary-check`、`proto-sync`）——`py-test` 偶发挂死时按 §4 处理。
- [ ] Conventional Commits 风格的标题。
- [ ] 带测试（新 feature 或 bug fix）。
- [ ] 行为证明已附（对用户可见 / UI 改动尤其需要）。
- [ ] 改 proto → 重新生成并提交 `_generated/` stubs。
- [ ] 不引入新的反向 import（`uv run lint-imports` 本地通过）。
- [ ] 触及别的 owner-area 时，由对应 CODEOWNERS approve（见 [docs/pr-standards.md](docs/pr-standards.md)）。
- [ ] Codex 评审已过或已请求（见 §6）。

**禁止**：

- `--no-verify` / `FAST_COMMIT` 绕过钩子来推 PR（CI 会重跑拦住，浪费时间）。
- 一个 PR 同时改 3+ 不相关的 feature。
- 大量 drive-by formatting（写你改的函数就好）。

## 9. 分支策略

- `main` —— 通过 PR 合入，要求门禁全绿。
- `feat/*` / `fix/*` / `chore/*` —— 短期分支，从 `main` 拉，合回 `main`。
- 不鼓励长期 topic branch；拆小 PR 勤合 main。

## 10. 行为准则

工作语言中英皆可（docs 里为了中文用户以中文为主，技术术语保留英文；代码 comment 英文）。尊重他人、就事论事。

安全漏洞请**不要**公开开 issue。私下联系维护者报告。

## 延伸阅读

- 架构与模块图: [docs/architecture.md](docs/architecture.md)
- 现状模块地图: [docs/architecture-modules.md](docs/architecture-modules.md)
- 模块化路线图: [docs/modularization-plan.md](docs/modularization-plan.md)
- PR 标准与 CODEOWNERS 路由: [docs/pr-standards.md](docs/pr-standards.md)
- Codex 评审流程: [.github/CODEX_REVIEW.md](.github/CODEX_REVIEW.md)
- CI 各 job 期望: [docs/ci-status.md](docs/ci-status.md)
- 运维手册: [docs/runbook.md](docs/runbook.md)
