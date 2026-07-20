# Changelog

All notable changes to corlinman are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[SemVer](https://semver.org/spec/v2.0.0.html).

## [1.35.0] — 2026-07-20 — gap-close backlog lands: 7 PRs + ordering fix

### fix: deterministic turn ordering on same-ms ties (#161)

- `get_session_turn_ids` (SQLite + Postgres) and `find_resumable_turn`
  now carry the same `(started_at_ms DESC, turn_id DESC)` contract as
  `list_session_turns` — two turns seeded within one wall-clock ms no
  longer come back in scrambled natural-row order (surfaced as a
  full-suite-only flake in the fork faithful-copy test).

### Dim 5 MCP leftovers 2/2 — `.mcp.json` layered scopes + MCP client resources (#116)

> claude-code parity Dim 5 收官：四项残留全部关闭。
> Dim 5 leftovers, part 2 of 2 — the last two verified-open items close.

### Added
- **`.mcp.json` 分层作用域**（`corlinman_mcp_server.scoped_config`）——
  claude-code 式 server 配置发现：`local`（`<项目>/.mcp.local.json`，
  gitignored）> `project`（`<项目>/.mcp.json`，入库）> `user`
  （`~/.corlinman/mcp.json`）> 网关内联 `[mcp]` 配置；按 server 名去重、
  强作用域胜出。文件形态兼容 claude-code 的 `{"mcpServers": {…}}`
  （`mcp_servers`/`servers` 别名亦可）。坏文件/坏条目只跳过不崩溃。
  网关 lifespan 与嵌入式控制台两个启动点同时接线。
- **MCP client resources**（`resources/list` + `resources/read`）——
  连接期分页发现（无该能力的 server 干净地降级为空）；每个有资源的
  server 合成一个 `{server}_read_resource` 工具进入现有广告/路由通道
  （schema 带资源清单、上限 10 条防膨胀；server 自带 **literal**
  `read_resource` 工具时合成让位、走正常 `tools/call`——advertise 与
  bridge 两侧同一 literal-wins 规则）；`McpToolBridge` 新增
  `resources/read` 分支，text 内容折叠、blob 以占位说明呈现；
  allow/deny server 策略同样约束资源面。

### Environment seam + Docker per-process sandbox backend (Wave D) (#120)

> claude-code parity Wave D（Dim 4 sandbox，phase 1）：三个代码执行工具
> （前台 `run_shell`、后台 shell 任务注册表、持久 REPL）的进程孵化统一
> 收敛到一个 `Environment` 缝合层——默认 local 后端与历史行为字节等价，
> 新增 docker 后端把每个孵化进程放进自己的一次性容器。
> Wave D of the parity program (Dim 4 sandbox, phase 1): all three
> code-execution tools now spawn through a single `Environment` seam — the
> default local backend is byte-identical to historical behavior, and a new
> docker backend runs each spawned process in its own throwaway container.

### Added
- **`Environment` 缝合层**（D1，`corlinman_agent.coding.environment`）——
  `spawn_shell` / `spawn_repl` 返回 `SpawnedProcess` 句柄（`.proc` +
  `kill()` + `reap()`）：终止逻辑随孵化的子进程走而不是挂在后端上——
  持久 REPL 会话比孵化它的调用活得久，kill 不能依赖事后重新解析后端。
  `LocalEnvironment`（默认）逐字节复刻历史 `create_subprocess_*` 调用
  （workspace cwd、env 白名单、POSIX rlimits + `setsid` preexec），全部
  既有行为与测试不变；`_build_child_env` / `_preexec_apply_rlimits` /
  `kill_process_group` / `reap_orphan_group` 原样从 `shell.py` 迁入并
  re-export（shell 依赖 environment，留在原处会循环导入）。
- **Docker 后端**（D2，`CORLINMAN_SANDBOX_BACKEND=docker`）——
  **一进程一容器**：每次孵化 = 一个 `docker run --rm`，容器名
  `corlinman-sbx-<uuid>` 即 kill token（`docker kill <name>` 原生精确，
  宿主 `killpg` 够不到容器内 PID；不用长驻容器 + `exec`，其 PID 追踪
  脆弱）。加固旗标：`no-new-privileges`、`--pids-limit=64`、
  `--memory=2g`（swap 同额）、`--ulimit nofile/cpu/fsize` 对齐本地
  rlimits；只转发 `LANG`/`LC_ALL`/`TZ`（宿主 `PATH`/`HOME`/`USER` 与
  一切密钥永不进容器）；REPL 直接跑镜像内解释器，只 `-i` 绝不 `-t`
  （tty 会破坏 marker 帧协议）。镜像知
  `CORLINMAN_SANDBOX_IMAGE`（默认 `python:3.12-slim-bookworm`）、
  用户重映射知 `CORLINMAN_SANDBOX_USER`（默认镜像自身用户；Linux
  宿主想避免 workspace 出现 root 属主文件时设 `uid:gid`）。docker
  故障以 `SandboxSpawnError(OSError)` / `DaemonUnavailableError` 抛出
  ——既有 except-OSError → `spawn_failed` 信封路径零改动复用；docker
  二进制缺失在孵化时就大声失败，绝不静默回落到宿主执行。
- **后端选择器**（D3，`get_environment`）—— `CORLINMAN_SANDBOX_BACKEND`
  默认 `local`，未知值抛出点名该变量的 `RuntimeError`；每次调用现读、
  不缓存，与 `open_backend_from_env` 同款形状。

### Fixed / Hardening（审查阶段预先消除的二阶边界）
- **docker 客户端自建会话**——`docker run` 客户端不 fork 时与服务器同
  进程组，后台任务终局 reap 的 `killpg` 会连服务器一起 SIGKILL；现在
  docker 客户端一律 `preexec_fn=os.setsid`（刻意不套 rlimits——那会
  绑住宿主 docker CLI，容器限额由 `--ulimit` 负责），且注册表 7 个
  终局路径全部改走句柄感知的 `_reap_task`（非本地句柄先
  `handle.reap()` 做容器原生清理，本地路径字节不变）。
- **前台超时不再可能挂死**——`DockerSpawnedProcess.kill()` 在
  `docker kill` 之后同时清扫 setsid 后的客户端进程组：即使 daemon
  挂死，dispatcher 的 `await proc.wait()` 也必然解除（该极端下容器
  可能泄漏——挂死的 daemon 无法受托删除它——属已记录的取舍）。
- 已记录取舍：`docker kill` 为兼容 `atexit` 走同步 `subprocess.run`
  （超时 10s 有界），daemon 挂死时单次终局清理最多阻塞事件循环 10s
  ——仅 opt-in 的 docker 后端受影响，local 默认路径零变化。

### Notes
- 沙箱**不是**安全边界的替身（见 `shell.py` 模块文档）：workspace
  bind-mount 刻意可读写、容器联网保持开启（与本地 shell 对齐，构建要
  拉依赖）、`_DENY` 前置筛查照旧对所有后端生效。docker 后端收窄的是
  宿主 workspace 之外的面与网关进程内密钥的暴露。

### Wave E parity — settings persistence, exit_plan_mode, --fork-session, fallback signature strip, hermetic tests (#119)

> claude-code parity Wave E（小批量五连）：权限设置终于能落盘分层加载、
> 模型可以自己结束 plan mode、会话可以无损分叉、跨模型 fallback 不再被
> thinking 签名炸出 400、测试套件对宿主路由环境免疫。
> Wave E of the parity program: durable layered permission settings, a
> model-callable `exit_plan_mode`, `--fork-session`, cross-model
> thinking-signature stripping on fallback, and hermetic tests.

### Added
- **分层权限设置持久化**（E1，`corlinman_agent.permission_settings`）——
  `from_layered_sources` 此前零生产调用方：所有部署只从 env 建 gate，
  durable 规则只能写环境变量。新增加载器按
  `<data_dir>/settings.json`（用户层）→ `<项目>/.corlinman/
  settings.local.json`（项目层，gitignored 惯例同 claude-code）→
  `CORLINMAN_AGENT_PERMISSIONS` env（最终话语权）叠层；`mode`/`strict`
  同样 env > 项目 > 用户。无任何设置文件时与旧 `from_env()` 字节等价
  （含 first-match-wins 默认）。坏文件/坏块只跳过不崩溃。
  `ApprovalGate` 与 servicer 的默认 gate 构造双双切换到新加载器。
- **控制台审批第三答案 `p`/`persist`**（E1）—— 交互审批在
  y（一次）/ a（本会话）之外新增 **persist**：等同 always 并通过
  `persist_allow_rule` 原子写入（tmp + rename、幂等去重）用户层
  `settings.json`，未来会话不再询问；persist 钩子失败降级为会话级
  授权而不是拒绝（操作员明明答了 allow）。`a` 保持会话级语义不变
  （Codex #104 —— 授权不得越过其上下文边界）。
- **`exit_plan_mode` 内置工具**（E2，claude-code 对标）—— plan mode
  会拒掉全部 mutating 工具，此前只能靠人 `/permissions` 手动放行。
  现在模型计划就绪后自己调用：plan → default 翻转 + 交互审批缓存
  重置（两个 resolver 来源 —— `set_approval_resolver` 与
  `app_state.approval_resolver` —— 都重置，plan 期间答的 always
  不得带进实施阶段）；非 plan mode 下干净 no-op；可选 `plan` 摘要
  回显给用户。**子代理拒绝**：权限 mode 是 servicer 级全局的，
  plan 模式下派生的 subagent 不得替父回合结束 plan mode（child
  executor 与递归 spawn / 后台 shell 同款拒绝信封）；skill
  allowed-tools 控制工具直通名单同步加入，防止 skill 把模型困死在
  plan mode。
- **`--fork-session`**（E3，claude-code 对标）——
  `AgentJournal.fork_session` 把源会话的 **completed** 回合按时间序
  复制到全新 session key（in_progress 在别处直播、errored 带 T4.4
  面包屑，均不复制；单回合损坏只跳过不废整个 fork；源会话全程只读）。
  控制台 `--fork-session` 旗标在恢复前铸新 `console:<id>` key 并
  fork，原会话零污染;attach 模式回显不支持说明后不 fork 继续。
- **fallback 跨模型 thinking 签名剥离**(E4,`reasoning_loop`)——
  thinking 块签名按模型铸造;OpenAI 兼容入口带进来的客户端历史在
  model-not-found / 持续过载两条 fallback 分支切换模型重放时,会把
  可恢复错误变成硬 400。现在两条分支在重放前剥掉
  `thinking`/`redacted_thinking` 块与 `signature` 键(纯 thinking
  消息内容折叠为 `""`,部分后端拒绝空块列表)。
- **测试封闭性**(E5,根 `conftest.py`)—— 宿主机泄漏的
  `ANTHROPIC_BASE_URL`/代理 env 会把 respx-mock 的 provider 测试
  重路由到真网络造成假失败;autouse fixture 对非 live 标记的测试
  统一清洗 7 个路由变量(测试自身 `monkeypatch.setenv` 仍生效,
  `live_llm`/`live_transport` 不受影响)。CI 里原先的
  `env -u ANTHROPIC_BASE_URL` 包装不再需要。

### #108 residuals — always-on subagent registry feed + resume user bubble + crashed-turn replay (#118)

> Closes the last three #108 items. The live-subagents panel no longer
> depends on someone having a chat page open, a resumed in-flight turn
> shows the question it is answering, and a crashed turn's messages stop
> vanishing from the transcript.

### Added
- **Process-wide subagent journal tail** (#108 item 2). A gateway
  background task (`run_journal_subagent_tail`) now tails the journal's
  `Subagent*` lifecycle events into the `LiveSubagentRegistry`
  continuously. Previously the ONLY cross-process feed point was the
  per-session SSE poll, so in `grpc_agent` mode `/admin/subagents` stayed
  empty unless that exact session's chat page was open. The tail is
  forward-only (cursor seeded at the boot high-water mark), idempotent
  with the SSE-poll feed, race-free (bounded scan against a snapshotted
  `MAX(rowid)`), and best-effort throughout. New journal surface:
  `latest_event_rowid()` + `load_subagent_events_since()` (SQLite;
  Postgres degrades to a no-op alongside the rest of the event plane).
  进程级子代理事件尾随：`/admin/subagents` 不再依赖恰好有人打开该会话的
  聊天页才能看到 grpc_agent 模式下的子代理。
- **Resume user bubble** (#108 item 3). Returning to a chat with an
  in-flight turn now renders the user's message (from the turn's
  `user_text_preview`) above the live streaming bubble — previously the
  assistant streamed a reply to nothing, because the settled transcript
  excludes the in-progress turn wholesale. The synthetic bubble is washed
  out by the authoritative transcript refetch when the turn settles.
  回到进行中的会话时，问题气泡随直播气泡一起渲染。

### Fixed
- **L-103 — crashed in-progress turns vanished from the transcript.**
  `_replay_from_journal` skipped EVERY `in_progress` turn; for a turn
  that crashed mid-run (never completed) and was followed by newer turns,
  the user's message and any partial answer disappeared from the thread
  forever. The skip is now scoped to the one turn that can legitimately
  still be live — the newest turn of the first page; older `in_progress`
  rows are crash artifacts and replay as real history.
  崩溃的历史 turn 不再从会话记录中消失（跳过逻辑收窄到最新 turn）。

### Dim 5 MCP leftovers 1/2 — sampling completer prod wiring + /mcp command + embedded MCP plane (#115)

> claude-code parity Dim 5 剩余四项中的两个 M 级项。
> Dim 5 leftovers, part 1 of 2: the sampling responder finally runs real
> completions, and the console gains claude-code's `/mcp` — backed by a
> real MCP client plane in embedded (Mode A) sessions.

### Added
- **MCP sampling completer（生产接线）** — v1.26.0 的 `SamplingResponder`
  机制此前没有任何 completer 写入（`state.extras["mcp_sampling_completer"]`
  零写入方 → 永远 `sampling_unavailable`）。新增
  `gateway/mcp/sampling_completer.py`：包装网关 **live** provider
  registry（逐调用读取 —— provider bootstrap 晚于 MCP 块、热重载会换
  handle），流式 token 折叠为单结果，reasoning 增量绝不泄漏给请求方
  MCP server；`finish_reason` → MCP `stopReason` 映射。registry 未就绪
  时干净地降级为单请求 `INTERNAL_ERROR`。
- **`/mcp` 控制台命令**（claude-code 对标）——
  `list / tools [server] / add <name> <cmd|url> [args…] / remove /
  restart / test / enable / disable`。attach 模式与降级路径回显
  unavailable（与 `/hooks` 同款约定）。
- **嵌入式（Mode A）MCP 客户端面** — 控制台"全脑"此前
  `mcp_manager=None`（外部 MCP 工具在 console 完全不可用）。现在
  `EmbeddedBrain` 按 `[mcp]` 配置连接外部 servers，用与网关相同的
  `register_mcp_tools` 通道完成 **广告**（`ChatStart.tools_json`）与
  **执行路由**（合成 `mcp`-kind registry 条目 → `McpToolBridge`）；
  `allowedMcpServers`/`deniedMcpServers` 策略同语义生效；
  `/mcp` 热插拔后 `refresh_mcp_tools()` 重跑广告 + 清理 stale 条目
  （`ChatService.with_advertised_tools` 新 setter），下一轮即生效；
  `aclose()` 一并回收连接。


### Dim 9 hooks residuals 1/2 — prompt evaluator prod wiring + 6 newly-live hook events (#117)

> claude-code parity Dim 9 残留：prompt-kind 钩子从"配置了但永远
> fail-open"变为真跑 LLM 裁决；pre_compact / session_start /
> session_reset / notification / file_changed / setup 六个事件从
> "accepted-but-dormant"点亮为有生产发射点。
> Dim 9 residuals part 1 of 2.

### Added
- **prompt-kind 钩子生产求值器**（`corlinman_server.hooks_evaluators`）——
  单次 LLM 裁决：钩子的 `prompt` 是裁决指令、事件载荷是证据，模型回
  `{"ok": bool, "reason": …}`（引擎已有的 verdict 协议）。两个
  HookRunner 构造点（standalone `main._build_hook_runner` + 网关
  `c2_wiring`）全部注入；provider **逐次惰性解析**（热重载即时生效、
  与 provider bootstrap 顺序无关）。裁决模型：`hooks.evaluator_model`
  > `CORLINMAN_HOOK_EVAL_MODEL` > `models.default`。reasoning 增量不
  计入裁决文本；解析失败/无模型 → 维持原 fail-open 行为。
- **agent-kind 晚绑定座**（`register_hook_agent_runner`）—— agent 钩子
  需要 out-of-turn subagent 入口；座已备好 + verdict 解析就绪，注册方
  落地前维持 fail-open（台账残留项）。
- **六个新 live 钩子事件**：
  - `pre_compact` —— reasoning loop 压缩临界时（同 elide 阈值门，静默
    轮次不扰）**blocking** 征询；deny = 本轮缓压缩（overflow 收缩路径
    无条件兜底，钩子只能延迟、不能弄死 turn）；
  - `session_start` —— servicer 聊天入口，每 session_key 每进程一次
    （有界 seen-set）；
  - `session_reset` —— console `/new` `/clear`；
  - `notification` —— `ask_user`（needs_input）+ 后台 subagent 终态
    （dispatcher 新 `hook_notifier` 注入座，网关接到
    `state.hook_runner` 惰性读取）；
  - `file_changed` —— `write_file`/`edit_file`/`notebook_edit` 的
    post_tool 之后，载荷带 `path`；
  - `setup` —— 每进程一次（网关 lifespan 启动完成 / embedded 大脑装配
    完成）。
  `hooks_live.LIVE_HOOK_EVENTS` 与 `/hooks`、`GET /admin/hooks` 的
  live/dormant 标注同步更新（仅剩 `session_end` 休眠）。

### W8: multi-tenant session isolation (security) (#114)

> 修复 chat-perfect 审计（hunt-51）遗留的 **critical** 多租户隔离绕过：
> journal 支撑的 `/admin/sessions*` 全部操作（list / delete / delete-all /
> patch / replay / cancel）此前不做租户过滤，任何管理端都能按 session_key
> 枚举、回放、改名、删除**所有**租户的会话。现在 journal turn 行带租户戳，
> 全部路由按解析出的租户收窄，跨租户访问一律表现为 404 / 空 —— 与不存在的
> key 不可区分。
>
> Fixes the **critical** multi-tenant isolation bypass from the
> chat-perfect audit (hunt-51): journal-backed `/admin/sessions*`
> operations were not tenant-scoped.

### Added
- **`turns.tenant_id` column** on both journal backends (SQLite gated
  `ALTER`, Postgres `ADD COLUMN IF NOT EXISTS`) + covering index. Legacy
  `''` rows are owned by the **default tenant** — never by other tenants.
- **Write-path attribution** — `ChatStart.tenant_id` (proto field 14):
  the OpenAI-compatible route stamps the authenticated tenant
  (`request.state.tenant`, API-key auth or the admin-session bridge);
  the servicer mirrors it into `extra["tenant_id"]` and `begin_turn`
  journals it. Channels / scheduler / console stay unattributed (=
  default tenant). Duck-typed channel contract untouched (tolerant
  `getattr`, regression-tested).
- **Tenant-scoped journal surface** — `list_session_summaries` /
  `delete_session` / `session_exists` / `update_session_meta` /
  `list_session_turns` accept an optional `tenant_id`; `None` keeps the
  single-tenant fast path byte-identical.
- **Principal capping** (`_resolve_request_tenant`) — a non-default
  principal tenant hard-caps the scope (mismatched `?tenant=` → 403);
  default-tenant operators keep the legacy any-tenant view selector.
- **Session cookie carries its tenant** — `AdminSessionStore.create`
  records the tenant the login verified against; the cookie auth path
  prefers it over the deployment default (fixes the
  `admin_auth.py` cookie-path tenant hardcode flagged in hunt-51).

### Fixed
- Cross-tenant `DELETE /admin/sessions/{key}` / `PATCH` / `replay` /
  `cancel` now 404; `DELETE /admin/sessions` (nuke) only wipes the
  resolved tenant's sessions; `GET /admin/sessions` no longer lists
  other tenants' sessions.


## [1.34.0] — 2026-07-19 — QZone folded into the QQ channel page + reference-image descriptions

### Added
- **reference-image descriptions, global ↔ task interop**: every
  reference asset now carries an operator-authored free-text
  `description` ("what this image shows / how to reference it", ≤500
  chars). New sqlite column with an explicit `ALTER TABLE` migration;
  upload form field + `PATCH assets/{aid}` accepts `label` and/or
  `description` (empty body 400s `empty_patch`). The persona studio
  grid gets a per-image description editor; the QZone job picker shows
  the same text (tooltip + snippet) because jobs reference assets by
  label — one source of truth, edits flow everywhere. Descriptions ride
  into generation at both injection points: the `image_with_refs`
  legend (`Reference image N = label (desc)`) and the qzone builtin's
  system-prompt block (`label（描述）`, best-effort, never fails the
  run). (#160)

### Changed
- **QZone publishing now lives inside the QQ channel page**: the whole
  `/scheduler/qzone` surface (daily-post upsert + jobs table + B6
  auto-reply sub-section) became `<QzonePanel>` and mounts on
  `/channels/qq` below the channel config editor — QZone borrows the
  running NapCat login state, so it belongs with the channel. The old
  route redirects (bookmarks keep working); the standalone sidebar
  entry is gone and its search keywords folded into the QQ item. (#160)
- **QQ channel config form de-noised**: `access_token` (OneBot WS
  auth) and `napcat_access_token` (NapCat WebUI auth) are both live
  fields but meaningless for the bundled NapCat — they folded behind
  the "advanced" disclosure next to the endpoint overrides they
  authenticate, with human labels ("OneBot WS token" / "NapCat WebUI
  token") and leave-blank-for-bundled hints. The default view is now
  just reply policy / IDs / whitelist / toggles. (#160)
- **eclipse-pearl mascot lost its mouth**: eyes-only face (blink
  animation kept), eyes re-centered. (#160)

### Fixed
- **QQ config round-trip**: `group_whitelist`, `proactive_groups`,
  both toggles and all six tuning numbers never echoed back through
  `config_keys`, so the editor always rendered blank/off over a
  configured value; they now pre-seed correctly. (#160)

## [1.33.1] — 2026-07-18 — opaque topbar

### Fixed
- **topbar was see-through**: `.c-appbar`'s post-utility
  `background: transparent` silently beat the topbar's `bg-sg-space-0`
  utility — invisible on the pure-black dark canvas, but on the Paper
  theme scrolled content bled straight through the sticky bar, and the
  floating bar's `top-2/4` gutter let content slide past its top edge.
  The bar is now opaque canvas color (`var(--sg-space-0)`, dark theme
  visually unchanged) with a canvas-colored strip blanking the sticky
  gutter, plus a guard test pinning `.c-appbar` opaque forever. (#159)

## [1.33.0] — 2026-07-18 — QZone daily-post diversity + scheduler UI rework + auto comment replies

### Added
- **QZone daily-post diversity engine**: every firing now composes its
  system prompt as persona body → life block (with rhythm signals) →
  "今日灵感种子" (one `persona_life_event_seed` freeform draw) →
  "最近已发过的说说" (anti-repeat excerpts from a new per-persona
  post-log sidecar, atomic-write, last 30) → an anti-formulaic tail
  that demands a fresh topic/scene/opening every day and tells the
  model to act on ⚠ life-rhythm nudges by advancing its persona state.
  Metadata knobs: `diversity` (default on), `recent_posts_n` (1-14),
  `jitter_minutes` (0-180 — runner-level random send-time delay so
  posts stop landing on the exact same second; manual trigger
  unaffected). (#155)
- **life-rhythm signals** (hermes port, persona-generic):
  `compute_life_signals` derives days-in-state / days-since-outing and
  a three-tier nudge (≥13d not out → HIGH go_out, ≥8d out →
  wrap_outing, ≥6d same state → change_scene); folded into
  `persona_life_get` and the daily-post prompt. (#152)
- **composition direction for reference-image art**: `image_with_refs`
  now wraps prompts with a candid slice-of-life intro (different
  actions/facings, off-axis framing, mid-action poses, lived-in
  clutter; persona-generic style). Opt out with
  `CORLINMAN_IMAGE_REFS_INTRO=off`; success envelope reports
  `composition_intro`. (#153)
- **task-level reference images + jitter on the scheduler API**:
  `image_ref_labels` (persona-asset labels, ≤8, validated) and
  `jitter_minutes` promoted to top-level `POST/PATCH/GET
  /admin/scheduler/jobs` fields (metadata remains the store of
  record); the daily builtin pins those labels in its prompt so
  generated art uses the uploaded references. (#157)
- **`qzone.reply_comments` scheduled builtin** (net-new vs hermes):
  scans the persona's own recent 说说 and replies to fresh comments
  in-character (`max_replies`, `lookback_posts`), with a per-persona
  seen-comments sidecar for dedup and an honest audit dict
  (`replies_posted` / `tids_scanned` / `skipped_seen`). Shared
  chat-drive skeleton extracted to `builtins/_qzone_chat.py`. Admin
  page gained an "自动回复评论" sub-section. (#158)
- **scheduler page rework**: jobs are edited in place (row → backfilled
  form → PATCH), cron is picked as 每天/每周/高级 via a schedule picker
  backed by a pure `cron-schedule` lib (Sunday emits `0`, never `7`),
  reference images are picked/uploaded/deleted in a thumbnail grid
  that shares the persona-studio asset cache, and delete/pause/resume
  ride per-row sprite actions. (#149, #151, #154, #156)

### Fixed
- **daily-post audit always lost `tid`/`qzone_url`**: `ToolResultEvent`
  had no payload slot, so every successful publish recorded
  `last_qzone_url=None` and history showed a bare "ran". The parsed
  tool envelope (≤8 KiB) now rides `payload_json` through the gateway
  and the builtin unions it over the input args; the audit also
  carries the published `text` (fuel for the anti-repeat log). (#150)
- **sidebar avatar initial overflowed the presence orb**: the orb's own
  `position:relative` overrode the un-prefixed `absolute`, pushing it
  back in-flow and shoving the initial out of the pearl; the initial
  is now the absolute centered overlay instead. (#148)

## [1.32.0] — 2026-07-18 — ask_user question cards + per-family reasoning tiers

### Added
- **web chat ask_user question cards**: the agent's `ask_user`
  clarification questions now render on the web — options appear as
  tappable pills under the assistant bubble (single-pick sends
  immediately; multi-pick submits joined labels; historical turns show
  inert options). Previously the options were silently lost on web
  (Telegram had buttons, QQ-family had lists, web had nothing). A
  still-streaming call stays visible in the tool trace until its args
  parse. New design-system card `components/question-card.html`.
- **per-model-family reasoning-effort tiers**: the composer's thinking
  control is no longer a hardcoded 低/中/高. A backend registry
  (`corlinman_providers.reasoning_tiers`) maps each *resolved* model id
  to its real ladder — gpt-5.6 six tiers (none…max), gpt-5.2/5.4/5.5
  five, gpt-5.1 four, o-series three, Claude 4.6+/Fable four/five
  (`output_config.effort` + adaptive thinking), Gemini 3.x
  `thinking_level` / 2.5 `thinking_budget`, grok-3-mini/4.5, DeepSeek
  V4 `thinking.type` + high/max, Qwen `enable_thinking` + budget,
  GLM-5 / GLM-4.x, Kimi k2.x toggles. `/admin/models` advertises
  `reasoning_tiers` per alias (relay aliases resolve to the upstream
  id); the UI renders exactly those options, hides the control for
  no-knob models, and requests are clamped to the nearest supported
  tier per family — never a 400 for an out-of-ladder pick. Anthropic
  and Google providers gained thinking-parameter support (previously
  absent entirely).

### Fixed
- **streaming thread line**: the luminous left-edge thread started
  inside the bubble's 20px corner arc and visibly floated outside the
  outline; it now spans only the straight left-edge segment. Same fix
  back-synced to all 15 design-system cards; the legacy Spatial Glass
  `styles.css` is gone from the design project.
- **CORS preflights on protected routes**: the API-key auth middleware
  401'd `OPTIONS` requests (preflights carry no credentials by spec),
  stranding every cross-origin browser client before the CORS
  middleware could answer.

## [1.31.1] — 2026-07-18 — reasoning summaries stay in the thinking block

### Fixed
- **OpenAI-compatible relays** (Responses→chat.completions shims, e.g.
  gpt-5.x): reasoning-summary bodies streamed as plain `content` while
  only the bold headline arrived on `reasoning_content` — the planning
  prose rendered as the assistant's reply, glued together across steps.
  The provider stream now buffers content after a headline-only
  reasoning chunk and routes it by what the step turns out to be: tool
  calls → reasoning block; plain stop / oversized buffer / dropped
  finish → the real answer, streamed as content. DeepSeek-R1-style
  reasoning is unaffected. Summary parts join with blank lines in the
  thinking block.

## [1.31.0] — 2026-07-18 — Eclipse Minimal v2 design language

> The whole admin UI switches from Spatial Glass (visionOS colored glass)
> to **Eclipse Minimal v2** — the corlinman design language from the
> claude.ai/design project: a pure-black canvas with a moonrise halo,
> matte charcoal surfaces, a five-step moon-white ink scale, and a tint
> pipeline that colors only the "light". The eclipse pearl becomes the
> product's signature — and the README's mascot.

### Added
- **Tint pipeline** (`lib/tint.ts` + `data-tint`) — five presets
  (dawn/ice/rose/moss/iris, L/C locked) plus a custom hue wheel; tint
  colors only the pearl, streaming thread, live dots, caret, solid
  primary buttons and selected states. Replaces Theme Studio; legacy
  theme-CSS localStorage keys are purged at boot.
- **Eclipse pearl** (`components/ui/presence-orb.tsx`) — the signature
  element: brand mark, chat identity, app-bar streaming indicator
  (spins while a turn streams), login/onboard/404 hero.
- **Self-drawn icon sprite** (`public/icons-sprite.svg`, 149 symbols,
  24 grid / 1.8 stroke / round caps) served once and cached; the
  `components/icons` barrel keeps lucide-compatible PascalCase exports.
- Self-hosted type stack: M PLUS 1 (display) + MiSans (body) +
  JetBrains Mono, latin subsets in-repo (Docker builds stay offline).
- Animated SVG mascot (`docs/assets/eclipse-pearl.svg`) with a tiny
  dot-eyed face; replaces the 9MB product-tour GIF in the README.

### Changed
- Every surface is matte opaque charcoal: shells, cards, overlays,
  chat bubbles (tail corners + streaming thread), composer well,
  approval sheet, session rows — per the moon-edge/well/elevation
  light grammar. Chat prose, code and JSON render monochrome.
- Chat assistant messages gain the agent-bubble chrome (600px cap,
  bottom-left tail); streaming shows the luminous thread + caret.
- `font-semibold`/`font-bold` resolve to 500 — hierarchy comes from
  the ink scale, per the weight discipline.

### Removed
- `backdrop-filter` everywhere (Tailwind core plugins disabled),
  Liquid Glass optics (lg-*), aurora/nebula/starfield backdrops,
  CursorLight, tilt-card, mascot sprite, Theme Studio, the `geist`
  and `lucide-react` dependencies.

### Enforced (vitest)
- Zero backdrop-filter repo-wide; zero lucide-react imports; bloom and
  grad-text whitelists; nebula tokens pinned transparent; tint preset
  pairs in both themes; font weights capped at 500.

## [1.30.0] — 2026-07-18 — human-paced QQ groups + truthful admin UI

> QQ group behaviour goes from "replies to everything, always" to
> whitelist-gated, human-paced speech with an opt-in proactive voice —
> and the admin UI stops lying: statuses now reflect reality (timezones,
> connection badges, dates in your language), the whole surface is
> properly bilingual, and forms shed derivable/dead fields.

### Added
- **QQ group whitelist** (`[channels.qq].group_whitelist`) — hard gate:
  only listed groups are ever answered; @mentions and slash commands do
  NOT bypass it. Empty list mutes every group; key absent = no whitelist.
- **QQ proactive speech** (default off) — `proactive_enabled` plus
  humanized pacing: random gaps (`proactive_min/max_gap_minutes`,
  45–180 default), active-hours window (9–23, overnight supported),
  per-group daily budget (`proactive_daily_max`, 4), persona voice via
  the normal chat pipeline in a dedicated per-group session; skips
  automatically while the QQ account is offline.
- **QQ emergency mute** — `group_replies_enabled = false` drops every
  group message before any gate; private chat unaffected.
- **Scheduler job timezones** — cron evaluation honours each job's IANA
  `timezone`; the QZone form sends the browser zone so the "next fire"
  preview matches the real firing instant (previously `0 9 * * *`
  fired 09:00 UTC = 17:00 Beijing).
- `ui/lib/format.ts` — all admin timestamps/numbers now follow the
  SELECTED UI language, not the OS locale (~25 call sites converted).
- `FieldHint` primitive — single-line field help with the long-form
  contract behind a hover tooltip; the copy-diet standard for forms.

### Changed
- **QQ group replies are quiet by default** — new
  `group_reply_policy = "mention_or_keyword"`: groups without an
  explicit keyword list answer only @mentions and slash commands
  (legacy reply-to-everything is `"all"`); non-mention replies respect
  a per-group cooldown (`group_reply_cooldown_secs`, 20s default).
- **Image generation**: default timeout 120s → 300s
  (`CORLINMAN_IMAGE_TIMEOUT_SECS`); auth/config rejections
  (401/403/404) and connection failures now fail fast as
  *unrecoverable* — the error tells the model to stop retrying and to
  tell the user the feature is unavailable, and a 10-minute
  per-endpoint breaker short-circuits follow-up calls. Timeouts and
  5xx/429 stay retryable.
- **Admin UI language**: zh-CN stays the default unless the operator
  explicitly toggles — `navigator.language` sniffing removed (a zh
  operator on an en-US browser was silently flipped to English).
- Admin copy diet (wave 3): page ledes cut to one plain sentence — no
  more raw `/admin/*` paths, regex patterns, config keys, or backend
  module names in user-facing help across the worst-offender pages.
- Forms shed derivable/expert fields (wave 4): QZone job name is
  auto-derived (`<persona>.daily_qzone`) and cron became presets +
  advanced; provider dialog shows base_url only for OpenAI-compatible
  kinds and prefills the key env-var; channel endpoint URLs, evolution
  tunables, and tenant display-name collapsed behind "advanced";
  persona id auto-slugs from the display name.

### Fixed
- **NapCat "QQ Is Logined" wedge** — after a session drop NapCat
  refused to mint a login QR while reporting offline; QR refresh now
  detects the stale state via CheckLoginStatus and drives the restart
  fallback (was a bare 502). Genuinely-logged-in returns a clear 409.
- QQ channel connection badge was hardcoded "unknown" — now derived
  from the live health watcher; removed the stats tile that fabricated
  a "throttled" count from the connection enum.
- QQ recent-message + log-stream timestamps rendered UTC wall-clock
  (hours off local); channel enable-switch invalidated dead query keys
  leaving status panels stale.
- 121 missing locale keys per language (evolution settings, skills
  drawer, agent picker rendered raw keys; identity page fell back to
  English); Chinese fragments purged from the English bundle and
  English leaks from the Chinese one; Telegram page's ~50 hardcoded
  English strings wired to existing translations.
- RAG tag filter removed — the UI collected tags but the backend never
  received them; persona editor's permanently-disabled test box
  removed.
- `deploy/install.sh` wrote colored log output into the NapCat systemd
  unit's `ExecStart` (unit failed to load as bad-setting) — logs now go
  to stderr.

## [1.29.0] — 2026-07-17 — unified memory kernel

> A nine-wave overhaul of the agent's memory. The old path dumped every
> raw turn into one FTS store, recalled it by keyword only, siloed
> memory per chat session, and shared one global notes namespace across
> all users (a cross-user leak). It is replaced by a unified, per-user,
> bi-temporal **memory kernel** — plus four capabilities no mainstream
> agent-memory system ships. Every new behaviour is config-gated and
> **off by default**; with the memory kernel left in its default
> `shadow`/off modes the chat path behaves exactly as 1.28.

### Added
- **corlinman-memory-kernel** — a new package: one `memory.sqlite`,
  additive `mk_*` tables, atomic bi-temporal facts (contradicted facts
  are *invalidated with a validity interval*, never deleted), an
  observation ingest queue, a recall ledger, and core-memory blocks.
  Rollout gate `CORLINMAN_MEMORY_KERNEL=off|shadow|on` (default
  `shadow`: observations accumulate and recall runs for telemetry only,
  nothing is injected).
- **Per-user, cross-channel scoping** (`[memory.scope]`, default on):
  durable memory is keyed by canonical identity, so the same person on
  QQ and Telegram (once linked) shares one memory and no user can read
  another's — closing the previous global-namespace leak. An operator
  identity merge re-homes the merged user's memory.
- **Ranked hybrid recall** (`[memory.recall]`): FTS5 + optional vector
  cosine fused with RRF, then ranked by relevance · recency · importance
  · trust, with a relevance floor and a per-injection char budget.
  Recalled memory is framed as untrusted data with per-item provenance
  (poisoning defence). The FTS index is CJK-capable (trigram), fixing
  Chinese recall that the previous tokenizer silently missed.
- **Sleep-time reconcile** (`memory.reconcile` scheduler builtin): turns
  the raw observation queue into curated facts off the hot path
  (LLM extraction → PII redaction → risk classification → mem0-style
  ADD/UPDATE/NOOP against existing memory), rebuilds core-memory blocks,
  and runs dry-run-first with an auditable per-run report.
- **EPA affect lens** (`[memory.affect]`): memories carry an
  emotion vector and the persona a live mood; recall becomes
  mood-congruent, with a mood-repair bias that prevents negative
  spirals. (Innovation.)
- **Implicit trust loop** (`[memory.trust]`): each reply is attributed
  against the memories it was shown — used / ignored / contradicted —
  and trust self-adjusts with no explicit feedback tool; repeatedly
  contradicted facts retire themselves. (Innovation.)
- **Dream cycle** (`memory.dream` scheduler builtin): a nightly
  affect-weighted replay that writes evidence-backed reflections into
  memory and a first-person entry into the persona's diary, with a
  small morning mood shift. (Innovation.)
- **Memory golden evals** — a YAML-scripted harness (`corlinman-memory-
  evals`) that gates recall regressions in CI the way code regressions
  are gated: scope-leak count must be zero, recall@k must hold.
  (Innovation.)

### Changed
- `LocalSqliteHost` opens with WAL + `busy_timeout` and reaps orphaned
  synthetic file rows; the agent servicer shares one memory-host handle
  instead of opening its own. No behaviour change for existing callers.

### Fixed
- FTS queries containing operator characters (`-`, `:`, quotes) no
  longer silently return empty — user text is escaped before `MATCH`.
- A leaked-connection class that could hang the Python test suite to the
  CI cap is closed at the source (test teardown + a repo-level backstop).

## [1.28.2] — 2026-07-15 — neutral User-Agent for OpenAI-compatible relays

> Patch: an OpenAI-compatible relay behind Cloudflare (Sub2API) blocked
> chat requests with "your request was blocked" (403) purely because of
> the OpenAI Python SDK's default `User-Agent` — a common WAF rule on
> such relays.

### Fixed
- The OpenAI-wire provider client now sends a neutral `corlinman-gateway`
  User-Agent instead of the SDK default `OpenAI/Python <ver>`, so relays
  that block the SDK's fingerprint accept the request. An operator custom
  `User-Agent` header still overrides it; harmless against real OpenAI.

## [1.28.1] — 2026-07-15 — provider key hygiene + de-duplicated Providers tab

> Patch for a prod report: a provider saved without a usable key looked
> configured (it showed key source "value"/字面量), so requests went out
> unauthenticated and the upstream rejected them ("blocked"); and the
> Providers & Keys tab showed two overlapping add-provider surfaces.

### Fixed
- An empty / valueless `api_key` (`{}`, `{value: ""}`, `{env: ""}`) now
  reports `api_key_source = "unset"` instead of "value", so the key field
  reads as empty and the operator is prompted to enter one. `POST
  /admin/providers` also refuses to persist a valueless key table, so an
  accidental `{}` can't recreate the misleading "looks configured, isn't"
  state.

### Changed
- Removed the redundant "Custom providers" section from the `/models`
  Providers & Keys tab. It was a parallel add-flow writing the same
  `[providers.*]` registry (only tagged `params.custom = true`), and every
  custom provider already appears in the main Providers table — so it
  duplicated the table. The main table + editor manage every provider;
  `/admin/providers/custom` stays for back-compat.

## [1.28.0] — 2026-07-15 — sub2api-style upgrade system + Models & Keys consolidation + nav registry

> Web UX + self-update overhaul. The one-click upgrade path is rebuilt on
> sub2api's design (supervisor-delegated restart, atomic swap with a kept
> rollback slot, health-poll-until-version reload); the sticky "update
> available" badge is fixed at the root; and the admin UI collapses four
> overlapping provider/key surfaces into one guided "Models & Keys" page,
> with a single navigation registry driving the sidebar, command palette,
> dev-settings grid and breadcrumbs. Merged as PRs #121–#128; verified
> live on prod.

### Added
- **sub2api-style one-click upgrade** (`system/upgrader/*`, `docker/upgrade_helper.py`).
  - Docker mode rebuilt around a **detached helper container** that performs
    the swap from outside the container being replaced (the old in-container
    recreate stopped its own orchestration mid-swap and had no rollback).
    It keeps the previous container as `corlinman-previous` — an instant
    rollback slot — and asserts the new container reports the target version
    on `/health` before declaring success; any failure restores the previous
    container. The helper negotiates the daemon's Engine API version (a
    pinned `/v1.41` 400s on Docker 25+).
  - A **boot finalizer** settles upgrade records the restart interrupts:
    version-match → succeeded, helper-succeeded-but-wrong-version →
    `version_assertion_failed`, helper failure mirrored (with `rolled_back`),
    else stalled. Live-helper records are parked and settled lazily.
  - New admin endpoints: `GET /admin/system/rollback-versions`,
    `POST /admin/system/rollback` (empty body = restore previous; docker
    does an instant container swap), `POST /admin/system/upgrade/{id}/cancel`.
  - Native helper (`deploy/corlinman-upgrader.sh`) gains a post-upgrade
    `/health` version assertion with git rollback on mismatch, request-level
    `allow_downgrade`, and an optional `UPGRADER_GH_PROXY`.
  - `[system.update_check] proxy_url` (fail-closed) for GitHub access behind
    restrictive networks; the checker now caches recent releases for the
    rollback picker.
- **Version badge** in the top bar (sub2api's `VersionBadge`): always-visible
  `v{current}` chip, amber pulse on an update, a one-click "Update now" confirm
  (no typed-tag friction; the audit log records the actor), a restart window
  that polls the unauthenticated `/health` and reloads only when the reported
  version equals the target, and a rollback panel on `/system`.
- **Guided provider setup flow** (`model-hub/provider-setup-flow.tsx`): a
  5-step preset → auth (API key / env / OAuth) → probe → pick models → set
  default flow, reused as the `/models` empty state + "Quick setup" dialog,
  the onboarding step-1 inline embed (replacing the new-tab hand-off), and a
  dashboard getting-started card.

### Changed
- **`/models` is the canonical "Models & Keys" page** — a three-tab
  (Providers & Keys / Model routing / Advanced credentials) consolidation of
  the old `/providers`, `/credentials` and `/models` surfaces, which now
  redirect to it. The `Credentials` sidebar row is removed.
- **Single navigation registry** (`ui/lib/nav-registry.ts`) is the one source
  for the sectioned sidebar (Chat / Operations / Configuration / System /
  Developer), the ⌘K command palette (developer pages now gated on dev mode),
  the dev-settings discovery grid and breadcrumbs — replacing four drifting
  hardcoded lists.
- `POST /admin/system/upgrade` `typed_confirmation` is now optional;
  `POST /admin/models/aliases` accepts a `{default}`-only body without wiping
  the alias table.

### Fixed
- **Sticky "update available" badge** — `resolve_app_version()` (new
  `system/app_version.py`) resolves one release-spaced version from the root
  `pyproject` for every reader (updater, `/health`, telemetry, MCP), so the
  updater no longer compares the never-bumped sub-package version against the
  release tag and report an update forever. Docker images now bake the version
  and expose it on `/health`.

## [1.27.0] — 2026-07-04 — Wave 4: compaction breaker (Dim 2) + background shell (Dim 4) + #108 backend

> Closes claude-code parity Wave 4: the summary-LLM compactor now degrades
> gracefully under a broken/low-value summarizer, `run_shell` gains a
> first-class background mode with a poll/kill control surface hardened
> across the full permission and subagent matrix, and the last three #108
> backend cleanups land.

### Added
- **Dim 2 — summary-LLM cooldown + failure breaker** (`reasoning_loop`). The
  optional summary-based compactor no longer thrashes: a failed summary trips
  a precise N-round cooldown (skips exactly N rounds, no off-by-one retry) and
  a run of low-savings summaries trips a breaker that disables it for the turn.
  Env knobs `CORLINMAN_COMPACT_SUMMARY_COOLDOWN_ROUNDS` (default 5) and
  `CORLINMAN_COMPACT_SUMMARY_BREAKER_LIMIT` (default 3).
- **Dim 4 — `run_shell(run_in_background=true)`** plus `shell_task_output`
  (paged poll) and `shell_task_kill` (terminate). Properties:
  - **Session-ownership isolation** — a task is keyed to the session that
    started it; only that session can poll or kill it (registry gate).
  - **64 KiB paged reads** with a `has_more` cursor and a monotonic offset;
    non-bool `run_in_background` and non-integer/overflowing `offset` are both
    hardened to never raise (validated at dispatch, clamped on read).
  - **Unified process-group reap** (`_reap`) routed through EVERY terminal
    path — natural exit, log-cap kill, lifetime watchdog, explicit kill,
    registry shutdown, and interpreter `atexit` — including a direct
    `killpg`-by-pid fallback that reaps daemonized grandchildren after the
    leader zombie is gone.
  - **Lifecycle watchdog** (max lifetime) + **log-cap** eviction that deletes
    the evicted task's spill file.
  - **Task-control permission model** — the poll/kill surface tracks the
    *grant to start* tasks, not `run_shell`'s per-command scoping: an
    `allow`/`log`/`ask` run_shell grant (scoped `run_shell(npm:*)`, unscoped,
    or rescued from a `*`-deny / `default=deny` catch-all, under strict/plan)
    carries through to the control tools, confined by the session-ownership
    gate. `shell_task_kill` aliases run_shell; read-only `shell_task_output`
    stays plan-allowed.
  - **Subagent safety** — a child is refused `run_in_background` (its bounded
    lifetime can't own a detached task) and the task-control tools are stripped
    from every child unless its card or per-spawn allowlist names them
    explicitly, so a child (which dispatches under the parent `session_key`)
    can't poll or kill the parent's jobs.

### Fixed / Performance
- **#108 item 3** — duplicate tool names that canonicalize to the same wire
  name now emit a single structured `warn_alias_collisions` per gate instead
  of silently dropping one.
- **#108 item 4** — the subagents overview SSE loop is now an adaptive
  three-clock poll with a pre-scan keepalive, cutting idle churn while keeping
  sub-heartbeat latency on real changes.
- **#108 item 5** — public-URL auto-detection caches on the persist file's
  mtime instead of re-reading every request.

## [1.26.0] — 2026-07-03 — MCP client: sampling + tools/list_changed + dynamic advertisement (Dim 5)

> Completes the MCP client dimension (claude-code parity Dim 5). The
> bespoke JSON-RPC client dropped every server-initiated frame; it now
> routes them, so corlinman can service a server's sampling requests and
> react to a server pushing `tools/list_changed`.

### Added
- **Server→client inbound frame router** in both transports (stdio + ws):
  `classify_inbound` splits request/notification/response; unhandled
  server requests reply `-32601` so a compliant server never hangs.
- **`sampling/createMessage` responder** (`[mcp.sampling]`): mode
  `off`/`auto`/`ask` (secure default `off`), per-server rate limit, model
  whitelist, `maxTokens` clamp, over an injected provider-agnostic
  completer. The capability is advertised in the handshake only when
  wired + enabled. *(The production completer that runs the LLM
  completion is a documented follow-up; until then sampling stays
  dormant/secure-off.)*
- **`tools/list_changed` client listener**: a server push re-lists that
  server's tools (debounced, `list_changed_debounce_ms`) and
  re-advertises the tool plane.
- **Dynamic re-advertisement** (`refresh_mcp_advertisement`): one
  entrypoint shared by the list_changed listener and admin hot-plug —
  recomputes `mcp_tools_json`, **prunes** synthesized entries for
  vanished servers (previously left dead tools advertised until
  restart), and refreshes the live ChatService.

### Fixed
- **Issue #108 (MCP hot-plug schema refresh)**: admin
  enable/disable/restart/remove/reconfigure now re-advertise the tool
  plane without a restart (the `McpAdapter` fires the same refresh hook).

## [1.25.0] — 2026-07-03 — Declarative hooks + /hooks (claude-code parity Dim 9)

> Operators can now define lifecycle hooks in config — no code. A
> `[hooks.declarative]` sub-table maps events (claude-code names like
> `PreToolUse` or snake_case) to matcher groups of hook definitions,
> layered over the existing `HookRunner` (legacy flat `[hooks]` keys are
> untouched and keep their historical exit-code contract).

### Added
- **Declarative hook settings**: per-event matcher groups (`matcher` =
  tool-name pattern `exact | A|B | prefix*`; optional `if` = the shared
  permission-rule grammar, e.g. `run_shell(git:*)`) with four hook kinds:
  `command` (stdin JSON; exit 0 = allow w/ optional JSON verdict on
  stdout, exit 2 = block w/ stderr reason, other = fail-open), `http`
  (POST payload, 2xx JSON verdict), `prompt` / `agent` (injected
  evaluators; fail open until wired). Config mistakes become warnings —
  never a boot failure; everything fails open except an explicit block.
- **`/hooks` console command**: view all three layers (shell / discovered
  / declarative) with per-event live-emitter status and config warnings;
  `test <event> [tool] [json]` dry-runs the real fold; `reload` rebuilds
  the runner from the current config without a restart.
- **New live hook sites**: post-tool hooks now fire with the actual
  result (previously zero callers), the loop's Stop veto/inject path is
  active in the servicer-driven flow (previously inert), declarative
  `user_prompt_submit` verdicts land as system notes, and `post_compact`
  fires after a real compaction.
- **Hooks hot-reload**: a `[hooks]` config change now rebuilds the
  runner via the ConfigWatcher (was boot-time-only).
- **`GET /admin/hooks`**: `discovered` / `declarative` / `warnings` /
  `live_events` fields (backwards compatible).

## [1.24.3] — 2026-07-03 — Pre-merge audit fixes (stack #102–#107)

> Patch release. 22 confirmed findings fixed from the pre-merge audit of the
> stacked PRs (28/33 Codex inline comments verified live at tip + workflow
> review); 6 architectural findings deferred to #108.

### Fixed
- **MCP (P1)**: synthesized `mcp` registry entries are now created AFTER the
  plugin registry exists — advertised MCP tools were unroutable
  (`plugin_not_found`) on every boot. Advertisement guards added: invalid
  OpenAI-charset names, manifest-collision servers, and literal-shadowed
  namespaced names are dropped consistently from both `tools_json` and entries.
- **Console sessions**: `--continue` resumes true recency (pinned sessions no
  longer hijack it); fuzzy `/resume` proves uniqueness beyond one 50-row page.
- **`/rewind` (turn-keyed)**: ownership probe rejects foreign-session turn ids;
  Postgres stub backend degrades instead of wiping the window and reporting
  success; prior turns page to exhaustion (was: newest 50); numeric turn-id
  tie-break; window swap is all-or-nothing on journal failure; degraded
  rebuilds fall back to the label match; user text starting with `[turn:` can
  no longer masquerade as a journal tag.
- **Approval resolver**: "always" grants cleared on `/new`, `/clear`, and
  permission-mode switches (a cached `run_shell` grant no longer bypasses
  `/plan`); concurrent approval prompts serialized.
- **Live subagents panel**: `rejected`/`depth_capped` children show as failed;
  respawns after agent restart replace stale terminal rows; `/status` falls
  back to the live registry for inline rows.
- **Provider editor**: dirty drafts re-persist before alias binding; Add/Add-all
  skips aliases already routed to another provider (with a warning) and is
  gated on the enabled switch; the models probe trims pasted full-endpoint URLs.
- **Compaction**: elision sentinel check requires the full generated shape
  (adversarial tool output starting with the prefix can't bypass compaction);
  duplicate synthesized tool-call ids resolve to their own round's shell.
- **File tools**: new-file atomic writes respect the process umask again.

## [1.24.2] — 2026-07-03 — System-prompt flags + informative elision

> Patch release. Config-compatible. ABSORB_MATRIX Dim 10 residual +
> Dim 2 (b)/(c) — the last two small-slice items on the landing list.

### Added
- **`corlinman console --system-prompt TEXT`** — replace the default coding
  prompt + project memory wholesale for the run; **`--append-system-prompt
  TEXT`** — append after whatever prompt is in effect (default composition or
  an override). Append alone keeps the default coding prompt intact.

### Changed
- **Elided tool payloads are now informative one-liners** — compaction writes
  `[older tool output elided — tool(args…) · N chars]` (stable prefix, fully
  deterministic → prompt-cache safe) instead of the flat generic sentinel, so
  the model knows what was dropped and can re-fetch it.
- **Saved-token feedback in compaction** (claude-code microcompact semantics):
  at ≥ summary-threshold pressure the cheap elide pass is measured first and
  the LLM summarize sub-call is skipped when elision alone pulls the estimate
  back under threshold; no-op elide passes preserve list identity so a
  saturated history no longer invalidates the incremental token cache (and
  re-walks the full message list) every round.

## [1.24.1] — 2026-07-02 — Background memory-recall prefetch

> Patch release. Config-compatible. ABSORB_MATRIX Dim 6 (mechanism absorbed
> from hermes' background next-turn prefetch).

### Changed
- **Recency memory recall is prefetched off the hot path** — the start-of-turn
  `host.recent(...)` await is now precomputed in the background right after the
  previous turn's memory store (the moment its result changes), and consumed
  one-shot at the next turn; a cache miss falls back to the inline recall.
  Cuts start-of-turn latency, most visibly on remote memory-host backends. The
  relevance (BM25) recall depends on the incoming user text and deliberately
  stays inline.

## [1.24.0] — 2026-07-02 — Session management: --continue, fuzzy /resume, turn-keyed /rewind

> Minor release. Config-compatible. ABSORB_MATRIX Dim 11 (会话管理:一键续聊、
> 模糊恢复、精确回退).

### Added
- **`corlinman console -c/--continue`** — resume the most recent journal
  session (the summaries are newest-first, so this is a zero-cost lookup); an
  explicit `--session` wins; attach mode / empty journal degrade with a note.
- **Fuzzy `/resume <fragment>`** — an exact key wins alone; a unique substring
  match resumes; multiple matches print a disambiguation list instead of
  guessing; zero matches keep today's semantics (start a fresh named session).

### Changed
- **`/rewind` window truncation is now turn-keyed** — the workspace snapshot
  taken at each turn's start now embeds the journal turn id in its commit
  subject (`snapshot: [turn:<id>] <label>`), and rewinding to a tagged
  checkpoint rebuilds the conversation window **exactly** from journal turns
  strictly before that id, instead of matching the sanitized user-text label
  (which degraded to "window unchanged" on duplicate text or cross-surface
  interleave). Legacy untagged checkpoints keep the label-match fallback;
  a missing journal degrades honestly.

## [1.23.0] — 2026-07-02 — Console permission surface (mode control + interactive approval)

> Minor release. Config-compatible. ABSORB_MATRIX Dim 3 — the permission engine
> (modes + `Bash(cmd:*)`-style rules) existed but had no console surface: the
> mode was a boot-time env default and every `ask` verdict fail-closed to deny
> because nothing ever wired an approval resolver (控制台权限面板 + 交互式工具审批).

### Added
- **`/permissions [mode]` + `/plan [off]`** — show or switch the runtime
  permission mode (`default` / `acceptEdits` / `plan` / `bypass`); the gate
  re-reads its mode on every tool call, so the switch applies immediately. A
  typo **never** changes the mode (silently coercing `plan`→`default` would
  re-enable mutations); `bypass` prints a warning. `/plan` is the plan-mode
  toggle. `/permissions` also lists the session's always-allowed tools.
- **`corlinman console --permission-mode <mode>`** — seeds the embedded agent's
  gate at boot (via `CORLINMAN_AGENT_PERMISSION_MODE`).
- **Interactive tool approval** — an `ask` permission verdict now pauses the
  live spinner and prompts **y**es / **a**lways-this-session / **N**o instead of
  fail-closing to deny. "Always" caches the tool for the session; anything
  unexpected (empty input, EOF, prompt failure) denies — fail-closed. Wired for
  the embedded interactive REPL only: `--print` has no user to ask and attach
  mode has no in-process servicer, so both keep the fail-closed posture.

### Fixed
- **`notebook_edit` classified as an edit + mutating tool** — it was absent
  from both permission sets, so plan mode did not deny it (a mutating tool
  escaping the no-side-effects guard) and `acceptEdits` did not auto-allow it.

## [1.22.9] — 2026-07-02 — Live token + cost in the console status bar

> Patch release. Config-compatible. ABSORB_MATRIX Dim 12 — the console bottom
> bar now surfaces session spend.

### Added
- **Live token + cost in the bottom status bar** — the console's prompt bar
  showed only `model · session`; it now appends the running session token count
  and estimated USD cost once a turn produces usage, a glanceable session-spend
  readout (hidden while idle).

## [1.22.8] — 2026-07-02 — `notebook_edit` tool (.ipynb cells)

> Patch release. Config-compatible. ABSORB_MATRIX Dim 4 — the claude-code
> NotebookEdit analog (Jupyter notebooks were previously read-only).

### Added
- **`notebook_edit` builtin tool** — edit a Jupyter notebook by 0-based cell
  index: **replace** a cell's source (clearing a code cell's stale
  outputs/execution_count), **insert** a new code/markdown cell, or **delete** a
  cell. Workspace-confined and rewritten atomically. Advertised alongside the
  other coding tools.

## [1.22.7] — 2026-07-02 — Atomic file writes + per-tool tracing

> Patch release. Config-compatible. ABSORB_MATRIX Dim 4 (atomic Write/Edit) +
> Dim 12 (per-tool OTel span).

### Fixed
- **Atomic `Write` / `Edit`** — both the write and edit coding tools opened the
  target with `O_TRUNC` and wrote in place, so a crash or partial write could
  leave a **truncated/corrupt** file. Writes now stage into a unique sibling
  temp file (`tempfile.mkstemp`), `fsync`, then `os.replace` onto the target
  (atomic rename). The existing file's mode (e.g. an executable bit) is
  preserved; a symlinked target is refused and `os.replace` never follows a
  link, preserving the prior `O_NOFOLLOW` workspace-escape posture.

### Added
- **Per-tool OTel span** — each tool execution in the chat loop is now wrapped
  in a `tool.execute` span (`tool.name` / `tool.plugin` / `tool.is_error`,
  exceptions recorded), complementing the existing request-level spans. No-op
  when no tracer is installed.

## [1.22.6] — 2026-07-02 — Console `/cost` (estimated session spend)

> Patch release. Config-compatible. ABSORB_MATRIX Dim 12 — surfaces the
> per-model USD cost the agent loop already computes but the console never
> showed (`/usage` was tokens-only).

### Added
- **`/cost` console command** — shows the estimated USD spend for the current
  session (model, turns, in/out tokens, cost), reusing the reasoning loop's
  per-model pricing coefficients. An unknown/unpriced model reports
  "unavailable" rather than a misleading $0. (A live cost/token status bar
  remains a follow-up.)

## [1.22.5] — 2026-07-02 — Console `/init` bootstraps CORLINMAN.md

> Patch release. Config-compatible. ABSORB_MATRIX Dim 8 — the claude-code
> `/init` analog (project-memory discovery/@include was already shipped; this
> was the one missing piece).

### Added
- **`/init` console command** — analyzes the codebase and writes a concise
  `CORLINMAN.md` project-memory file at the repo root. Resolves to a one-shot
  brain turn (via `TurnRequest`) that inspects the project with the agent's file
  tools (build/lint/test commands, architecture, conventions) and writes the
  file, improving an existing `CORLINMAN.md` rather than discarding it. The
  existing discovery/@include pipeline then folds it into every subsequent
  session's system prompt.

## [1.22.4] — 2026-07-02 — Tunable context-compaction reserve

> Patch release. Config-compatible (defaults unchanged). ABSORB_MATRIX Dim 2 —
> makes the model-aware compaction budget's output reserve operator-tunable,
> including claude-code's fixed-buffer (`window − buffer`) semantics.

### Added
- **Operator-tunable compaction reserve** — when the compaction budget is
  derived from a model's declared context window, the reserved output margin is
  now overridable: `CORLINMAN_CONTEXT_RESERVE_TOKENS` pins a **fixed** buffer
  (`window − buffer`, matching claude-code's `AUTOCOMPACT_BUFFER`), else the
  proportional reserve's fraction and cap are tunable via
  `CORLINMAN_CONTEXT_RESERVE_FRACTION` / `CORLINMAN_CONTEXT_RESERVE_CAP`. All
  three default to the previous behaviour (0.15 fraction, 48k cap), so existing
  deployments are unchanged. The reserve is clamped to never exceed the window.

## [1.22.3] — 2026-07-02 — MCP tool namespacing + server allow/deny policy

> Patch release. Config-compatible. Hardens the v1.22.0 MCP tool-face
> (ABSORB_MATRIX Dim 5) — closes a bare-name collision gap and adds a server
> policy absorbed from claude-code's `allowedMcpServers`/`deniedMcpServers`.

### Fixed
- **Cross-server MCP tools no longer silently drop, and can't shadow builtins**
  — discovered MCP tools were advertised by their bare name with first-wins
  dedup, so a tool of the same name on two servers dropped the second, and an
  MCP tool named like a builtin (`calculator`, `web_search`) shadowed it. Tools
  are now advertised **namespaced as `{server}_{tool}`** (unique per server,
  distinct from bare builtins); the `McpToolBridge` strips the `{server}_`
  prefix back to the bare tool the server knows, guarded by `has_tool` so a real
  on-disk `mcp` manifest advertising a bare name is untouched.

### Added
- **MCP server allow/deny policy** — `[mcp].deniedMcpServers` /
  `allowedMcpServers` (deny wins; a non-empty allow-list is exclusive) filter
  which connected servers' tools are advertised + routable, applied at boot in
  `register_mcp_tools`.

## [1.22.2] — 2026-07-02 — Jittered retry backoff (thundering-herd defence)

> Patch release. Config-compatible. First Phase-2 absorb from
> `audit/ABSORB_MATRIX_2026-07-02.md` (Dim 1, mechanism absorbed from
> hermes-agent's jittered backoff — re-implemented, no code copied).

### Changed
- **Transient-retry backoff is now jittered** — when a provider 429/5xx has no
  `retry-after` hint, the reasoning loop's exponential backoff
  (`0.5·2^(n-1)` capped 16s) previously used a fixed value, so a fleet of
  workers retrying the same overload resynchronised into a thundering herd.
  Backoff now applies **equal jitter** (half fixed + a random half), spreading
  retries across `[base/2, base]`. Provider `retry-after`/reset hints are still
  honoured verbatim. Extracted as the testable `_retry_backoff_seconds` helper
  (injectable RNG) in `reasoning_loop.py`.

## [1.22.1] — 2026-07-02 — Live multi-agent panel: accurate tool-call count

> Patch release. Config-compatible. Fixes an inflated tool-call number on the
> live multi-agent panel (实时多智能体面板的工具调用计数虚高).

### Fixed
- **Live subagent tool-call count no longer inflates** — the shared
  `LiveSubagentRegistry` is fed the same `ToolStateRunning` frame once per open
  SSE client (the session poll) and again via the emitter observer, and each
  delivery did `tool_calls_made += 1`, so the panel showed 2×–N× the real count
  during a run. Counting is now idempotent by the frame's `tool_call_id` (a
  per-child seen-set, pruned with the terminal row), so a re-delivered tool
  start is counted exactly once regardless of how many clients are watching or
  which feed path (emitter vs cross-process journal poll) delivers it.

## [1.22.0] — 2026-07-02 — External MCP tools reach the model (advertise + route)

> Minor release. Config-compatible. Connected external MCP servers' tools are
> now advertised to the model and executable end-to-end — closing the gap where
> `McpClientManager.discovered_tools()` had no consumer, so the agent could
> never see or call an external MCP tool (让 agent 真正看得见并调用外部 MCP 工具).
> See `audit/BUG_LEDGER_2026-07-02.md` §3 (L-003).

### Added
- **MCP tools in the agent tool plane** — at gateway boot, after the MCP client
  manager connects its servers, the gateway now (1) synthesizes one `mcp`-kind
  plugin-registry entry per ready server so the existing tool executor routes a
  bare tool call through the `mcp` branch → `McpToolBridge` → `call_tool` with
  no new dispatch code, and (2) injects the discovered tools' OpenAI function
  schemas into every `ChatStart.tools_json`, so the agent servicer advertises
  them to the model. Both halves run gateway-side (the only process where the
  live manager and plugin registry exist) from a single pass over
  `discovered_tools()`; the schemas are threaded from gateway state, not the
  chat request, so the channel request contract is untouched. Tool names are
  advertised bare (server resolved at execution via `find_tool`), de-duplicated
  across servers, and a synthesized entry never clobbers a real on-disk
  manifest. Boot-time snapshot; hot-plug refresh is a follow-up.

## [1.21.9] — 2026-07-02 — openai_compatible `/openai` mounts serve chat again + green gate

> Patch release. Config-compatible. Fixes a silent chat-404 regression for
> openai_compatible providers whose base URL is a bare `/openai` API root
> (裸 `/openai` 根地址的中转/网关), and restores the green local CI gate. Part of
> the zero-bug sweep — see `audit/BUG_LEDGER_2026-07-02.md`.

### Fixed
- **`/openai`-mounted base URLs no longer 404 every chat message** — the
  adaptive base-url normalizer only recognised a path ending in `/v<digits>`
  as an already-complete API root, so a base URL ending in a bare `/openai`
  mount (Google Gemini's documented OpenAI-compat endpoint
  `…/v1beta/openai`, or a relay served at `…/openai`) got `/v1` appended and
  the OpenAI SDK hit `…/openai/v1/chat/completions` → 401/404 on every turn
  (裸 `/openai` 根地址每条消息都 404). Both mirror normalizers —
  `complete_openai_base_url` (chat client) and `_provider_models_url` (admin
  model probe) — now treat a `/openai`-ending path as an API root, so chat and
  the "fetch models" probe resolve to the same root. Regression tests added on
  both sides.
- **Local CI gate green again** — fixed the two latent gate failures on the
  branch: a ruff `I001` import-order error in `test_tool_aliases`, and a mypy
  `arg-type` where the console spinner frame (`Spinner.render()` typed
  `RenderableType`) was passed into `Text.append_text` (needs `Text`); the
  frame is now `isinstance`-narrowed, with a render-path regression test.

## [1.21.8] — 2026-06-16 — Codex provider test no longer false-fails after OAuth login

> Patch release. Config-compatible. Fixes the admin provider "Test" button for
> Codex OAuth accounts when the ChatGPT Codex model catalog endpoint returns a
> transient HTTP 400 even though the stored OAuth credential is usable.

### Fixed
- **Codex provider test follows OAuth readiness instead of the live catalog** —
  `/admin/providers/codex/test` now validates that the gateway can read the
  stored Codex OAuth credential (and refreshes it when expired) without using
  the ChatGPT Codex model catalog as a hard liveness check. This removes the
  false `codex: HTTP 400` toast seen immediately after a successful Codex
  login, while leaving `/admin/providers/codex/models` live discovery intact
  for the model dropdown.

## [1.21.7] — 2026-06-15 — Codex login, flagship defaults, and reasoning effort

> Patch release. Config-compatible. Fixes Codex OAuth on native VPS deployments,
> makes newly configured providers usable immediately with current flagship
> defaults, and exposes per-request reasoning effort in the web chat UI.

### Added
- **Chat reasoning effort control** — `/chat` now persists a composer-level
  low / medium / high / xhigh setting and forwards it through the gateway to
  provider runtime params. The Codex adapter maps it to the Responses API
  `reasoning.effort` field.
- **Future flagship selection** — provider autobind and OAuth provisioning now
  score the live model catalog by provider family, generation, and tier. When a
  future flagship appears in discovery (for example a newer GPT, Claude Opus,
  Gemini Pro, Qwen Max, or Groq GPT-OSS size), it becomes the default without a
  code change; curated static defaults remain as the safe fallback.

### Changed
- **Anthropic and Google model discovery can use live APIs** — API-key-backed
  Anthropic and Gemini providers query their native model-list endpoints when a
  key is configured, while still falling back to the built-in catalog if the key
  is unavailable or the upstream list fails.
- **Current provider defaults are flagship-oriented** — OpenAI/Codex,
  Anthropic, Google, Mistral, Cohere, DeepSeek, Qwen, GLM, Together, Groq, and
  Replicate defaults were refreshed to the current flagship choices used by the
  admin autobind flow.

### Fixed
- **Codex OAuth works after login on split native deployments** — the agent
  process now resolves live provider aliases from the persisted Python config,
  and the Codex provider reads/refreshed credentials from the configured
  `data_dir` instead of assuming the service user's home directory. This fixes
  the post-login 403/ unusable-chat path seen on the VPS.
- **Config changes refresh provider state immediately** — provider registry and
  model-source state are refreshed after admin/OAuth config mutations, so login
  and provider edits take effect without stale in-process routing.
- **Codex model discovery uses the ChatGPT Codex backend** — admin probing and
  OAuth provisioning query the Codex models endpoint with the same Cloudflare
  headers used by the runtime adapter, including token refresh handling.

## [1.21.6] — 2026-06-14 — OAuth login makes the new account the active model

> Patch release. After `codex login` (and the other OAuth flows) the freshly
> provisioned account now actually becomes the active default and chat works
> immediately, instead of staying pinned to a prior — possibly stale —
> provider. Config-compatible. (PR #99)

### Fixed
- **OAuth login takes over `models.default`** — an explicit login now repoints
  the default to the just-provisioned account's best model even when a different
  provider was already the default, so `codex login` is immediately usable
  instead of leaving chat on the previous (often stale) provider and 401-ing.
  The takeover is non-destructive (other providers' aliases are left intact —
  only the `default` pointer moves) and is gated on successful model discovery,
  so a transient upstream model-list outage during login never moves a working
  default onto a guessed fallback id.
- **Saving the alias table no longer drops provider bindings** — the Models page
  "Save all" posts a flat `{name: target}` map; the bulk endpoint now MERGES,
  preserving each existing alias's `provider` + `params` instead of replacing the
  table wholesale. Previously this stripped the provider off every alias (e.g.
  the ones OAuth login provisioned); the resolver then dropped the provider-less
  aliases and chat fell through to the wrong upstream (the `401` + "—" provider
  column).
- **Chat model picker no longer lists `0`, `1`, `2`…** — it read the
  `/admin/models` alias *array* with `Object.entries` (which yields numeric
  indices as names). It now consumes the v0.2 array shape (and tolerates the
  legacy record shape), and **groups models per provider** so you can pick a
  provider then one of its available models.
- **Latest-model detection prefers the newest version** — discovered model ids
  not in the curated preference list are now ordered newest-version-first
  (tolerating suffixes like `gpt-5.5-codex`), so a fresh release wins over an
  older sibling the upstream happened to list first.

## [1.21.5] — 2026-06-14 — OAuth model provisioning + env-backed autobind

> Patch release. Config-compatible — existing provider/model config is
> preserved; OAuth login now provisions usable model config, and autobind
> respects manually configured and env-var-backed providers. (PR #97, #98)

### Added
- **OAuth login provisions a usable model list** — after Anthropic / Claude Code
  / Codex OAuth completion, the gateway discovers the account's upstream models
  and writes a provider slot plus `models.aliases` (and a `models.default` when
  none exists), so chat works immediately without a manual trip through
  Providers/Models.

### Changed
- **Dashboard greeting uses the signed-in admin** instead of hard-coded copy,
  and the hero/status wording was tightened in English and 简体中文.

### Fixed
- **Disconnect only cleans up what OAuth provisioned** — a marker distinguishes
  flow-provisioned slots from operator config, so disconnecting an OAuth account
  disables/clears only its own slot and dangling default while leaving manually
  configured (including env-var-backed) providers and user-created aliases
  untouched; provisioning likewise never repurposes a manual slot, shadows a raw
  default, or resurrects an explicitly disabled manual provider.
- **`/v1/models` no longer advertises dead models** — aliases pointing at a
  disabled/unbuilt provider are hidden until the provider is re-enabled, so model
  pickers don't offer ids every chat would fail to resolve.
- **Autobind honors env-var-backed built-in providers** — enabling a built-in
  slot (e.g. `[providers.openai]`) with only its documented vendor env key
  (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, …) set now binds a
  `models.default`, consistently across `/admin/providers` and
  `/admin/credentials`; custom-named slots still require an explicit key.

## [1.21.4] — 2026-06-12 — credentials model autobind hardening

> Patch release. Config-compatible — existing provider/model config is
> preserved, with safer defaults when credentials or custom providers change.
> (PR #96)

### Fixed
- **Saving a usable provider now fills missing chat model config** — writing or
  enabling a provider with its primary credential auto-binds a sensible
  `models.default` alias when no default exists, so chat can work immediately
  after setup.
- **Operator-authored aliases are preserved** — existing full aliases keep
  their model/params, shorthand self-aliases are upgraded to the full
  runtime-resolvable `{provider, model, params}` shape, and disabling a provider
  only clears the active default that actually targets it.
- **Unusable custom providers no longer become chat defaults** — Fish Audio TTS
  providers and keyless credentialed cloud custom providers are skipped, while
  keyless `openai_compatible` local relays remain supported.
- **Removing custom credentials clears stale chat routing** — deleting a custom
  provider slug's `api_key` now disables that provider and removes its active
  default model reference.

## [1.21.3] — 2026-06-12 — channel persona persistence

> Patch release. Config-compatible; existing binding preference DBs are
> migrated in place to add nullable per-binding persona selection. (PR #95)

### Fixed
- **Telegram reasoning previews no longer stutter token-by-token** — reasoning
  chunks are buffered until sentence punctuation or newline, then flushed
  before answer text, tool activity, completion, or errors. The final reply
  still excludes reasoning text.
- **Persona Studio selections now stick per channel conversation** — personas
  created from channel chats persist to that binding, survive `/new`, and can
  be cleared with `/use-default-persona`.
- **Existing binding preference databases migrate safely** — the new
  `persona_id` column is added idempotently while preserving model overrides
  and session epochs.

## [1.21.2] — 2026-06-12 — login hydration fix

> Patch release. Config-compatible — no migration required. (PR #94)

### Fixed
- **Login page no longer trips React hydration on English clients** —
  exported static HTML still hydrates in the default `zh-CN` locale, then the
  provider applies the persisted/browser language after mount. This preserves
  the operator's language preference while avoiding the pre-hydration
  Chinese/English text mismatch seen in production smoke testing.

## [1.21.1] — 2026-06-12 — attachment size + empty upload fixes

> Patch release. Config-compatible — no migration required. (PR #93)

### Fixed
- **Attachment cards no longer show `0B` after replay or live delivery** —
  transcript replay, live `AttachmentAdded` events, streamed
  `corlinman.attachment` chunks, and assistant/user journal metadata now carry
  positive file sizes through to the web UI.
- **Empty uploads fail locally with a clear error** — the composer rejects
  zero-byte files before calling `/v1/files`, showing `文件为空` / `File is
  empty` instead of a generic upload-failed card.

## [1.21.0] — 2026-06-12 — live agent attachments + chat stream reattach

> Minor release. Config-compatible — no migration required. (PR #92)

### Added
- **Agent attachments render live in web `/chat`** — `send_attachment` used
  to show only its tool-call trace; the file appeared only after reopening
  the conversation from history. The file now registers into the gateway
  store the moment the tool runs and streams to the client through BOTH
  surfaces: a new `AttachmentAdded` journal event and a
  `{"corlinman":{"attachment":{kind,url,name,mime}}}` extension chunk on
  `/v1/chat/completions`. Any file type is supported (the media-suffix
  allowlist is bypassed for explicit sends — PDFs, archives, anything);
  workspace-relative paths resolve correctly; the same file sent twice in
  one turn (e.g. `image_generate` + `send_attachment`) renders once.
- **Reattach to an in-flight turn** — navigating away mid-generation and
  back used to freeze the conversation at the committed history; new chunks
  never streamed. Generation always continued server-side; the client just
  had no way back in. Reopening a conversation now detects an
  `in_progress` latest turn, rebuilds the pending bubble from the journal
  event replay, tails the live SSE from the exact backlog cursor, and
  finalizes on the journal terminal event (with a status-poll safety net).
- **Cross-process observability bridge** — the standalone agent server
  (`corlinman-agent.service` in the two-process native deploy) never wired
  an event emitter, so production journaled **no** `turn_events` and every
  live-events surface (`/admin/sessions/{key}/events/live`, session detail
  timelines) sat silent for chat turns. The agent process now journals
  envelopes into the shared sqlite, and the gateway's live SSE gains a 1 s
  journal-polling fallback so subscribers see them without in-process
  fan-out. One turn identity everywhere: the reasoning loop's envelope ids
  are pinned to the journal turn id (`turns` and `turn_events` used to
  carry two unrelated id namespaces, so session→events joins matched
  nothing).

### Fixed
- **Thinking leaked into the visible reply** — the gateway ignored
  `is_reasoning` and emitted chain-of-thought as `delta.content`. Reasoning
  now rides the `delta.reasoning_content` extension (and
  `message.reasoning_content` non-streaming); the web chat folds it into
  the collapsible thinking block.
- **History replay sprawled tool calls** — each tool call rendered as its
  own assistant bubble. Consecutive assistant journal rows now merge into
  one bubble per turn (matching live), and settled tool traces default to
  the collapsed "N tool calls" summary chip.
- **Double-delivery hardening for the new event bridge** — journal
  text/reasoning/tool-input deltas are dropped client-side while the fetch
  stream owns the turn (both carry the same tokens; applying both doubled
  the reply and corrupted tool args), gateway tool chunks reuse the agent's
  real `call_id` (synthesised ids would render duplicate tool cards), and
  the chat event-merger's journal payload mappings were corrected to the
  real wire field names (`tool_call_id`/`elapsed_ms`/`partial_json`).
- **Fresh live streams no longer replay the previous turn** — a subscriber
  without a `Last-Event-ID` now tails strictly forward (the poll cursor
  seeds at the latest sequence); explicit resume keeps full delivery by
  naming its turn with the composite `<turn>:<seq>` cursor.

## [1.20.1] — 2026-06-11 — one-click updater fixes

> Patch release. Config-compatible — no migration required.

### Fixed
- **One-click upgrade flashed to 404 and never ran** (PR #91, three stacked
  bugs found on a live box):
  - The post-confirm redirect targeted `/admin/system?upgrade=<id>` — the
    backend API namespace, not a page route; the updates page lives at
    `/system` (the `(admin)` route group adds no URL segment). The operator
    saw an instant 404 and lost the progress view. Same-class dead links
    fixed in the TopNav update bubble, sessions back-link/breadcrumb/turn
    pills, and onboarding finish + handoff cards.
  - Native mode wrote the update checker's *stripped* display tag
    (`1.20.0`) into `.upgrade-request`; the privileged helper requires the
    literal GitHub release form (`v1.20.0`) and refused with `tag_invalid`.
    `NativeUpgrader.start()` now canonicalizes the tag; the helper script
    also accepts and re-canonicalizes requests written by older gateways.
    Docker mode is untouched (GHCR image tags carry no `v`).
  - `install.sh` run as root let uv place its managed CPython under
    `/root/.local/share/uv`; the hardened `User=corlinman` unit could never
    exec the venv interpreter (`status=203/EXEC`), so every native one-click
    upgrade health-failed into rollback — and the rollback failed the same
    way. The python store is now pinned to `$PREFIX/uv-python` with a
    one-shot venv-rebuild migration for existing installs.

## [1.20.0] — 2026-06-11 — CLI agent console + claude-code parity wave 1 + multi-model + enterprise chat

> Minor release. Config-compatible — no migration required. New optional
> config: `[console]` block (small_fast_model / auto_route / compaction).

### Added
- **`corlinman console` — interactive CLI agent console** (`docs/PLAN_CLI_CONSOLE.md`).
  A terminal REPL that hosts the *full* agent brain in-process (builtin tools,
  `subagent.spawn*` multi-agent fan-out, memory, journal — identical wiring to
  production, served on a private per-process UDS), or attaches to a running
  gateway with `--attach URL` (OpenAI-SSE client, opencode-style client/server
  split). Design follows claude-code first (turn loop, `-p/--print` one-shot
  pipe mode, small-fast-model routing) and hermes-agent for the console UX
  (slash commands, Ctrl-C interrupts the running turn, tool-progress modes
  `off|new|all|verbose`, session resume).
  - Slash commands: `/help /new /clear /model /models /session /sessions
    /resume /usage /status /progress /verbose /quit`.
  - Model routing: `[console].small_fast_model` + opt-in `[console].auto_route`
    sends classified-simple turns to the cheap model; an explicit `--model` or
    `/model` choice always wins.
  - Sessions persist in the same `agent_journal.sqlite` the gateway uses;
    `/resume <key>` replays a session's journaled turns into the window.
- **claude-code parity wave 1** (`docs/PLAN_CLAUDECODE_PARITY.md`, matrix in
  `docs/parity-matrix-2026-06-11.json`):
  - **Cross-channel session commands** — `/new`(/新会话), `/model`(/模型) and
    `/usage`(/用量) now work on EVERY surface (QQ/Telegram/Discord/Slack/
    Feishu/web/console) via the shared channel command registry, backed by a
    new per-binding prefs store (`binding_prefs.sqlite`: model override +
    session epoch, honored at the two request-builder choke points). The
    console additionally falls through to the shared registry, so `/persona`,
    `/whoami`, `/status` etc. work in the terminal too.
  - **CORLINMAN.md project memory** (CLAUDE.md analog): user-global + repo-root
    → cwd discovery, `CORLINMAN.local.md`, `@include` directives with cycle
    protection, `/memory` command.
  - **Context compaction**: `/compact` + opt-out auto-compaction of the console
    window (threshold/keep-recent configurable under `[console]`, summarizer
    runs on the small-fast model, 3-failure circuit breaker).
  - **Structured headless output**: `corlinman console -p --output-format
    json|stream-json` (claude-code result-envelope contract) + `--max-turns`.
  - **Live todo checklist** — `todo_write` tool calls render as a ☐/◐/☒
    checklist with the in-progress activeForm, deduped between updates.
  - **`/rewind` workspace checkpoints** — every chat turn already snapshots the
    agent workspace (git-backed); `/rewind` lists checkpoints and restores one,
    truncating the console window when the checkpoint maps unambiguously.
- **Multi-model adaptation (适配几乎所有模型)** — the provider layer now
  handles nearly every model family correctly:
  - OpenAI o1/o3/o4/gpt-5 reasoning models: `max_completion_tokens` instead of
    `max_tokens`, `temperature` omitted (they reject it).
  - DeepSeek R1 / QwQ reasoning streams: `reasoning_content` surfaces as
    `is_reasoning` token chunks (rendered dim, hidden by default) and is
    stripped on replay (R1 rejects echoed reasoning).
  - Strict-alternation models (DeepSeek/Qwen/GLM): consecutive same-role
    messages are merged instead of erroring.
  - Tool-less models: `supports_tools()` on providers + per-provider/alias
    `tools = false` param → the servicer skips builtin-tool injection and the
    turn degrades to text-only reasoning instead of a 400.
  - Vendor error mapping for DeepSeek/Qwen/GLM (billing vs rate-limit vs
    auth vs context-length), Moonshot/Kimi + Mistral/Codestral + bare
    `llama-*` (Groq) prefixes added to the auto-routing table.

- **Enterprise-grade web chat (#90)** — all 5 reported chat problems fixed:
  - chat SSE now sends 10s comment heartbeats (+ a 45s front-end stall
    watchdog), so long tool calls (image generation) no longer die to proxy
    idle timeouts as "network error";
  - streaming errors are sent as legal chunks (`finish_reason=error`) and
    rendered as a turn error with a retry button — no more stuck loading
    bubbles; cancelling shows immediate "stopping…" feedback and a neutral
    "stopped" state;
  - **file pipeline end-to-end**: new `/v1/files` upload/download (25MB cap,
    SVG forced-download against XSS), OpenAI content-parts through the whole
    chain, `attachments_json` journal persistence, history re-render;
  - the assistant can now send images: media paths in tool results are
    registered with the file service and rewritten to fetchable URLs.

### Fixed
- **The months-old CI 6-hour py-test hang is root-caused and fixed**: an
  abandoned (`break`-ed) `journal.iter_events` async generator finalized its
  aiosqlite cursor on a dead event loop and killed the worker thread, wedging
  every later journal call. `iter_events` is now break-safe by construction
  (internal paging, cursors closed before yielding) — py-test passes
  deterministically.

## [1.19.1] — 2026-06-11 — Upgrade progress bar + clearer manual fallback

> Patch release. Config-compatible — no migration required.

### Added
- **One-click upgrade now shows a determinate progress bar** that fills through
  the phases (validating → pulling → recreating → healthcheck → done) to 100%,
  snapping green on success and holding red at the phase a failure reached
  (backend failure codes like `image_pull_failed` / `timeout` no longer reset
  the bar to empty).

### Fixed
- **Clearer dead-end when one-click upgrade isn't available.** On a manual-only
  deployment (e.g. a root-owned native box that upgrades via the runbook), the
  upgrade dialog now explains *"one-click isn't available here — use the manual
  commands below"* instead of surfacing a cryptic toast.
- A `stalled` upgrade now renders as an error state in the progress bar (red),
  and `cancelled` as a neutral stop — neither still looks like an in-flight
  upgrade.

## [1.19.0] — 2026-06-11 — Spatial Glass UI redesign + CI hang fix

> Minor release. Config-compatible — no migration required. The admin UI is
> fully restyled; theme/language preferences are preserved.

### Added
- **Spatial Glass design system.** Full visionOS-style admin redesign:
  deep-space navy backdrop, layered frosted glass, depth/elevation, soft glow,
  large radii. Dark-first with a light token fallback. The legacy `tp-*`
  (Tidepool) token namespace is fully removed in favour of canonical `sg-*`.
- **Liquid Glass optics.** Apple WWDC25-style light interaction — a real
  pointer-tracked cursor radiance, specular glass highlights, chromatic edge
  refraction, and non-linear spring motion (all `prefers-reduced-motion` safe).
- **Theme Studio.** Custom theming extended to the *whole* theme — accent hue +
  intensity, canvas hue + chroma (taste-capped), and a user-adjustable **glass
  opacity** — plus six designer presets (深空/极光/暮霭/玫瑰/墨玉/鎏金). Generated
  CSS is persisted and injected pre-paint (no flash).
- **ChatGPT/Claude-grade chat.** Multi-type bubbles, streaming, markdown + code
  rendering, quoted replies, and a mascot empty-state.

### Changed
- **Outbound text normalization is now channel-capability-aware.** Markdown-flatten
  + AI-punctuation cleanup runs only on plain-text channels (QQ, Telegram,
  WeChat, Feishu); **Discord and Slack keep their native markdown/mrkdwn** so
  formatting and escaped mentions survive verbatim.
- The "每日说说" (daily post) feature now applies to **all persona agents**, not
  just the Grantley persona. QZone admin pages gained full zh/en switching.

### Fixed
- **The 6-hour CI py-test hang is gone.** The admin SSE catch-up replay no longer
  tears down an aiosqlite cursor mid-iteration under client disconnect. Replay is
  now a bounded `LIMIT`-paged read against a snapshot upper bound, with
  turn-scoped exact-sequence live dedup (bounded memory). CI jobs gained
  `pytest-timeout` + `timeout-minutes` hard rails.
- **`ask_user` no longer double-sends on Telegram.** A single-chunk prompt edits
  the placeholder *with* its keyboard instead of editing then re-sending, with a
  send fallback when the edit is rejected.
- **Plain-text channel replies no longer carry cluttered AI markdown/punctuation.**
- **NapCat embedded WebUI proxy** never forwards the admin cookie upstream
  (header allowlist); the scan-login/config scope and trusted-managed-NapCat
  assumption are documented.

## [1.18.2] — 2026-06-06 — Multi-agent status link noise fix

> Patch release for channel reply hygiene. No config migration is required.

### Fixed
- **Normal channel replies no longer append a live status link.** The
  shareable `🔗 实时状态` link is now sent only when the parent turn actually
  dispatches sub-agents via `subagent_spawn`, `subagent_spawn_many`, or
  `subagent_spawn_inline`.
- **Multi-agent fan-out still surfaces exactly one status link.** The link is
  sent as a standalone message when the first sub-agent starts; if that early
  send fails, the final reply appends one fallback link instead of dropping it
  or duplicating it.

## [1.18.1] — 2026-06-05 — NapCat QR refresh hardening

> Patch release for QQ scan-login reliability. No config migration is required.
> Existing NapCat sessions and channel settings remain compatible.

### Fixed
- **NapCat QR refresh no longer reports success while serving the same stale
  QR.** The gateway now compares the QR before and after `RefreshQRcode`; if
  NapCat's best-effort refresh is a no-op, it asks NapCat to restart, clears
  the stale WebUI credential, and waits for a different QR before returning.
- **Embedded NapCat WebUI refresh can be routed through corlinman's robust
  refresh path.** A hidden compatibility route at
  `POST /api/QQLogin/RefreshQRcode` returns NapCat's normal response envelope
  while using the gateway no-op detection/restart fallback.
- **NapCat WebUI token resolution is more tolerant across deployment modes.**
  The gateway now accepts `WEBUI_TOKEN` in addition to `NAPCAT_WEBUI_TOKEN` and
  `NAPCAT_WEBUI_SECRET_KEY`, matching NapCat's native environment variable.

### Documentation
- Added nginx and VPS runbook notes showing that `/api/QQLogin/RefreshQRcode`
  must exact-match to the gateway before the generic NapCat `/api/` proxy.
- Added shared `NAPCAT_WEBUI_TOKEN` / `WEBUI_TOKEN` defaults to the env template
  and native installer so fresh QQ installs keep gateway and NapCat credentials
  aligned.

## [1.18.0] — 2026-06-05 — Persona liveness, provider discovery, and deployment hardening

> Feature + hardening release. This ships the Grantley persona liveness wave,
> default-off evolution scheduler jobs, draft provider model discovery, and the
> deployment fixes needed for reliable local/full Docker image builds. It also
> closes the QQ/NapCat follow-ons found during the audit pass. Existing config
> remains compatible; the new scheduler/evolution jobs are opt-in/default-off.

### Added
- **Persona liveness is now wired end-to-end.** Personas are surfaced across the
  conversation paths, gain life-state admin API/UI support, can upload and serve
  per-persona visual assets, and can be exported/imported from the CLI. The
  scheduler now includes builtins for persona decay, life advance, and QZone
  daily work, with regression coverage for the chat, admin, asset, and CLI
  flows.
- **Default-off R8 passive evolution jobs.** The evolution engine `run_once`
  and shadow-test scheduler builtins are available without changing existing
  operator behavior, backed by a new `corlinman-shadow-tester` package and
  scheduler tests.
- **Draft provider model discovery.** The admin provider test flow can fetch
  model lists from draft provider configs while reusing saved keys safely and
  avoiding stale discovery results.

### Fixed
- **Local/full Docker image builds are stable again.** The Docker build path now
  builds from source, carries the selective-install runtime dependencies the
  gateway actually imports, avoids proto workspace resync drift, and handles
  empty `uv` arg expansion on bash 3.
- **Provider calls no longer receive Codex-only extras unless the target
  provider is Codex.** Internal chat metadata is filtered before normal
  provider dispatch so non-Codex providers don't see unsupported fields.
- **QQ/NapCat reliability fixes.** NapCat OneBot auth tokens survive config
  edits, the OneBot WebSocket server is ensured after login, and QQ local image
  attachments are sent as OneBot `base64://` image payloads instead of Docker-
  local `file://` paths.
- **Persona/admin polish.** Persona editor scrolling is stable after the large
  liveness UI expansion, and the PR-status label workflow has the write
  permission it needs.

## [1.17.0] — 2026-06-04 — Codebase modularization for multi-developer collaboration

> **Internal refactor release — no behavior changes, no config/wire-protocol
> changes, no data migration. Safe upgrade.** Every change is a verbatim
> *extract-and-reimport* (move a cohesive group of definitions into a sibling
> module, re-import the names so the public surface stays byte-for-byte), so
> all external importers keep working unchanged. The goal is to dissolve the
> "god-file" merge magnets that forced contributors to serialize on a handful
> of huge files, so owner-areas can now iterate in parallel behind stable
> seams. Validated per change with ruff + mypy + import-linter + boot smoke +
> targeted suites; the boot-critical and security-critical splits each passed
> a dedicated adversarial review.

### Changed
- **`gateway/lifecycle/entrypoint.py` decomposed: 3680 → 1769 LOC (−52%).** The
  boot orchestrator is split into focused sibling modules, all re-exporting
  through `entrypoint` so `build_app` and every import path are unchanged:
  `cli_helpers` (CLI/config-path helpers), `bootstrap_constants` (constants +
  scheduler/identity helpers), `config_loading` (config load + hot-reload
  watcher), `app_factory` (app-state + route builders + middleware/UI-static
  installers), `c2_wiring` (C2 / plugin-hotload / agent-runner wiring) —
  joining the earlier `config_resolve` / `scheduler_integration`. The residual
  is just `build_app` + the irreducible `lifespan` closure + `_serve` + `main`.
  The in-`build_app` middleware install order is provably byte-identical
  (verified via `app.user_middleware` with and without CORS configured).
- **`routes_admin_a/auth.py` 931 → 773 LOC:** the mechanical layer (wire-models,
  constants, the stateless login-rate-limiter, pure format/error helpers) moved
  to `_auth_lib`; **all security logic stays in `auth.py`** (argon2 hash/verify,
  sessions, cookie/TLS, forwarded-proto trust, credential persistence, locks,
  router + handlers). `__all__` unchanged.
- **26 god-files decomposed total** across the initiative: both admin route
  bundles (`routes_admin_a` / `routes_admin_b`) split into per-concern
  subpackages and per-route `_lib` helper siblings; the largest non-route
  domain files (`routes_voice/mod`, `evolution/background_review`,
  `grpc/placeholder`, `grpc/plugin_invoker`, `services/chat_service`,
  `services/direct_backend`, `evolution/curator`) split into cohesive siblings.
- Documentation: `docs/architecture-modules.md`, `docs/modularization-plan.md`,
  and `docs/PLAN_decompose_cores.md` updated to reflect the completed structure;
  `CONTRIBUTING.md` / PR template carry the module map + owner-areas.

### Fixed
- Removed remaining stale hardcoded version chrome from the README (badge +
  "what's new" + roadmap pinned at 1.10.0); the canonical version is surfaced on
  `/admin/system` and via the package metadata the update-checker reads.

## [1.16.0] — 2026-06-01 — Marketplace: Skills / MCP / Plugins (GitHub-backed, hot-plug) + mascot

### Added
- Unified **marketplace** for skills, MCP servers, and plugins, served from a
  curated GitHub registry repo (`sweetcornna/corlinman-marketplace`) with
  sha256-verified downloads; the legacy clawhub.ai source is retained behind a
  toggle. New package `corlinman_server.system.marketplace`.
- **GitHub-acceleration** for China-region hosts (`[marketplace.github_proxy]`:
  `off`/`auto`/`on`; presets ghproxy / jsdelivr / mirror / custom). The token
  is never sent through a third-party proxy.
- **MCP hot-plug**: install staged, enable to hot-connect a live server; new
  `/admin/mcp/*` routes; the previously-dead `/admin/plugins/{name}/{enable,
  disable,restart}` seam is now wired via `McpAdapter`. Installed specs persist
  in `<data_dir>/mcp_servers.sqlite` and reconnect on boot.
- **Plugin true hot-load**: install staged, enable to load into the live
  `PluginRegistry` with no restart; new `/admin/plugins/market/*` routes; specs
  persist in `<data_dir>/plugins.sqlite`.
- Admin UI: `/marketplace` (Skills / MCP / Plugins tabs), an Acceleration
  settings page, and an in-app bilingual **Contribute** guide.
- Seeded catalog: 21 MCP servers, 10 skills, 3 example plugins, plus a
  contribution guide + `build-registry.py` in the registry repo.

### Changed
- The brand glyph is now the corlinman mascot.

### Fixed
- Removed stale hardcoded version strings from the sidebar and brand mark — the
  canonical version is surfaced on `/admin/system`. Bumped `corlinman-server`
  to match the release version.

## [1.15.2] — 2026-05-31 — Deferred audit items: multi-tenant authz hardening + residual wiring

> The latent multi-tenant authz items + residual wiring follow-ons deferred
> from v1.15.1. Reproduce-first; full non-live suite green; real-run-verified
> on the production VPS that the holes close AND the existing single-operator
> default-tenant chat + admin flows are unaffected (chat key → 200 `pong`;
> an `embeddings`-scope key → 403 `insufficient_scope` on /v1/chat).

### Security — multi-tenant authz (latent; activates when multi-tenant is enabled)
- **`revoke_api_key` is tenant-scoped** (`tenant_id` + `key_id`) — a tenant can
  no longer revoke another tenant's key by id. (SEC-06a)
- **`TenantScopeMiddleware` is installed** for `/admin/*` and `/v1/*` with a
  transparent default-tenant fallback (single-operator deployments unaffected:
  `enabled=False` → everything resolves to `default`). API-key routes read the
  middleware-resolved tenant instead of a client-supplied `?tenant=`. (SEC-06b)
- **API-key `scope` is enforced** — `/v1/chat*` requires the `chat` scope (which
  existing prod chat keys hold); a narrower scope (e.g. `embeddings`) is 403'd
  `insufficient_scope`. Scoped to the chat endpoints only (plugin callbacks,
  models, voice, memory, canvas remain api-key-authenticated but not
  chat-scope-gated); super-scopes (`*`/`full`/`admin`) bypass. (SEC-09)
- **Evolution proposals persist `tenant_id`** and the meta-recursion cooldown is
  per-`(tenant, kind)` instead of one global cooldown. (BUG-09)

### Wiring / reliability
- Auto-rollback operator-apply routes thread the configured signal window so the
  apply-time metrics baseline matches the monitor's window (was a default-window
  mismatch that could trigger false breaches). (BUG-08 caller)
- Discord / Slack / QQ-official / WeChat runners bootstrap command extensions —
  commands-dir + skill commands + `$ARGUMENTS` now work on these channels too
  (parity with Telegram / QQ-OneBot / Feishu). (CMP-07 parity)
- Subagent mailbox queues are bounded (drop-oldest overflow, env-tunable) and
  tenant-namespaceable — bounded memory under a flooding sender.

### Not done by design
- `evolution-engine` budget-signal `tenant_id` (BUG-09b): `EvolutionEngine.run_once`
  is a single multi-tenant pass with a global per-kind budget gate, so there is
  no run-level tenant to attribute the signal to (TODO left in `engine.py`).

## [1.15.1] — 2026-05-31 — Whole-project audit fixes (security / DoS / reliability / completeness)

> A 16-auditor whole-project review (`audit/audit-2026-05-31/PLAN.md`) found 32
> issues (0 Critical). This release fixes the 27 High+Medium items with a
> reproduce-first discipline (a failing test proved each bug before the fix);
> full non-live suite green (~4097, 0 fail); every fix real-run-verified on the
> production VPS (both units), incl. the hook gate, calculator bomb, SSRF,
> shell-deny, memory recall, and a live chat. The 12 Low + the latent
> multi-tenant-only authz items (SEC-06/09, BUG-09) are deferred.

### Security (model-reachable / DoS)
- **Skill allowed-tools no longer leaks across sessions.** `_active_skills` was
  process-global on the singleton servicer (any session's skill pull narrowed
  every other session and never reset); re-keyed per session and cleared at
  turn end. (SEC-01)
- **`subagent_stop`/`cancel_session` is ownership-gated** — a session can only
  cancel itself or a descendant child, not any session by key. (SEC-02)
- **Scientific calculator can't bomb the event loop** — result-bit-length guard
  rejects nested-power bignums, and `dispatch_calculator` runs via
  `to_thread`+`wait_for`. (SEC-03)
- **`web_fetch` now fences fetched bodies** in untrusted-content markers as its
  schema promises (was a dead import). (SEC-04)
- **`run_shell` per-arg deny rules can't be bypassed** by compound/subshell/
  path/env-prefixed command shapes — every `;|&` segment is normalized and
  matched. (SEC-05)
- **`GET /admin/config` redacts channel bot/app tokens + the NapCat token**
  (were returned in cleartext). (SEC-07)
- **`vision_analyze` runs the SSRF host guard** and rejects `http://`
  (https-only), closing a provider-side SSRF to cloud-metadata. (SEC-08)

### Bugs / reliability
- **The blocking pre-tool hook gate now actually runs in the production split
  topology** — the standalone agent builds + holds a `HookRunner` (it was only
  built in the gateway process, so the gate was silently inert). (BUG-01)
- **Stalled/orphaned upgrades no longer wedge the upgrader forever** — `stalled`
  is terminal-only and a cold-start reconciles orphaned `running` records, so a
  retry is allowed. (BUG-02)
- Async pre-tool path merges specific+generic hook decisions (was dropping
  `mutated_args`/`inject_message`). (BUG-03)
- Per-parent `child_seq` counter prevents same-card subagent spawns from
  colliding on session/agent id. (BUG-04)
- `read_file` advances past a single over-long line instead of looping on the
  same offset. (BUG-05)
- Degraded-boot teardown reads `state.extras` defensively (was aborting all
  shutdown cleanup, leaking stores). (BUG-06)
- OneBot `parse_event` tolerates malformed fields instead of tearing down the QQ
  WebSocket. (BUG-07)
- Auto-rollback applier writes a real metrics baseline (was `{}`, which the
  monitor always rejected). (BUG-08)
- agent-brain IndexSync upsert + query share the configured namespace. (BUG-10)

### Performance
- Namespace-scoped memory recall uses a JOIN instead of an `IN(...)` bind list
  that crashed past SQLite's variable limit. (PERF-01)
- Graph back-links resolve without JSON-decoding the whole namespace per
  recall. (PERF-02)
- `GET /admin/curator/profiles` mtime-caches the skill registry (no full
  SKILL.md re-walk per poll). (PERF-03)

### Completeness
- `must_change_password` no longer 403s the onboarding `finalize-*` routes, so a
  fresh install can complete the wizard. (CMP-01)
- allowed-tools is enforced for injected/always-on/card skills too, not just
  on-demand `Skill()` pulls. (CMP-02)
- Episodes `ONBOARDING` kind is reachable via an `onboarding_first_n` knob.
  (CMP-03)
- The permission `ask` verdict is now wirable (resolver setter); strict mode
  denies `memory_write`/`send_attachment`/`text_to_speech`. (CMP-04/05)
- `SlashAccessPolicy` is enforced and the commands-dir loader + `$ARGUMENTS`
  substitution are wired. (CMP-06/07)
- Feishu resolves its bot `open_id` so the group @mention gate works. (CMP-08)

### Notes
- Residual follow-ons (flagged, not blocking): the auto-rollback operator-apply
  routes should thread the rollback config to enable baseline capture there;
  per-channel-runner command-extension bootstrap (Discord/Slack) for full
  CMP-07 parity; an interactive `ask` resolver needs a prompt channel the
  headless agent lacks. Deferred latent multi-tenant authz (SEC-06/09, BUG-09)
  remains for a multi-tenant hardening pass.

## [1.15.0] — 2026-05-31 — Agent-parity second wave: new tools, tool-craft, reliability extensions

> Follow-on to the v1.14.0 agent-parity work, driven by a fresh three-way
> re-audit vs claude-code / hermes-agent / openclaw. Implemented across 12
> file-disjoint lanes + 2 wiring agents; full non-live suite green
> (~4097 passed, 0 failed); deployed and prod-verified on a native VPS
> (real chat OK, `doctor` 9/9, new tools registered).
>
> **Honesty note:** the re-audit was partly anchored to a gaps list that
> predated v1.14.0, so a subset of these changes *refine or extend* features
> v1.14.0 already shipped (scientific calculator, Anthropic prompt-caching,
> `run_agent` cron, the `HookRunner` core) rather than adding them fresh. The
> genuinely net-new items are called out below.

### Added (net-new)
- **`read_file` multimodal:** PDF (per-page text via optional `pypdf`/
  `pdfminer`, base64 fallback) and Jupyter `.ipynb` (cells + outputs) branches.
- **`search_files` ripgrep parity:** `output_mode` (content / files_with_matches
  / count), case-insensitive, `-A/-B/-C` context lines, glob/type pre-filter,
  verbatim (un-stripped) match lines, mtime-sorted filename mode.
- **`web_fetch` extraction:** optional `prompt` param → HTML→Markdown (optional
  `markdownify`/`html2text`, stdlib fallback) + `next_offset` paging; untrusted-
  content suspicious-pattern detection surfaced as a separate signal.
- **New agent tools:** `text_to_speech`, `memory_write` / `session_search` /
  `memory_read` (over the existing FTS store), and an opt-in `execute_code`
  REPL (disabled by default; not advertised unless enabled), plus a model-
  callable `subagent_stop`.
- **MCP server:** `ToolAnnotations` (readOnly/destructive/idempotent hints,
  title, outputSchema) + `tools/list_changed` notifications (capability now
  advertises `listChanged: true`).
- **Hooks:** `emit_collect` decision path + new lifecycle events
  (SessionStart/End/Reset, Pre/PostCompact, Stop, PreToolDispatch) +
  file-based `HOOK.yaml`/`handler.py` discovery, extending the v1.14.0
  `HookRunner`.
- **Skills:** carry previously-dropped frontmatter (whenToUse / paths /
  platforms / model / effort / hooks), `disable_model_invocation`, and a
  tarball sha256 + static-scan trust gate.
- **Permissions:** `ask` verdict via the approval gate, per-argument/command
  pattern rules, permission modes (acceptEdits/plan/bypass/default).
- **Edit fidelity:** CRLF/BOM/curly-quote match-time normalization with EOL/
  encoding round-trip, a tier-4 block-anchor fuzzy matcher, and a changed-
  region diff snippet in edit/write results.
- **Persona/identity wiring:** `persona_resolver` + `agent_id` stamping so
  `{{persona.*}}` placeholders resolve; identity store assigned + `/admin/
  identity*` routes un-503'd + verification-phrase sweep scheduled.
- **Session/cancel primitives** (`Session` bundle, `cancel.combine`).
- **Channels:** `[MSG_BREAK]` outbound bubble split (fixes a user-visible
  token leak), inbound attachment/album handling + sender/reply attribution,
  a commands-dir loader with `$ARGUMENTS` substitution + ACL + unknown-command
  notice.

### Changed / refined (on top of v1.14.0)
- **Anthropic provider:** added `tool_result` `is_error`, `anthropic-ratelimit-
  unified-reset` parsing, single-flight OAuth refresh + one-shot 401 recovery,
  macOS Keychain credential import, and a richer `ContextOverflowError`
  (parsed limit) for loop-side shrink-retry — alongside the pre-existing
  prompt-caching. Removed an adapter-level retry loop that double-retried and
  could block on `Retry-After` (transient retries stay with the SDK; cross-call
  backoff/fallback live in the reasoning loop).
- **Reasoning loop:** wired retry/backoff + cross-model fallback on sustained
  overload, history dedup, CJK/multimodal-aware token estimate, per-model USD
  cost on the done event, and context-overflow shrink-retry.
- **Config hot-reload** is now **opt-in** (`CORLINMAN_CONFIG_HOT_RELOAD` /
  `[server].config_hot_reload`, default off) — a per-boot fs-observer otherwise
  accrued OS watch handles; added a best-effort `/admin/config/schema`.
- **Memory recall:** opt-in query-time exponential decay re-rank + residual-
  pyramid boost (BM25 side; dense-vector recall remains deferred).

### Fixed
- **Native upgrader:** `resolve_upgrader` forwarded an `audit_log` kwarg that no
  upgrader `__init__` accepts (and `data_dir` to the docker impl), so the
  in-app upgrader silently failed to initialise on native deploys
  (`gateway.system.upgrader_init_failed`). Pre-existing in v1.14.0; surfaced
  during real-operation testing and fixed.

### Deferred (in-flight collision / heavy-dep — reported, not built)
- Coordinator durable mailbox / re-addressable teammates (collides with the
  live subagent subsystem), EvolutionApplier materialisation (Rust→Python
  migration boundary), dense-vector recall (embedding deps; RAM-constrained
  hosts), MCP outbound Streamable-HTTP/SSE transports, and destructive
  session-control slash commands (`/clear` `/reset` `/stop`).

## [1.14.0] — 2026-05-31 — Agent-parity gap fills (audit Waves A–E)

> Full implementation sweep from the 2026-05-31 three-way gap audit
> (corlinman vs claude-code / hermes-agent / openclaw — 50 verified gaps in
> `audit/gap-fill-2026-05-30/GAP_REPORT.md`). Waves A–E shipped across 6
> packages; all tests green. Identity-store (already wired), TTS backend
> (no Python API), and EvolutionApplier real-apply (Rust→Python migration
> collision) are the only items intentionally left for later.

### Added (Wave E — hooks / coordinator / multiagent)
- **Blocking/discoverable hooks.** `HookRunner` class in `corlinman-agent`
  intercepts every tool dispatch via `pre_tool`/`post_tool`/`notification`
  events; if a registered shell command exits non-zero the tool call is blocked
  and the model receives the hook's stdout as the error message. Hooks are
  configured via the agent config `hooks` dict. A `GET /admin/hooks` endpoint
  lists active hooks. (cc parity — claude-code hooks.)
- **Agent mailbox / `send_message` + `recv_message` tools.** An in-process
  `AGENT_MAILBOXES: dict[str, asyncio.Queue]` store lets agents address each
  other by `agent_id`. `dispatch_send_message` enqueues a message;
  `dispatch_recv_message` dequeues with optional timeout. Registered in
  `BUILTIN_TOOLS` and `_dispatch_builtin`. (hermes coordinator parity.)

### Added (Wave D — wiring revivals)
- **`run_agent` cron dispatcher.** `scheduler/runner.py` now has a full
  `RunAgent` dispatch branch that invokes `app_state.agent_runner_fn` and
  emits `EngineRunCompleted` / `EngineRunFailed` on the hook bus; degrades
  gracefully to `error_kind="runner_not_registered"` when no runner is wired.
- **Agent-callable `memory_search` + `session_search` tools.** New
  `corlinman_agent/memory/` package registers both tools in `BUILTIN_TOOLS`;
  they query a pluggable `memory_host` interface and degrade to empty results
  when none is wired. (hermes long-memory parity.)
- **History dedup.** `_dedup_tool_results()` strips exact duplicate
  `(tool_name, args_json, content)` triples from prior history turns before
  each `_extend_with_tool_round`, replacing repeated content with a sentinel
  so tool-call-id chains stay structurally valid.
- **CJK-aware token estimator.** Text segments containing CJK Unified
  Ideographs (U+4E00–U+9FFF) are multiplied by 1.5× in `_estimate_chars()`,
  correcting the systematic under-budget that caused CJK-heavy turns to
  overflow the context window.

### Added (Wave C — multimodal)
- **Multimodal `read_file`.** `dispatch_read_file` now detects image
  extensions (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`) and returns a
  `list[dict]` image content block (base64 data-URL) instead of a text
  envelope, letting vision-capable models inspect images in the workspace.
- **`vision_analyze` tool.** New `corlinman_agent/image/analyze.py` module
  — accepts `{path}` (workspace file) or `{url}` (https URL) plus optional
  `{question}` and returns a multimodal content-block list the model can
  reason over. Registered in `BUILTIN_TOOLS`.
- **`ToolResult` content-block plumbing.** `ToolResult.content` widened from
  `str` to `str | list[dict[str, Any]]`; `_extend_with_tool_round` forwards
  list content verbatim (bypasses `_truncate_tool_result`) so image parts
  produced by tools reach the provider API unchanged.

### Added (Wave B — reliability)
- **Retry with exponential backoff.** The reasoning loop retries on HTTP 429,
  500, 502, 503, 504, `RateLimitError`, and `OverloadedError` — up to 3
  attempts, only before the first streaming event is emitted. (hermes parity.)
- **Prompt caching on last 2 user turns.** `AnthropicProvider` injects
  `cache_control: {type: ephemeral}` on the system prompt and the two most
  recent user turns when the model supports caching and the system prompt
  exceeds 256 estimated tokens. Reduces repeat-request cost on long sessions.
- **Per-model USD cost tracking.** `_MODEL_COSTS` table + `_estimate_turn_cost_usd()`
  accumulate `turn_cost_usd` and `session_cost_usd`; both are populated on
  every `TurnComplete` emit. `ReasoningLoop.session_cost_usd` property exposed.
- **Context-overflow shrink-and-retry.** `ContextOverflowError` (and matching
  string patterns) tightens `context_budget` by 20%, re-compacts history, and
  retries the call once before propagating the error.
- **Model fallback chain.** `ReasoningLoop.__init__` accepts `fallback_models`
  (default `["claude-sonnet-4-6", "claude-haiku-4-5"]`); on
  `ModelNotFoundError`, billing errors, or quota exhaustion the loop switches
  to the next model before streaming starts.

### Added (Wave A — remaining)
- **`[MSG_BREAK]` bubble splitting.** All 7 channel senders (QQ, Telegram,
  Discord, Slack, Feishu, QQ Official, WeChat Official) now split outbound
  text on `[MSG_BREAK]` and send each segment as a separate message with a
  0.3 s inter-bubble delay. The `grantley` persona was emitting the raw token
  to users; it no longer does.
- **OAuth identity headers.** `AnthropicProvider` detects OAuth bearer tokens
  and adds `anthropic-beta: oauth-2025-04-20`, `x-app: cli`,
  `user-agent: claude-cli/2.1.88 (claude-code)`, and a "You are Claude Code"
  system-prompt prefix so Claude subscription tokens are accepted by the API.
- **Config hot-reload wiring.** The existing `ConfigWatcher` instance (stored
  on `AppState`) is now copied into `admin_b_state.extras["config_watcher"]`
  during the lifespan startup block; `POST /admin/config/reload` was silently
  receiving `None` from `state.extras.get("config_watcher")` before this fix.
  A `config_swap_fn` closure is also wired to keep `state.config`,
  `app.state.corlinman_config`, and `ConfigWatcher._snapshot` consistent on
  manual `POST /admin/config` writes.

### Added
- **Model-aware compaction budget.** `ReasoningLoop` now sizes its per-round
  context budget from the model's declared context window
  (`window − reserved_output`, reserve capped at 48k) instead of a flat 120k
  constant, via a best-effort `provider.context_window(model)` accessor
  (implemented on `DeclarativeProvider`, returning the matching
  `ModelSpec.context_length`). A 1M-token model no longer compacts at 120k and a
  32k model no longer overflows. `$CORLINMAN_CONTEXT_BUDGET` still pins every
  model when set; providers without the accessor keep the flat default
  unchanged. (`RESEARCH_AGENT_PARITY` A1 / claude-code `getEffectiveContextWindowSize`.)
- **read_file truncation guidance.** A truncated read now cuts on a line
  boundary and returns `next_offset` + a `hint` (continue from offset / narrow /
  use `search_files`) instead of a silent head slice the model would just
  re-read. (parity B1.)

### Changed
- **Read-before-edit guard (claude-code parity B5/C5).** When a `FileState` is
  threaded (the production agent path), `edit_file` now refuses to edit an
  existing file the agent never read or wrote this turn (`file_not_read`), since
  `is_stale` returns `False` for an unrecorded path and a blind edit could
  clobber unseen bytes. A new `FileState._seen` set tracks observed paths and
  survives the post-write/edit cache `forget`, so read→edit, write→edit, and
  consecutive edits all proceed; the re-read cache semantics are unchanged.
  **Behavior change for deployments** — the model may need to read before a
  cross-turn edit (it self-corrects); disable with
  `CORLINMAN_REQUIRE_READ_BEFORE_EDIT=0`.

## [1.13.2] — 2026-05-30 — Subagent persona-state seeding fix

### Fixed
- **Subagent spawns now seed the child's persona-state row.** All three spawn
  dispatch paths in the agent servicer (`subagent_spawn` / `_many` / `_inline`)
  threaded the system-prompt **registry** store
  (`corlinman_server.persona.PersonaStore`, `personas.sqlite`) into the child
  runner's `_seed_child_persona`, which needs the tenant-aware **state** store
  (`corlinman_persona.store.PersonaStore`, `agent_state.sqlite`). The registry's
  `get()` rejects `tenant_id=`, so every child spawn logged
  `subagent.runner.persona_seed_failed: unexpected keyword argument 'tenant_id'`
  and the child's mood/fatigue state row was silently never written (seeding is
  best-effort, so spawns still succeeded — which masked the bug). Now passes
  `_get_persona_state_store()`. Surfaced by a live prod fan-out smoke test of
  the v1.13.1 deploy; covered by a new `test_servicer_spawn_seeds_child_persona_state`
  regression that asserts the row is actually present.

## [1.13.1] — 2026-05-30 — Multi-agent subsystem hardening

> Fixes a packaging defect that crashed every subagent fan-out with
> `No module named 'corlinman_subagent'` in any non-`--all-packages` install,
> then closes 13 adversarially-confirmed defects across the subagent spawn /
> supervisor / dispatcher stack — surfaced by a multi-agent audit and graded
> against Claude Code's subagent semantics. The two highest-impact fixes:
> child tool-allowlists are now enforced at the **execution** boundary (not
> just hidden from the schema), and orphaned background rows no longer wedge a
> tenant's quota across a restart.

### Fixed
- **`corlinman-server` now declares its `corlinman-subagent` dependency.** The
  agent servicer lazily imports `corlinman_subagent.supervisor` to enforce the
  subagent caps but never declared the package — it free-rode on
  `uv sync --all-packages`. A published-wheel or stale-venv install dropped it,
  crashing every `subagent_spawn` / `_many` / `_inline` fan-out with
  `No module named 'corlinman_subagent'`. The dependency edge is now explicit
  in the lockfile.
- **Child tool-allowlist enforced at execution (D1).** `run_child` filtered the
  child's *advertised* tool schema but never checked tool names at execution,
  so a model that emitted a hidden tool ran it with the parent's authority. The
  drain now refuses any tool outside the child's allowlist with a
  `tool_not_in_allowlist` envelope — advertised toolset == usable toolset.
- **Orphaned background subagent rows no longer wedge tenant quota (D3).** A row
  persisted as `queued`/`running` whose driving task died on restart stayed
  "in-flight" forever and counted against the 15-slot per-tenant quota until
  every future background spawn rejected. `stalled` is now terminal and the
  store reconciles orphans to it on boot (freeing the quota).
- **Cancelled delegations release their resources (D2, D5).** A cancelled
  `run_child` no longer orphans its shielded drain task (live ReasoningLoop +
  provider stream), and the supervisor slot guard is entered *before* the first
  post-acquire `await` so a cancel can't leak the per-parent / per-tenant
  counters.
- **`run_in_background` is no longer advertised (D4).** The end-to-end
  background path was never wired (the servicer never threads a dispatcher into
  the spawn path; the published factory raises), so the schema field only
  invited a mode that always rejects. Removed from the `subagent_spawn` schema;
  the defensive reject branch is retained for hand-crafted args.
- **`subagent_spawn_many` per-task schema relaxed (D11).** Only `goal` is
  required now (was `agent` + `goal`); a missing `agent` defaults to
  `general-purpose`, matching the dispatcher and single `subagent_spawn`.
  Strict providers no longer 400 an agent-less fan-out task.
- **Synthesis fallback honors the wall-clock budget (D10).** The forced
  final-answer round is now bounded by the child's *remaining* budget (capped at
  30s) instead of a fresh 30s on top of `max_wall_seconds`.

### Changed
- **Single-level subagent nesting is now the contract (D7).** `max_depth`
  defaults to `1` (was `2` but unreachable). A subagent cannot spawn a
  sub-subagent — matching Claude Code's Task tool and the executor's existing
  blanket recursive-spawn refusal. The runner prunes spawn tools from every
  child regardless of `max_depth`, so the advertised toolset matches the
  enforced one.
- **Wall-clock ceiling decoupled from the default budget (D9).** New
  `DEFAULT_MAX_WALL_SECONDS_CEILING = 300`; the 60s default and the 300s
  request ceiling are now distinct, so a child may legitimately request up to
  300s and the clamp actually engages.
- **Background dispatcher lifecycle + layering cleanups (D6, D8, D12).** Removed
  the dead `Supervisor._lock` (the cap read-modify-write is await-free, so no
  lock is needed) and the dead `child_emitter` (which imported the server
  package, creating a server→subagent→server cycle); guarded the supervisor's
  `corlinman_agent.events` imports so the low-level package degrades cleanly.
  Added `AsyncSubagentDispatcher.shutdown()` (cancel + await in-flight tasks,
  called from lifespan teardown) and dropped its dead `_snapshot` / `_asdict`
  helpers. Refreshed stale cap/ceiling docstrings (per-parent 3→10, ceiling
  300, depth) across the stack.

## [1.13.0] — 2026-05-30 — Shareable agent status card across every channel

> Chat replies on every channel can now carry a tap-through link to a public,
> read-only "what is the agent doing right now" page — a live trajectory view
> backed by a signed, self-expiring capability token. Off by default; lights
> up only when the operator sets `[server].public_url`.

### Added
- **Public agent status card.** New unauthenticated route
  `GET /status/{token}/data` (JSON snapshot: `session_key`, `status`, `turns`,
  `events`, `started_at_ms`, `updated_at_ms`) plus `GET /status/{token}/events/live`
  (SSE live feed, 10s heartbeat, `Last-Event-ID` resume). Mounted at root in
  `gateway/routes/register.py`; the signed token in the path **is** the
  capability (a tampered/expired token → `403`). The journal is read lazily
  from `app.state.corlinman_journal`. (#28, #31)
- **Status link in every channel reply.** All seven chat channels
  (Telegram, Discord, Slack, Feishu, QQ/OneBot, QQ Official, WeChat Official)
  append a `🔗 实时状态: {public_url}/status/{token}` line to the final reply
  when the feature is configured. The four spinner channels route through the
  shared `_build_footer_for_outcome`; QQ / QQ-Official / WeChat inject on their
  own reply paths. Dropped gracefully when it would overflow a channel's length
  cap.
- **Public status UI.** `ui/app/status/[token]/` — a static-export shell that
  reads the token from the URL at runtime, fetches the snapshot, subscribes to
  the SSE feed, and renders the same read-only `EventTimelineBody` the admin
  surface uses. (#29)
- **Trajectory redaction (privacy).** The public snapshot redacts tool-call
  args/results by default (tool *names* + status survive, payload bodies are
  stripped) so a shared link can't leak prompts / keys / file contents. Toggle
  via `CORLINMAN_STATUS_REDACT`. (#30)
- **Config.** `[server].public_url` and `[channels].status_url_in_replies`
  (default `true`) in `docs/config.example.toml`; the entrypoint arms the
  feature once at boot (`_wire_status_links`) before channels start, injecting a
  token-minting closure so `corlinman-channels` never imports `corlinman-server`
  (import-linter layering preserved). The `agent_status_card` tool falls back to
  the config `public_url` when `CORLINMAN_PUBLIC_URL` is unset. (#33)

## [1.12.3] — 2026-05-30 — Subagents run their tools; reliable PDF/document generation

> Two live-usage fixes found from a real multi-agent run: research subagents
> returned only their search *trajectory* (no synthesized answer), and a
> "summarize to PDF" task produced a letter-spaced garbage file.

### Fixed
- **Subagents now actually EXECUTE their tools and synthesize an answer.** A
  spawned child's `ReasoningLoop` emitted `web_search` (etc.) calls, but the
  runner only *recorded* them — it never ran the tool or fed the result back,
  so `_collect_results` timed out, the loop ended on the tool round, and the
  child returned `output_text=""` (only `tool_calls_made` was populated). The
  parent had to redo all the work. The child runner now takes a
  `tool_executor` (the gateway binds it to the parent's own builtin dispatcher
  under the parent's permission gate + workspace) and calls
  `loop.feed_tool_result(...)` exactly as the parent does, so the model
  receives results and writes a real final answer. Children may not recursively
  spawn (refused with a clean envelope). Added:
  - a **guaranteed-synthesis fallback** — if tools ran but no answer text was
    produced, one tools-disabled round turns the tool results into a final
    answer, so a delegation never comes back empty;
  - **`max_tool_calls` enforcement** (was documented but never applied) capping
    real tool execution as a cost guard;
  - a truthful **finish-reason mapping**: a child that ends on a tool-call
    round now maps to `LENGTH` (truncated), not a silent `STOP`.
- **Reliable PDF / document generation.** When asked to "总结成 PDF" the agent
  had no prescribed pipeline (no document skill existed) and improvised —
  headless-chrome with the wrong flags, then `reportlab` it couldn't install,
  then a hand-rolled raw-PDF script whose glyph advances were wrong (every
  character space-padded). Now ships:
  - a **`corlinman-md2pdf`** console script (Markdown → clean, CJK-correct PDF
    via headless Chrome with a proper font stack; self-contained Markdown
    converter, no fragile deps);
  - a **`document-generator` bundled skill** documenting the exact pipeline and
    the anti-patterns (never hand-roll PDF bytes, never `reportlab`, never bare
    `--headless`).

### Changed
- **Always-on skills reach the main agent.** Stage-3 skill injection only fired
  when a message invoked an agent card (`{{角色}}` token), so the *main* chat
  agent received no skills at all. `ContextAssembler` now also injects a
  configurable `default_skill_refs` on every turn (default:
  `document-generator`), merged/deduped with any invoked card's `skill_refs`.
  Missing-skill refs stay non-fatal.

## [1.12.2] — 2026-05-30 — Hotfix: subagent model inheritance + offline default + max-10 fanout

> Second prod hotfix on the subagent path. Two 400s and one capacity limit:
> a model-less spawn reached the provider with `model=""`, a fresh VPS couldn't
> resolve the `general-purpose` default, and the fan-out cap was stuck at 3.

### Fixed
- **`subagent_spawn_inline` → 400 `model is required`.** An ephemeral inline
  card binds no model, and — unlike top-level chats — the gateway does not
  rewrite an empty `model` for *child* `ChatStart`s, so a model-less spawn hit
  the provider with `model=""`. The child now **inherits the parent's resolved
  model** as the final fallback. New precedence in `run_child`:
  `model_override` > `agent_card.model` > `parent_model` > `""`. The parent's
  model is threaded from `ChatStart.model` through all three spawn dispatchers
  (`subagent_spawn` / `_many` / `_inline`).
- **`subagent_spawn` → `agent_not_found: 'general-purpose'` on a fresh host.**
  The servicer's `_get_agent_registry` loaded only `<DATA_DIR>/agents` (empty on
  a new VPS) instead of the gateway's three-tier (repo + user + project) stack,
  so the bundled `general-purpose` card was never seen. It now reuses
  `_build_agent_registry_stack`, **and** a new in-code `builtin_general_purpose`
  card (via `AgentCardRegistry.get_or_builtin_default`) backstops the default
  even when no card is on disk ("offline-first", Claude-Code parity). An
  explicit unknown `subagent_type` still rejects with `unknown_subagent_type`
  (typo protection preserved).

### Changed
- **Max parallel subagents raised 3 → 10** to match Claude Code's Task-tool
  fan-out. `SUBAGENT_SPAWN_MANY_MAX_TASKS` and
  `SupervisorPolicy.max_concurrent_per_parent` bumped in lock-step
  (per-tenant cap stays 15, depth stays 2).
- `subagent_spawn` / `subagent_spawn_many` now seed the child persona row
  (`persona_store` threaded through) like the rest of the agent path.

## [1.12.1] — 2026-05-30 — Hotfix: invalid subagent tool names

### Fixed
- **Chat 400 `Invalid 'tools[N].name'` on every request.** v1.12.0 advertised
  the subagent tools to the model, but they were named with dots
  (`subagent.spawn` / `subagent.spawn_many` / `subagent.spawn_inline`), which
  OpenAI-style providers reject (tool names must match `^[a-zA-Z0-9_-]+$`).
  Renamed to underscores (`subagent_spawn` / `subagent_spawn_many` /
  `subagent_spawn_inline`) across the constants, schemas, skills, and tests.
  Pre-v1.12.0 these tools were dispatch-only (never sent to the model), which
  is why the latent invalid name only surfaced once they were advertised.

## [1.12.0] — 2026-05-30 — Dynamic subagents + status-card foundation

> Brings Claude Code's dual-mode subagent dispatch to corlinman — the main
> agent can now call an existing registered agent **and** spin up a temporary,
> purpose-built one on the fly — and lays the (signed-token) foundation for a
> shareable "agent status card" link. Researched from Claude Code's own source.

### Added
- **`subagent.spawn_inline`** — a temporary / ad-hoc child agent built from an
  inline `system_prompt`, run once, **never registered** (ephemeral `AgentCard`,
  `source="inline"`). Reuses the existing runner / supervisor / blackboard via a
  shared `_run_child_under_slot` helper. `tools_allowed=["*"]` inherits the
  parent's tools, bounded by `tool_allowlist` ∩ parent (escalation rejected).
  See `docs/PLAN_DYNAMIC_SUBAGENTS.md`.
- **Existing-agent call surfaced** — `subagent.spawn` / `subagent.spawn_many`
  are now **advertised** to the main agent (they were dispatch-only, so the
  model never saw them). No logic change.
- **Agent status-card foundation** — a stateless, signed, expiring share token
  (`gateway/status_token.py`) scoping read-only access to one conversation, plus
  the `agent_status_card` builtin tool that mints a
  `<CORLINMAN_PUBLIC_URL>/status/<token>` link for the current session. The
  public route + UI page are tracked follow-ups (see
  `docs/PLAN_AGENT_STATUS_CARD.md`); the tool returns a clear `public_url_unset`
  envelope until an operator opts in.

### Fixed
- **Subagent supervisor caps were not enforced at the servicer.** The spawn
  dispatch calls omitted `supervisor_acquire` / `max_depth` /
  `max_wall_seconds_ceiling`, so depth + per-parent + per-tenant concurrency
  caps were effectively off at that entry point. All three spawn tools now share
  one in-process `Supervisor` (default depth 2, 3-per-parent, 15-per-tenant).

## [1.11.0] — 2026-05-30 — Persona life system + QZone comments

> Ports the "Grantley (格兰)" tooling from hermes-agent into corlinman and wires
> it into the **current persona system**: a persona now lives an ongoing life
> (missions, travels, a private diary) backed by the native runtime
> persona-state store and surfaced through the `{{persona.*}}` placeholder
> layer; the bot can read + comment on the QQ空间 timeline; and the `/persona`
> creation wizard can author a persona's life lore via online research or
> user-provided materials.

### Added
- **`persona_life_*` tools** — `persona_life_get` / `persona_life_set_state` /
  `persona_life_diary_add` / `persona_life_event_seed`. Persona-agnostic
  (keyed by the bound persona), persisted in the native `corlinman-persona`
  runtime-state store (`agent_state.sqlite`, `state_json`). `set_state` mirrors
  `mood`→native column, `activity`→`recent_topics`, and the salient fields→flat
  `state_json["life_*"]` keys so a system_prompt can interpolate
  `{{persona.life_location}}` / `{{persona.life_state}}` / … via
  `PersonaResolver`. The built-in `grantley` persona ships a bundled
  Knights-College event-seed pack.
- **`qzone_*` read + comment tools** — `qzone_list_feed` / `qzone_get_post` /
  `qzone_post_comment` / `qzone_list_friends`: read the 好友动态 timeline and
  comment on (or reply under) 说说. Async-httpx port reusing the
  `qzone_publish` auth path; added `OneBotClient.fetch_friend_list()`.
- **Persona-creation lore authoring** — new `persona_life_set_seeds` /
  `persona_life_get_seeds` tools, plus a new **"Stage 4b — 人生设定/事件种子"** in
  the `configure-persona` wizard offering an explicit choice between
  **agent auto-research online** and **filling from user-provided materials**
  (skippable). Stage 6 writes the authored seed library after `persona_create`.

### Fixed
- **`corlinman-persona` store missing WAL/busy_timeout.** `PersonaStore._open`
  now enables `journal_mode=WAL` + `synchronous=NORMAL` + `busy_timeout=5000`
  (matching every other corlinman sqlite store) so the EvolutionLoop / decay
  job / placeholder resolver / `persona_life_*` tools — each holding a separate
  handle to `agent_state.sqlite` — no longer race to "database is locked".

## [1.10.1] — 2026-05-29 — Channel subsystem completion

> Fixes the "Telegram/QQ channel pages cannot be accessed" report, completes
> the QQ (OneBot/NapCat) channel, bundles NapCat into the installer by default,
> and surfaces every built channel in the admin UI.

### Fixed
- **Channel admin pages returned 401.** The api-key middleware listed a bare
  `/channels/` in `protected_prefixes`, which matched the static UI page routes
  (`/channels/qq`, `/channels/telegram`) and rejected unauthenticated browser
  loads *before* the static mount — so only the channel pages were unreachable
  while every other admin page loaded. Narrowed the prefix to the one real
  bearer API there (`/channels/telegram/webhook`); the canonical
  `/v1/channels/...` stays gated by `/v1/`. Locked by
  `tests/gateway/lifecycle/test_ui_static_serving.py`.

### Added
- **QQ / OneBot channel completed** — inline image/emoji send, video/file
  inbound, direct `/help` `/whoami` `/status` command handlers, per-send
  health counters, and a real `POST /admin/channels/qq/reconnect` (was a 501
  stub).
- **NapCat bundled by default** — docker brings up the pinned
  `mlikiowa/napcat-docker:${NAPCAT_VERSION:-v4.18.4}` sidecar by default
  (`--without-qq` to opt out); native installs download a pinned NapCat
  AppImage and register a `corlinman-napcat.service` systemd unit.
- **All 7 channels surfaced in the admin UI** — added Discord, Slack, Feishu
  (full inbox: status + recent messages + test-send) and WeChat-Official,
  QQ-Official (config + status), each with a uniform
  `GET/POST /admin/channels/{name}/…` admin API, sidebar nav, en/zh i18n, and
  page tests. Added the previously-missing `channels/qq/page.test.tsx`.
- **Build-time route guard** — `ui/scripts/assert-routes-built.mjs` fails the
  UI build if any required route HTML is missing or identical to `404.html`,
  preventing a stale bundle from silently shipping pages-missing.

### Notes
- Known follow-ups: Slack `files.upload` wiring unaudited; QQ numeric health
  counters not yet surfaced in the QQ UI stats row; the native NapCat
  provisioning in `install.sh` is best-effort (a failed AppImage download warns
  but does not block the gateway upgrade) and has not been exercised on a host.

## [1.10.0] — 2026-05-29 — Audit rounds 4–9 + CI greening + durable voice sessions

> ~50 commits since v1.9.0 (`c53b19a`..`d953fe7`) across **Rounds 4
> through 9** of the audit loop, a dedicated **CI-greening pass**, and a
> **voice-store feature**. v1.9.0 was tagged before Round 4, so this
> release bundles all of it. The headline: the entire Python CI gate
> (**ruff + mypy + import-linter**) is **green for the first time** —
> and greening it was not cosmetic, it surfaced and fixed genuine latent
> bugs (dangling asyncio tasks, an exception-silencing `finally`, loop
> closures) that 1176 lint errors had buried. Other highlights: an
> **unauthenticated `/v1/voice` WebSocket** closed (and its token moved
> off the query string); **Anthropic/Bedrock multi-round + parallel tool
> calling** fixed; a deploy **privilege-escalation** hardening plus the
> **Critical native-install startup regression** it briefly introduced,
> now fixed; the scheduler's default cron jobs finally **spawn and fire**;
> a real **placeholder engine**; journal/identity **transaction
> serialization**; provider client-leak / 429 / retry fixes; a web-fetch
> **DNS-rebind** close; admin-provider **SSRF** guard; **agent-brain
> secret-blocking**; an MCP **cross-tenant IDOR** fix; and a durable
> **SQLite voice session store**. ~190 new tests; the full uv-workspace
> suite now runs **4553 passed / 4 skipped** (from the 4363 baseline),
> with **0 regressions** across the whole arc. **No data migration
> required. Operators on 1.9.x should upgrade** (see Upgrade notes for
> the few behavior changes). Full audit trail in `audit/` (ISSUES.md,
> PROGRESS.md, FINAL_REPORT.md, evidence/{round-4..9}/).
>
> 自 v1.9.0 起约 50 个提交（`c53b19a`..`d953fe7`），覆盖审计循环的
> **第 4 至第 9 轮**、一次专门的 **CI 转绿** 以及一个**语音存储功能**。
> v1.9.0 在第 4 轮之前打的标签，所以本次发布把这些全部打包。重点：
> 整条 Python CI 门禁（**ruff + mypy + import-linter**）**首次全绿**——
> 而且转绿不是表面功夫，它暴露并修复了被 1176 条 lint 错误掩盖的真实
> 潜伏 bug（游离的 asyncio 任务、吞掉异常的 `finally`、循环闭包）。
> 其它重点：关闭了**未鉴权的 `/v1/voice` WebSocket**（令牌移出查询串）；
> 修复 **Anthropic/Bedrock 多轮 + 并行工具调用**；部署侧**提权**加固，
> 以及它一度引入、现已修复的 **Critical 原生安装启动回归**；调度器的
> 默认 cron 任务终于会**被拉起并触发**；真正的**占位符引擎**；日志 /
> 身份的**事务串行化**；provider 客户端泄漏 / 429 / 重试修复；web-fetch
> **DNS rebind** 关闭；admin-provider **SSRF** 防护；**agent-brain 密钥
> 拦截**；MCP **跨租户 IDOR** 修复；以及一个持久化的 **SQLite 语音会话
> 存储**。新增约 190 个测试；完整 uv-workspace 测试套件现为 **4553 通过
> / 4 跳过**（基线 4363），整段过程 **0 回归**。**无需数据迁移，1.9.x
> 的运维者应当升级**（少数行为变更见「升级须知」）。完整审计记录见
> `audit/`（ISSUES.md、PROGRESS.md、FINAL_REPORT.md、evidence/{round-4..9}/）。

### Security / 安全

- **(critical) Unauthenticated `/v1/voice` WebSocket closed.** The
  api-key gate is a `BaseHTTPMiddleware`, which never sees the WebSocket
  ASGI scope — so the realtime voice handshake at `/v1/voice` opened a
  provider session (and billed audio) for any unauthenticated caller,
  with the tenant taken from a spoofable `X-Tenant-Id` header. The
  handshake now authenticates inline (reusing `verify_api_key`), closes
  with `4401` before any provider session opens on a missing/invalid
  key, and binds the tenant to the verified key — not a header. Retained
  audio paths now sanitize `tenant_id`/`session_id` against `../`
  traversal. (#R5-S1 #R5-S2)
  — **（critical）关闭未鉴权的 `/v1/voice` WebSocket。** api-key 门禁是
  `BaseHTTPMiddleware`，看不到 WebSocket 的 ASGI scope——于是 `/v1/voice`
  的实时语音握手会为任意未鉴权调用方打开 provider 会话（并产生音频计费），
  且租户来自可伪造的 `X-Tenant-Id` 头。握手现在内联鉴权（复用
  `verify_api_key`），令牌缺失/无效时在打开任何 provider 会话之前以
  `4401` 关闭，并把租户绑定到已验证的密钥而非请求头；保留音频路径会对
  `tenant_id`/`session_id` 做 `../` 穿越清洗。
- **(high, regression-sec) Voice WS token no longer travels on the query
  string.** R5-S1's fix accepted the token via `?api_key=…`, which leaks
  the key into gateway/proxy access logs. The token is now carried via
  the `Sec-WebSocket-Protocol` subprotocol
  (`corlinman.voice.token.<token>`) or an `Authorization` / `X-API-Key`
  header; the query-string fallback was removed. (#R6-REG3)
  — **（high，回归-安全）语音 WS 令牌不再走查询串。** R5-S1 的修复曾经接受
  `?api_key=…`，会把密钥泄漏进 gateway/代理的访问日志。令牌现在通过
  `Sec-WebSocket-Protocol` 子协议（`corlinman.voice.token.<token>`）或
  `Authorization` / `X-API-Key` 头传递；查询串回退已移除。
- **(high) OAuth callback-state validation enforced on all 4 PKCE
  flows.** The xai / codex / gemini / anthropic OAuth submit handlers
  validated the returned `state` only conditionally (or not at all),
  leaving a CSRF window on the credential-binding step. All four now
  require + constant-time-compare the state (reject-present-mismatch for
  the anthropic bare-code fallback). A test that had encoded the bug was
  corrected. (#R4-D1)
  — **（high）四条 PKCE 流程全部强制校验 OAuth callback state。** xai /
  codex / gemini / anthropic 的 OAuth 提交处理器此前只有条件性（或根本不）
  校验返回的 `state`，凭据绑定步骤存在 CSRF 窗口。四者现在都强制校验并
  常量时间比较（anthropic 裸 code 回退采用「出现即不许不匹配」）；一个把
  bug 编进断言的测试已被纠正。
- **(high) agent-brain blocks secrets before they reach the vault.** The
  memory curator auto-wrote candidate memories straight into the vault,
  including ones bearing API keys / tokens. It now classifies risk and
  marks secret-bearing candidates `BLOCKED`, enforcing
  `auto_write_max_risk` before any write. (#R6-SEC-brain)
  — **（high）agent-brain 在写入 vault 前拦截密钥。** 记忆策展器此前会把
  候选记忆（包括携带 API key / token 的）直接写进 vault；现在会做风险分级，
  把携密候选标记为 `BLOCKED`，并在任何写入前强制执行 `auto_write_max_risk`。
- **(high) MCP per-token tenant scoping (cross-tenant IDOR).** MCP
  resource `list` / `read` did not scope to the calling token's tenant —
  any token could enumerate and read another tenant's memory resources.
  Now scoped per-token. Latent until `/mcp` is bound, fixed pre-emptively.
  (#R6-SEC-mcp)
  — **（high）MCP 按令牌做租户隔离（跨租户 IDOR）。** MCP 资源 `list` /
  `read` 此前不按调用令牌的租户限定——任意令牌都能枚举并读取其他租户的
  记忆资源。现已按令牌隔离。该问题在 `/mcp` 绑定前为潜伏态，已提前修复。
- **(medium) web-fetch DNS-rebind TOCTOU closed.** The agent's web-fetch
  SSRF guard resolved DNS, validated the IP, then let httpx re-resolve on
  connect — a rebind could swap in a private/metadata IP between the two.
  The validated IP is now pinned for the actual connection. (#R7-SEC012)
  — **（medium）关闭 web-fetch 的 DNS rebind TOCTOU。** agent web-fetch 的
  SSRF 防护此前先解析 DNS、校验 IP，再让 httpx 在连接时重新解析——两步之间
  rebind 可换入内网/元数据 IP。现在把已校验的 IP 钉定到实际连接上。
- **(medium) admin-provider probe SSRF guard.** The admin provider-probe
  now blocks cloud-metadata and link-local targets while still allowing
  loopback/private hosts (so self-hosted Ollama/vLLM relays keep working).
  A first, blanket `is_safe_host` attempt was dropped because it broke
  those relays; this is the surgical replacement. (#R7-SEC008)
  — **（medium）admin-provider 探测 SSRF 防护。** admin provider 探测现在
  屏蔽云元数据与链路本地地址，同时仍放行回环/私网主机（自托管 Ollama/vLLM
  中继照常可用）。第一版「一刀切」的 `is_safe_host` 因会打断这些中继而被
  撤回，这是精修后的替代实现。
- **(low) Constant-time admin username compare + conditional Secure
  cookie.** Admin login now compares the username via
  `hmac.compare_digest` with an always-run argon2 verify (kills the
  timing oracle that distinguished valid vs invalid usernames); the
  session cookie sets `Secure` when served over https. (#R9-SEC011 #R9-SEC009)
  — **（low）admin 用户名常量时间比较 + 条件性 Secure cookie。** admin 登录
  现以 `hmac.compare_digest` 比较用户名并始终运行一次 argon2 校验（消除区分
  有效/无效用户名的时序侧信道）；会话 cookie 在 https 下设置 `Secure`。

### Fixed / 修复

- **(critical, regression) Native gateway runs as the unprivileged
  service user again.** R5-S3 hardened the deploy but left the systemd
  `ExecStart` pointing at root's `uv` with no `HOME`, so the gateway
  would not start on a native systemd box (which is how prod runs). The
  unit now invokes the venv console-script directly, sets `HOME`, and
  fixes `.venv` ownership to `root:corlinman`; the upgrade path keeps the
  ownership invariant. (#R6-REG1)
  — **（critical，回归）原生 gateway 重新以非特权服务用户运行。** R5-S3
  加固了部署，却把 systemd `ExecStart` 指向了 root 的 `uv` 且无 `HOME`，
  导致 gateway 在原生 systemd 机器上无法启动（生产正是这种部署）。该 unit
  现在直接调用 venv 控制台脚本、设置 `HOME`，并把 `.venv` 所有权修正为
  `root:corlinman`；升级路径保持所有权不变式。
- **(high) Anthropic/Bedrock emit `tool_use`/`tool_result` on multi-round
  tool input.** Both adapters dropped `tool_calls` when rebuilding the
  request after the first tool round, so every post-first-tool turn
  failed. They now emit the correct vendor blocks. (#R5-B1)
  — **（high）Anthropic/Bedrock 在多轮工具输入时发出 `tool_use`/`tool_result`。**
  两个适配器此前在第一轮工具之后重建请求时丢掉了 `tool_calls`，导致首轮
  工具之后的每一轮都失败；现在会发出正确的厂商块。
- **(high, regression) Parallel tool results coalesced into one Anthropic
  user turn.** R5-B1 fixed single-tool rounds but broke *parallel* tool
  rounds — multiple `tool_result` blocks were split across turns, which
  Anthropic/Bedrock reject. They now coalesce into a single user turn.
  (#R6-REG2)
  — **（high，回归）并行工具结果合并进一个 Anthropic user turn。** R5-B1
  修好了单工具轮次却弄坏了**并行**工具轮次——多个 `tool_result` 块被拆到
  不同轮次，Anthropic/Bedrock 会拒绝。现在它们会合并进单个 user turn。
- **(critical) Scheduler runtime is spawned in the lifespan so default
  cron jobs actually fire.** `scheduler.runner.spawn()` had **zero
  production callers** — v1.9.0's dispatch-routing fix was necessary but
  not sufficient: the tick loops were never created, so the default jobs
  (`system.update_check`, `evolution.darwin_curate`) never ran. The
  runtime is now spawned in the lifespan with `app_state` threaded
  through, and `SchedulerHandle.trigger()` is completed. Real-run
  verified: a booted gateway spawned 3 tick tasks and fired a per-second
  job 3× in 2.6s. (#R4-F1)
  — **（critical）调度器运行时在 lifespan 中拉起，默认 cron 任务真正触发。**
  `scheduler.runner.spawn()` 此前**没有任何生产调用方**——v1.9.0 的 dispatch
  路由修复必要但不充分：tick 循环从未被创建，默认任务（`system.update_check`、
  `evolution.darwin_curate`）从未运行。运行时现在在 lifespan 中拉起并贯穿
  `app_state`，`SchedulerHandle.trigger()` 也补全。实跑验证：启动后的 gateway
  拉起 3 个 tick 任务，一个每秒任务在 2.6 秒内触发 3 次。
- **Real `PlaceholderEngine` ported + `{{episodes.*}}` resolver wired.**
  `{{memory.*}}` / `{{episodes.*}}` placeholders had been bound to a
  `_NullEngine`, so the tokens echoed unresolved. The real engine
  (depth/cycle/dispatch parity) is now ported and `build_default_engine`
  registers an `EpisodesResolver`; `{{episodes.recent}}` resolves against
  the episodes DB. `{{memory.*}}` auto-activates when a `MemoryHost` is
  published. (#R4-F2)
  — **移植真正的 `PlaceholderEngine` + 接线 `{{episodes.*}}` 解析器。**
  `{{memory.*}}` / `{{episodes.*}}` 占位符此前绑定到 `_NullEngine`，令牌原样
  回显。现在移植了真引擎（深度/环/分发对齐），`build_default_engine` 注册
  `EpisodesResolver`；`{{episodes.recent}}` 会对接 episodes DB 解析。
  `{{memory.*}}` 在发布 `MemoryHost` 后自动激活。
- **persona/user/goals placeholder resolver seam added.** A resolver
  adapter + seam for `{{persona.*}}` / `{{user.*}}` / `{{goals.*}}` is now
  wired into `build_default_engine`; the entrypoint id-stamping plumbing
  is spec'd in `ARCH_DEBT.md`. (#R6-G8)
  — **新增 persona/user/goals 占位符解析器接缝。** `{{persona.*}}` /
  `{{user.*}}` / `{{goals.*}}` 的解析适配器与接缝已接入 `build_default_engine`；
  entrypoint 的 id 标注管线在 `ARCH_DEBT.md` 中给出规格。
- **(high) Journal writes serialized on the shared SQLite connection.**
  A bare `commit()` on the shared connection could flush another session's
  open `BEGIN IMMEDIATE` transaction, corrupting atomicity.
  `SqliteJournalBackend` writes are now serialized (non-reentrant-safe).
  (#R5-B3)
  — **（high）日志写入在共享 SQLite 连接上串行化。** 共享连接上的裸
  `commit()` 可能 flush 掉另一会话已打开的 `BEGIN IMMEDIATE` 事务，破坏原子性。
  `SqliteJournalBackend` 的写入现已串行化（非重入安全）。
- **(high) Identity store holds `tx_lock` on single-statement writes.**
  A recurrence of the R5-B3 class in the identity store
  (`_issue_phrase` / `_sweep`) could leave orphan rows under async
  interleave; both now hold `tx_lock`. (#R6-CONC)
  — **（high）身份存储在单语句写入时持有 `tx_lock`。** 身份存储
  （`_issue_phrase` / `_sweep`）中 R5-B3 同类问题的复发，在异步交错下可能
  留下孤儿行；两者现在都持有 `tx_lock`。
- **(high) `list_session_summaries` correlated-subquery → window + turn_id
  fallthrough fixed.** The session-summary query scanned
  O(sessions × turns × msgs) via a correlated subquery, and `begin_turn`
  could fabricate a colliding `turn_id` after 20 collisions. Rewritten as
  a window function with a collision-free insert. (#R7-PERF006 #R7-BUG010)
  — **（high）`list_session_summaries` 关联子查询改窗口函数 + turn_id 兜底
  修复。** 会话摘要查询此前用关联子查询扫描 O(会话 × 轮次 × 消息)，且
  `begin_turn` 在 20 次碰撞后可能伪造一个碰撞的 `turn_id`。重写为窗口函数
  + 无碰撞插入。
- **`list_session_summaries` previews no longer mix same-millisecond
  turns.** Tie-broke the summary subqueries on `turn_id DESC` so previews
  from two turns landing on the same millisecond don't interleave columns.
  (#R4-D6)
  — **`list_session_summaries` 预览不再混淆同毫秒轮次。** 摘要子查询按
  `turn_id DESC` 做平局判定，使落在同一毫秒的两个轮次的预览不再串列。
- **HookEvent `turn_id` preserved across all branches** — carried over
  from the v1.9.0 batch context for completeness of the journal-correlation
  fix (`agent_servicer.py`). (#R1-002 follow-through)
  — **HookEvent `turn_id` 在所有分支中保留**——延续日志关联修复的上下文
  （`agent_servicer.py`）。
- **Provider client lifecycle / 429 / declarative-auth fixes.**
  - The codex path constructed an `AsyncOpenAI` client per `chat_stream`
    and never closed it; now closed on every success/error/cancel path
    (the R1-003 leak fix had missed codex). (#R4-D2)
    — codex 路径此前每次 `chat_stream` 都新建 `AsyncOpenAI` 客户端且从不关闭；
    现在在每条 成功/错误/取消 路径上关闭（R1-003 的泄漏修复漏掉了 codex）。
  - 429 `Retry-After` is now extracted into `RateLimitError.retry_after_ms`
    on both the OpenAI and Anthropic mappers (was always `None`). (#R4-D3)
    — 429 的 `Retry-After` 现在在 OpenAI 与 Anthropic 两个映射器上提取进
    `RateLimitError.retry_after_ms`（此前恒为 `None`）。
  - Anthropic OAuth credential reads are now mtime-cached (was a sync
    read+parse per request). (#R4-D4)
    — Anthropic OAuth 凭据读取现在按 mtime 缓存（此前每请求同步读+解析一次）。
  - Late-streamed OpenAI tool-call ids are promoted via `_ToolCallState`
    (BUG-006); declarative `auth_kind="header"` is honored for the
    openai/anthropic/gemini wire formats, and `auth_kind="query_param"`
    now raises an explicit "not yet supported" error instead of silently
    sending a bearer token. (#R7-B1 #R7-B2)
    — 晚到的流式 OpenAI tool-call id 经 `_ToolCallState` 提升（BUG-006）；
    声明式 `auth_kind="header"` 在 openai/anthropic/gemini 三种线格式下被遵守，
    而 `auth_kind="query_param"` 现在显式抛出「尚不支持」错误，而不再静默地
    发送 bearer 令牌。
  - GoogleProvider sends real multimodal parts instead of a list `repr`.
    (#R6-BUG-google)
    — GoogleProvider 发送真实的多模态 parts，而非列表的 `repr`。
- **(high) `wstool` malformed frame no longer crashes the reader / leaks
  the runner.** `from_dict` raised `TypeError` (instead of `ValueError`)
  on a malformed frame, crashing the reader and leaking the runner; the
  reader is now cleaned up in a `finally`. (#R6-BUG-wstool)
  — **（high）`wstool` 畸形帧不再使 reader 崩溃 / 泄漏 runner。**
  `from_dict` 在畸形帧上抛 `TypeError`（而非 `ValueError`），导致 reader
  崩溃并泄漏 runner；reader 现在在 `finally` 中清理。
- **auto-resume stops reporting false-positive 'resumed' for undrained
  channels.** (#R7-AR)
  — **auto-resume 不再为未排空的通道误报 'resumed'。**
- **UI GATEWAY_BASE_URL prefix on session-cost + upgrade-SSE fetchers.**
  Both fetchers omitted the configured `GATEWAY_BASE_URL`, so they hit the
  wrong origin behind a sub-path deployment. (#R6-BUG-ui)
  — **UI 在 session-cost 与 upgrade-SSE 取数器上补 GATEWAY_BASE_URL 前缀。**
  两个取数器此前漏掉了配置的 `GATEWAY_BASE_URL`，在子路径部署下会打到错误的 origin。
- **onboard image-provider `reuse` awaits the async probe** (was always
  returning 409). **chat model-picker open handler wired** (the picker was
  unreachable). **edit-and-rerun sends truncated history** instead of a
  stale closure. (#R5-C1 #R5-C2 #R5-B2)
  — **onboard 的图像 provider `reuse` 现在 await 异步探测**（此前恒返回 409）；
  **接线了 chat 模型选择器的打开处理器**（此前不可达）；**编辑并重跑发送截断
  后的历史**而非陈旧闭包。

### Performance / 性能

- **(high) `O(K²)→O(K)` streaming tool-call arg assembler.** The
  direct-backend assembled streamed tool-call argument fragments via
  repeated string `+=`; switched to list-append + `join` (~2s → ~6ms for
  4k fragments). (#R5-P1)
  — **（high）流式 tool-call 参数装配从 `O(K²)` 降到 `O(K)`。** direct-backend
  此前用反复的字符串 `+=` 装配流式 tool-call 参数片段；改为 list 追加 +
  `join`（4k 片段下约 2s → 约 6ms）。
- **(high) Batch streaming-delta journal writes off the hot path.** The
  observability emitter committed to SQLite per streamed token; writes are
  now batched off the hot path. (#R6-PERF)
  — **（high）把流式 delta 的日志写入批处理、移出热路径。** 可观测性 emitter
  此前每个流式 token 都向 SQLite commit；写入现已批处理并移出热路径。
- **(high) Bound `PersistentSubagentStore` terminal retention.** Terminal
  subagent records grew unbounded (memory/disk + O(N) write-amplification);
  capped at 512. (#R5-P3)
  — **（high）限制 `PersistentSubagentStore` 终态保留量。** 终态子代理记录
  此前无界增长（内存/磁盘 + O(N) 写放大）；上限设为 512。
- **(high) `React.memo` on `MessageBubble`.** Streaming deltas re-parsed
  every settled markdown bubble on each event; memoizing stops the
  re-parse storm. (#R4-D5)
  — **（high）给 `MessageBubble` 加 `React.memo`。** 流式 delta 此前在每个
  事件上重解析所有已定型的 markdown 气泡；memo 后止住了重解析风暴。
- **(high) Memory-host conversational recall bounded by SQL `LIMIT`.** Was
  a full-namespace scan; now `O(limit)`. (#R7-P1)
  — **（high）记忆主机的会话召回由 SQL `LIMIT` 限定。** 此前为整命名空间扫描；
  现在为 `O(limit)`。
- **(medium) Episode inserts batched into one transaction/commit.**
  (#R7-PERF008)
  — **（medium）episode 插入合并进单个事务/commit。**
- **(medium) Chat-UI: selective `reduceEvent` clone + no all-provider probe
  fan-out.** The reducer deep-cloned the whole pending message per event,
  and the model-picker fanned out N parallel provider probes on open; both
  trimmed. (#R7-PERF010 #R7-PERF012)
  — **（medium）Chat-UI：`reduceEvent` 选择性克隆 + 取消全 provider 探测扇出。**
  reducer 此前每事件深拷贝整条待定消息，模型选择器打开时并行扇出 N 个 provider
  探测；两者均已收敛。

### CI & Quality / CI 与质量

- **(high) py-ruff greened: 1176 → 0.** Safe + reviewed-unsafe autofix
  (~700 fixes across ~300 files) + config-align to the codebase's real
  conventions (dropped the never-enforced `N`/`SIM` families; ignored
  CJK-unicode / `E402` / `A002` / `A004` / FastAPI-`Depends`-`B008` /
  `B017` / StrEnum-`UP042` / `UP046`-`047`; excluded `audit/`) — **and
  fixed the real bugs the noise hid**: 3 dangling asyncio tasks (RUF006),
  a `return`-in-`finally` that silenced exceptions (B012), 2 loop closures
  (B023), 2 dataclass-default calls (RUF009), a stray import (F401).
  (#R8-ruff)
  — **（high）py-ruff 转绿：1176 → 0。** 安全 + 复核过的非安全 autofix
  （约 300 文件约 700 处）+ 配置对齐到代码库真实约定（移除从未强制的
  `N`/`SIM` 家族；忽略 CJK-unicode / `E402` / `A002` / `A004` /
  FastAPI-`Depends`-`B008` / `B017` / StrEnum-`UP042` / `UP046`-`047`；
  排除 `audit/`）——**并修复了被噪声掩盖的真实 bug**：3 个游离 asyncio
  任务（RUF006）、1 个吞异常的 `return`-in-`finally`（B012）、2 个循环闭包
  （B023）、2 个 dataclass 默认值调用（RUF009）、1 个多余导入（F401）。
- **(high) py-mypy greened: 166 → 0** (471 files Success). Per-package
  root-cause fixes (no-any-return narrowing, None-guards,
  `RequestResponseEndpoint`/`HTTPConnection`/`Scope` annotations,
  `functools.partial` loop-var binding); the net `type: ignore` count
  *dropped* (~10 total, each with a `[code]` + reason on a genuine stub
  gap or intentional runtime monkey-patch). (#R8-mypy)
  — **（high）py-mypy 转绿：166 → 0**（471 文件 Success）。逐包根因修复
  （no-any-return 收窄、None 守卫、`RequestResponseEndpoint`/`HTTPConnection`/
  `Scope` 注解、`functools.partial` 循环变量绑定）；净 `type: ignore` 数量
  *下降*（共约 10 个，每个都带 `[code]` + 理由，对应真实 stub 缺口或刻意的
  运行时 monkey-patch）。
- **(high) import-linter layering guard re-enabled + now gating.** A
  phantom `corlinman_embedding` root package had been aborting the whole
  layering contract (silently disabling the guard); removing it re-enabled
  the contract, which caught 3 real `agent→server` upward imports
  (grandfathered + filed). `boundary-check` is now part of the `gate`
  needs. (#R5-Q1)
  — **（high）重新启用 import-linter 分层守卫并纳入门禁。** 一个幻影
  `corlinman_embedding` 根包此前会让整条分层契约 abort（静默禁用守卫）；
  移除后契约重新生效，捕获了 3 处真实的 `agent→server` 向上导入（已祖父化
  并归档）。`boundary-check` 现已纳入 `gate` 的 needs。
- The previously-lying `docs/ci-status.md` was corrected to match reality.
  Dead code removed (`session_query.py`, a Rust-era `sessions.sqlite`
  reader). (#R5-Q1 #R7-QUAL007)
  — 此前失实的 `docs/ci-status.md` 已更正为实情；删除死代码
  （`session_query.py`，Rust 时代的 `sessions.sqlite` 读取器）。

### Features / 功能

- **Durable SQLite voice session store.** New `SqliteVoiceSessionStore`
  persists voice sessions across restarts (the session half of the voice
  persistence work). R5-B3-concurrency-safe (dedicated connection + lock),
  opened-once-and-cached (no per-connect leak), real-run verified via the
  live `/v1/voice` route. The transcript→chat bridge stays deferred (it
  needs a merge-semantics design decision). (#R9-voice-store)
  — **持久化 SQLite 语音会话存储。** 新增 `SqliteVoiceSessionStore`，跨重启
  持久化语音会话（语音持久化工作中的会话部分）。R5-B3 并发安全（专用连接
  + 锁）、一次性打开并缓存（无每次连接的泄漏），并经实时 `/v1/voice` 路由
  实跑验证。转写→聊天的桥接仍延后（需要合并语义的设计决策）。

### Tests / 测试

- **~190 new tests; full uv-workspace suite 4553 passed / 4 skipped**
  (from the 4363 baseline; 0 regressions across the arc). New coverage
  includes: 31 production-route tests (canvas / channels / memory /
  wechat_webhook / plugin_callback) + a memory namespace-index guard
  (#R9-TEST007); MCP `token_config_to_acl` + server-build (#R6-TEST-mcp);
  `home_channel_store` + admin-session TTL/gc (#R6-TEST-stores); voice
  money/quota — `cost.py` + `budget.py` (#R6-TEST-voice); plus the
  per-fix regression tests above (voice-WS 4401, Anthropic parallel
  tools, scheduler spawn, journal serialization, secret-block, etc.).
  — **新增约 190 个测试；完整 uv-workspace 套件 4553 通过 / 4 跳过**
  （基线 4363；整段 0 回归）。新增覆盖包括：31 个生产路由测试（canvas /
  channels / memory / wechat_webhook / plugin_callback）+ 记忆命名空间索引
  守卫（#R9-TEST007）；MCP `token_config_to_acl` + server-build
  （#R6-TEST-mcp）；`home_channel_store` + admin-session TTL/gc
  （#R6-TEST-stores）；语音计费/配额——`cost.py` + `budget.py`
  （#R6-TEST-voice）；以及上述各项修复的回归测试（语音 WS 4401、Anthropic
  并行工具、调度器拉起、日志串行化、密钥拦截等）。

### Docs / 文档

- Reality-aligned docs (the audit's "align the risky features, don't build
  them blind" stance): `run_in_background` marked not-yet-implemented
  (#R4-F3); evolution apply/rollback, goals, identity-ingest, and voice
  persistence aligned to reality + spec'd in `ARCH_DEBT.md` (#R6-C-align,
  #R5-C4); the `/nodes` page shows an honest "not available" panel instead
  of a silently-empty mock table (#R5-C3).
  — 与实情对齐的文档（审计「对齐有风险的功能、不盲目实现」的立场）：
  `run_in_background` 标记为尚未实现（#R4-F3）；evolution apply/rollback、
  goals、identity-ingest、语音持久化均对齐实情并在 `ARCH_DEBT.md` 给出规格
  （#R6-C-align、#R5-C4）；`/nodes` 页面显示诚实的「不可用」面板，而非静默
  空白的 mock 表（#R5-C3）。

### Upgrade notes / 升级须知

**No data migration required. Operators on 1.9.x should upgrade.** A few
behavior changes operators should know:
**无需数据迁移，1.9.x 的运维者应当升级。** 运维者需要知道的少数行为变更：

1. **Native systemd installs auto-migrate on upgrade.** The gateway now
   runs as an unprivileged `corlinman` user (was root) via the venv
   console-script. `install.sh --upgrade` and the one-click updater
   regenerate + reload the systemd unit automatically — **no manual
   action**. If you customized the unit, move your overrides into a
   systemd drop-in (`/etc/systemd/system/corlinman.service.d/*.conf`) so
   the regenerated unit doesn't clobber them.
   — **原生 systemd 安装在升级时自动迁移。** gateway 现在通过 venv 控制台
   脚本以非特权 `corlinman` 用户（此前为 root）运行。`install.sh --upgrade`
   与一键更新器会**自动**重生并重载 systemd unit——**无需人工操作**。若你
   定制过该 unit，请把覆盖项放进 systemd drop-in
   （`/etc/systemd/system/corlinman.service.d/*.conf`），以免重生的 unit
   覆盖它们。
2. **Voice WebSocket clients must move the token off the query string.**
   The `/v1/voice` token must now be sent via the `Sec-WebSocket-Protocol`
   subprotocol (`corlinman.voice.token.<token>`) or an `Authorization` /
   `X-API-Key` header. The `?api_key=` query-string fallback was removed
   (it leaked the key into access logs). Browser clients:
   `new WebSocket(url, ["corlinman.voice.v1", "corlinman.voice.token." + token])`.
   — **语音 WebSocket 客户端必须把令牌移出查询串。** `/v1/voice` 令牌现在
   必须通过 `Sec-WebSocket-Protocol` 子协议
   （`corlinman.voice.token.<token>`）或 `Authorization` / `X-API-Key` 头
   发送。`?api_key=` 查询串回退已移除（它会把密钥泄漏进访问日志）。浏览器
   客户端：`new WebSocket(url, ["corlinman.voice.v1", "corlinman.voice.token." + token])`。
3. **Custom declarative providers — auth_kind behavior tightened.**
   `auth_kind="header"` now correctly sends the key in the declared header
   (openai/anthropic/gemini wire formats). `auth_kind="query_param"` now
   raises an explicit "not yet supported" error instead of silently sending
   a bearer token — if you relied on the old (broken) bearer behavior,
   switch to `header`.
   — **自定义声明式 provider —— auth_kind 行为收紧。**
   `auth_kind="header"` 现在会把密钥正确地放进声明的请求头
   （openai/anthropic/gemini 线格式）。`auth_kind="query_param"` 现在显式抛出
   「尚不支持」错误，而不再静默发送 bearer 令牌——若你依赖旧的（错误的）
   bearer 行为，请改用 `header`。
4. **agent-brain memory curator now blocks secret-bearing candidates.**
   Candidate memories that carry API keys / tokens are marked `BLOCKED` and
   no longer auto-written to the vault. If you depended on secrets landing
   in memory, that path is intentionally closed.
   — **agent-brain 记忆策展器现在拦截携密候选。** 携带 API key / token 的
   候选记忆会被标记为 `BLOCKED`，不再自动写入 vault。若你曾依赖密钥落入
   记忆，该路径已被有意关闭。
5. **CI: the required `gate` check is now green.** The `N`/`SIM` ruff
   families were dropped and several false-positive rules ignored (see
   `pyproject.toml`); import-linter is re-enabled and gating. Contributors'
   local `ruff` / `mypy` should now pass clean against the aligned config.
   — **CI：必需的 `gate` 检查现已全绿。** 移除了 `N`/`SIM` ruff 家族并忽略了
   若干误报规则（见 `pyproject.toml`）；import-linter 已重新启用并纳入门禁。
   贡献者本地的 `ruff` / `mypy` 现在应当能在对齐后的配置下干净通过。

## [1.9.0] — 2026-05-29 — Security batch + reliability sweep

> Twenty-two commits since v1.8.13, produced by a four-round audit loop
> (3 perpetual rounds + an on-demand cleanup pass). **Five critical**
> issues closed including two RCE-class authorization gaps, one
> supply-chain hijack window, one scheduler default-job failure, and
> two zero-coverage critical-path test holes. Twelve additional high-
> severity bugs / sec gaps closed. No API or behavior breakage.
> Operators on 1.8.x should upgrade.
>
> Full audit trail in `audit/` (ISSUES.md, PROGRESS.md, FINAL_REPORT.md,
> evidence/{round-1,2,3,cleanup}/).

### Security

- **(critical) Unauthenticated /v1/* RCE closed.** `install_api_key_middleware`
  was defined in `gateway/middleware/auth.py` but never installed — the
  boot path probed for an `install` symbol that didn't exist, so the
  middleware was silently skipped and every `/v1/chat/completions`
  request was accepted without auth. Combined with the agent servicer
  auto-injecting `run_shell` into the default builtin tool set, an
  unauthenticated POST that nudged the model into shell execution was
  unauth RCE on the default `0.0.0.0:8080` bind. Now installed at
  `build_app` time with the real `AdminDb` rebound during lifespan;
  fail-closed with `401 admin_db_not_configured` when the DB can't
  open. `/health` is left unauthenticated. (#R1-001)
- **(critical) /canvas, /memory, /channels, /plugin-callback gated.**
  R1-001 closed `/v1/*` but the route registry mounts the same routes
  under legacy aliases that the api-key middleware never saw. Unauth
  callers could wipe memory docs, render canvas, subscribe to canvas
  SSE streams (exfil rendered LLM output), and poison parked agent
  loops via `/plugin-callback/{task_id}` (fake tool results). The
  api-key gate now covers all four prefixes alongside `/v1/`. The
  WeChat vendor webhook keeps its own signature-based auth and stays
  outside the bearer gate. (#R2-001)
- **(critical) Supply-chain hijack window closed.** `deploy/install.sh`
  and `.github/workflows/release-image.yml` still referenced the
  `ymylive/corlinman` namespace (repo was transferred to `sweetcornna`;
  GitHub redirect masked the divergence). Anyone re-registering
  `ymylive` on github.com or claiming it on ghcr.io would have controlled
  the install one-liner and the release Docker image for every fresh
  deploy. Retargeted at `sweetcornna/corlinman` across install, release
  workflow, upgrader, and docs. Operator follow-up to occupy the
  abandoned namespace is tracked in `audit/ARCH_DEBT.md`. (#R3-003)
- **(critical) gRPC agent refuses public bind.** `resolve_agent_bind`
  returned env-supplied bind values verbatim with no host validation —
  an operator setting `CORLINMAN_PY_ADDR=0.0.0.0:50051` exposed the
  unauthenticated Agent gRPC surface (and its `run_shell` / `write_file` /
  `apply_patch` tools) to anyone on the network. Now refuses non-loopback
  binds unless the operator also sets `CORLINMAN_GRPC_AGENT_ALLOW_PUBLIC=1`
  (with a structlog warning on every public-bind resolution for audit).
  Raises `GrpcAgentBindError` with actionable remediation message on
  refusal. (#SEC-204)
- **(high) Server-side `must_change_password` enforcement.** The
  seeded `admin/root` first-boot credentials had `must_change_password=True`
  but only the UI cookie flow consulted the flag; every `/admin/*` route
  accepted Basic admin/root regardless. With the default `0.0.0.0` bind
  that opened a real first-boot takeover window. `_auth_shim` now 403s
  every protected admin route with `password_change_required` while the
  flag is set; only the rotation + introspection allowlist (login /
  logout / me / password / username / onboard / onboard/finalize) may
  run. The flag is mirrored onto admin-B state so both bundles fire
  uniformly. (#SEC-007)
- **(high) artifact-panel XSS hardening.** SVG artifacts were rendered
  via `dangerouslySetInnerHTML` (executes `<script>` and event handlers
  in the admin origin with operator session cookies). HTML iframe
  previews used `sandbox="allow-scripts allow-same-origin"` on srcDoc
  (defeats sandbox isolation for same-origin scripts). Any LLM output
  including a crafted `<svg onload=…>` or HTML payload was stored XSS
  with admin-API access. SVG now renders inside an empty-sandbox iframe;
  HTML iframe sandbox no longer includes `allow-same-origin`. (#R1-004)
- **(high) WeChat XML parser hardened against billion-laughs DoS.**
  `parse_wechat_xml` used stdlib `xml.etree.ElementTree.fromstring`,
  vulnerable to entity-expansion bombs against attacker-controlled
  webhook bodies (vendor signature is a low-entropy shared token —
  once leaked, attacker can mint signed bomb payloads). Swapped to
  `defusedxml.ElementTree.fromstring`; widened the catch to
  `(ET.ParseError, ValueError)` so `EntitiesForbidden` / `DTDForbidden`
  collapse into the existing 400 contract. New dep: `defusedxml>=0.7.1`
  on `corlinman-channels`. (#R2-004)
- **(high) Canvas session-id entropy raised 32 → 192 bits.**
  `_new_session_id` returned `"cs_" + uuid.uuid4().hex[:8]` (32 bits)
  guarding `GET /v1/canvas/session/{id}/events` (SSE — operator output
  exfil) and `POST /v1/canvas/frame` (push frames into another session).
  With TTL=600s and ≥1 active session, a fan-out scanner could land on
  a live id within minutes. Switched to `secrets.token_urlsafe(24)` =
  192-bit ids (NIST SP 800-63B recommendation). Ids are ephemeral so
  no migration needed. (#R2-005)

### Fixed

- **(critical) Scheduler default cron jobs actually run now.**
  `scheduler.runner.dispatch()` routed `kind="run_tool"` and
  `kind="run_agent"` to `_emit_failed("unsupported_action")` instead of
  the live `BUILTIN_ACTIONS` registry. The two default jobs
  (`system.update_check` + `evolution.darwin_curate` registered in
  `entrypoint.py`) silently never fired since the v1.8.0 cron port;
  the admin "fire now" route worked because it bypassed dispatch — so
  the bug shipped green. dispatch() now resolves
  `BUILTIN_ACTIONS[f"{plugin}.{tool}"]`, awaits `run_builtin(...)`, and
  emits `EngineRunCompleted` (`ok=True`) or `EngineRunFailed(error_kind=
  "builtin_not_ok")` (`ok=False`). Unknown plugins/tools still emit
  `unsupported_action` legitimately. (#R3-002)
- HookEvent emissions in `agent_servicer.py` were passing `turn_id=None`
  to `TurnComplete` / `TurnErrored` across three branches because
  `journal_turn_id` was cleared *before* the constructor call (the
  null-then-use pattern was repeated identically in all three sites).
  Hook subscribers saw `turn_id=None` for every successful turn, every
  error-event turn, and every catch-all exception. Now captured into a
  local before the consume-clear. (#R1-002)
- Provider clients leaked per chat turn. `AsyncOpenAI(...)` and
  `AsyncAnthropic(...)` were constructed inline in `chat_stream` with no
  `await client.close()` on any path — httpx connection pool grew
  unbounded per turn until fd exhaustion. Now wrapped in `try/finally`
  with `_safe_close`; 401-recovery on OpenAI explicitly closes the stale
  client before the retry mints a fresh one. Inherited fix covers
  Azure / OpenAI-compatible / DeepSeek / Qwen / GLM / Mistral / Cohere /
  Together / Groq / Replicate. (#R1-003)
- `useChatStream` leaked an EventSource per rapid resend. The live-stream
  close-ref was reassigned without invoking the prior close fn, so a
  second send during an in-flight turn (or `editAndRerun` during the
  500ms grace window) left the first SSE consumer reducing into a stale
  `pendingMessage` until page unmount. Now `closeLiveRef.current?.()` runs
  before reassignment. (#R1-005)
- `Inbox.increment_retry` had a SELECT-then-UPDATE race — two concurrent
  retry calls both read `retries=0`, both wrote `1`, lost one increment,
  so the row never reached `_MAX_RETRIES` and retried forever instead
  of flipping to `'dead'`. Rewritten as a single atomic `UPDATE … SET
  retries = retries + 1, status = CASE WHEN retries + 1 >= ? THEN 'dead'
  ELSE 'pending' END … RETURNING retries`. (#R2-002)
- Follow-up to the above: the atomic UPDATE had no status guard, so a
  stray `increment_retry` against an already-`done` or `dead` row would
  resurrect it back to `'pending'` (the boot drainer or a replayed
  signal could then redeliver an already-processed message). Added
  `AND status IN ('pending','dispatched')` to the WHERE clause; the
  existing `if row is None: return -1` no-op return tells callers the
  update was a no-op. (#R3-001)
- Fire-and-forget `asyncio.create_task` calls in
  `gateway/evolution/signals/user_correction.py` and `corlinman_hooks/bus.py`
  held no strong reference to the spawned tasks — per CPython asyncio
  docs, the runtime keeps only weak refs and tasks can be GC'd
  mid-execution under load, silently dropping signal handler dispatches.
  Applied the existing `set[Task]` + `add_done_callback(set.discard)`
  idiom already used in `channels/service.py` and `native_upgrader.py`.
  (#R2-003)
- Subagent dispatcher cap was advertised "per-tenant" in docstring,
  exception name, and config field but actually counted in-flight
  subagents across all tenants — one noisy tenant could starve every
  other tenant's dispatches. Added `tenant_id` to `SubagentRequest`
  (default `"default"` so existing callers keep working), threaded
  through from `parent_ctx.tenant_id` in `tool_wrapper.py`, and
  filtered the snapshot via `count_in_flight_for_tenant`. Aligns with
  the supervisor's authoritative per-tenant ceiling. (#R3-004)
- `UpgradeStatus.is_terminal()` excluded `"stalled"` while
  `is_in_flight()` included it. Both `NativeUpgrader.progress` and
  `DockerUpgrader.progress` used `is_terminal()` as the SSE loop exit
  condition, so a stalled upgrade left the progress generator polling
  every 500ms forever — leaking task + WS bytes per observer until the
  client disconnected. One-line fix: `is_terminal()` now includes
  `"stalled"`; `is_in_flight()` semantics ("still occupying a slot")
  preserved. (#R3-005)

### Tests

- New `tests/gateway/routes/test_chat_approve.py` covers all five
  branches of the per-turn approval handler (503 approvals_disabled,
  400 invalid_request × 2, 404 resolver miss, 500 resolver exception,
  200 approve, 200 deny, call_id strip + scope passthrough). The
  handler shipped with zero direct tests before this commit. (#TEST-001)
- New `tests/gateway/routes/test_chat_streaming.py` covers the
  client-disconnect cancel propagation branch in `/v1/chat/completions`
  by driving the raw ASGI protocol and asserting the scripted
  `ChatService` observes `cancel.set()` mid-stream. The disconnect
  branch (the only thing that stops billing upstream tokens after the
  user closes their tab) was previously unexercised. (#TEST-002)
- New `tests/test_failover.py` (47 cases) covers the
  `CorlinmanError` hierarchy in isolation. New
  `tests/test_anthropic_provider_error_mapping.py` (15 cases) covers
  Anthropic vendor HTTP status → mapped exception class via respx
  stubs, including the `BadRequest` message discrimination paths and
  `Retry-After` header handling. Discovered (filed in audit, not fixed
  here): Anthropic + OpenAI mappers both drop the `Retry-After` header
  so `RateLimitError.retry_after_ms` is always `None`. (#TEST-003)
- New `ui/lib/sse.test.ts` (9 cases) + `ui/lib/sessions/__tests__/event-stream.test.ts`
  (12 cases) cover the SSE wrapper backoff schedule, retry-counter
  reset on first message, disposed-flag race, URL composition with /
  without `since`, malformed-frame handling, and reconnect schedule
  delegation. Both wrappers shipped with zero direct tests despite
  carrying every chat / log / approval stream. (#TEST-004 #TEST-005)
- New `tests/gateway/grpc/test_agent_server.py` (13 cases) +
  `tests/gateway/routes_admin_a/test_must_change_blocks_admin_routes.py`
  (10 cases) cover the SEC-204 and SEC-007 fixes respectively. Updated
  `tests/gateway/lifecycle/test_evolution_wiring.py` (5 cases) to
  rotate the seeded password before hitting protected admin routes
  under the new gate.

### Docs

- README no longer lists `newapi` as a production provider — the entire
  `corlinman-newapi-client` package was deleted, `/admin/newapi*`
  routes are gone, and `ProviderKind.NEWAPI` was removed. A short
  migration footnote points operators at the silent
  `kind: newapi → openai_compatible` shim in `corlinman_providers.specs`.
  (#QUAL-001)
- README RAG section downgraded to BM25-only reality. Earlier claims
  about a "usearch HNSW index", "Reciprocal Rank Fusion", and "optional
  cross-encoder rerank (`bge-reranker-v2-m3`)" were all aspirational —
  zero matches in `python/packages/**/src/`. Now described as "SQLite
  FTS5 (BM25) today; HNSW + RRF + cross-encoder rerank on the roadmap"
  with a link to `docs/PLAN_PORT_COMPLETION.md`. Chinese section
  mirrored. (#QUAL-004)
- Bonus: Chinese `doctor` count corrected `21 项 → 9 项` to match
  the actual `doctor.py` check registry. (#QUAL-005)
- Version badge bumped 1.7.0 → 1.9.0. (#QUAL-002)

## [1.8.13] — 2026-05-28 — Fix: glass-card backdrop covers every chat pane

> Follow-up to v1.8.12. The glass-card backdrop was only painting on
> the message list **after** there were messages — the empty `/chat`
> landing (左 sidebar + 右「开始一段新对话」) and the live header /
> composer were still sitting directly on the dark oil-painting
> wallpaper, which is the screen the user lands on every time they
> open the surface.

### Fixed

- `app/(admin)/chat/layout.tsx` — the two-column flex shell now
  carries `gap-3 sm:gap-4 p-3 sm:p-4` so each pane reads as its own
  card against the wallpaper.
- `components/chat/chat-sidebar.tsx` — both the expanded and the
  collapsed `<aside>` use `rounded-xl border border-tp-glass-edge
  bg-tp-glass shadow-tp-panel overflow-hidden` (was a faint
  `bg-tp-glass-inner/30` + right border that effectively dissolved
  into the wallpaper).
- `components/chat/chat-area.tsx` — the chat column `<section>` is
  now wrapped in the same glass-card class set, with the outer
  flex container gaining `gap-3 sm:gap-4` so the artifact panel
  (when open) reads as its own neighbour card.
- `components/chat/artifact-panel.tsx` — promoted from
  `bg-tp-glass-inner/30 + border-l` to the full glass card so the
  visual language stays consistent across all three panes.
- `app/(admin)/chat/page.tsx` — the empty-state `<section>` (the
  "开始一段新对话" landing) is now a glass card too, so the screen
  you see before picking a session has the same readable backdrop
  as a live conversation.
- `components/chat/message-list.tsx` — reverted the inner card I
  shipped in v1.8.12; it would have layered a second card inside
  the now-card chat-area section. The scroll container is back to
  the plain `relative h-full` it was pre-v1.8.12.

## [1.8.12] — 2026-05-28 — Fix: provider enable now wires /chat end-to-end

> User report: enabling an `openai_compatible` provider in
> `/admin/providers` (with `key: literal` + base_url pointing at a
> private relay like `https://cdnapi.cornna.xyz`) showed up as 「已启用」
> on the admin card and probing `/v1/models` returned a healthy list,
> but `/chat` still refused to produce any response. Multiple users
> hit this.
>
> Root cause was a **double bug** in the model → provider resolution chain:
>
> 1. `routes_admin_b/providers.upsert_provider` only persisted the
>    `[providers.<name>]` block. It never touched `[models]`, so
>    `models.default` stayed empty.
> 2. `OpenAICompatibleProvider.supports()` is intentionally hardcoded
>    to `return False` — the OpenAI-compatible adapter only resolves
>    via an explicit `[models.aliases.*]` entry, never via the legacy
>    `MODEL_PREFIX_DEFAULTS` prefix scan.
>
> Combined effect: the chat composer fell back to its `FALLBACK_MODEL
> = "gpt-4o"`, `ProviderRegistry.resolve` skipped the user's enabled
> provider (because `.supports()` rejected `gpt-4o`), landed in the
> legacy prefix branch, and instantiated a default `OpenAIProvider`
> pointing at `api.openai.com` with **no API key** — the user's
> literal key was never reached. The same dead-end blocked every
> subagent / scheduled-job code path that resolves a model through
> the registry, so "前端配 apikey 后全部功能均可用" was effectively
> false until both legs were fixed.

### Fixed

- `routes_admin_b/providers.upsert_provider` and `patch_provider`
  now call a new `_autobind_default_alias` after persisting an
  enabled provider. When `models.default` is empty, it probes the
  provider for `/v1/models`, picks a sensible model id (preferring
  well-known ones like `gpt-4o-mini` / `claude-3-5-haiku-latest`
  over the alphabetical first), writes
  `[models.aliases.<name>] = { provider, model, params }`, and
  sets `models.default = <name>`. Probe failure falls back to a
  per-kind default model. Idempotent — an existing `models.default`
  is never clobbered.
- `routes_admin_a/sessions._replay_from_journal` now passes
  `tool_calls` through on assistant messages and folds the
  matching `role="tool"` row's content back onto the originating
  call's `result` field. Without this, tool-only assistant turns
  rehydrated as empty bubbles on session resume — visible as a
  column of timestamp-only ghost bubbles in the `/chat` history.
- `ui/lib/api/sessions.ts` — `TranscriptMessage` gains the
  optional `tool_calls?: TranscriptToolCall[]` field; new
  `TranscriptToolCall` type mirrors the OpenAI shape plus an
  optional `result` slot.
- `ui/app/(admin)/chat/page.tsx` — `transcriptToChatMessages`
  rehydrates `tool_calls` into `ToolCallState[]` (status
  `settled` when a `result` is present, `ok` otherwise) so the
  bubble renders historical tool invocations + their outputs.
- `ui/components/chat/message-list.tsx` — the scroll container is
  now wrapped in a `bg-tp-glass + border-tp-glass-edge +
  shadow-tp-panel` rounded card with `p-3 sm:p-4` outer breathing
  room, so the conversation reads against the dark navy oil-paint
  wallpaper instead of dissolving into it.

### Tests

- `tests/gateway/routes_admin_b/test_providers_autobind_default.py`
  — 5 cases: enable → autobind from probed list, existing
  `models.default` preserved, probe failure → kind fallback,
  PATCH-enable triggers autobind, disabled upsert skips autobind.
- `tests/test_sessions_replay_tool_calls.py` — pins the journal →
  replay → transcript `tool_calls` passthrough, including the
  tool-result fold.

## [1.8.11] — 2026-05-28 — Fix: follow GitHub redirects after repo transfer

> The corlinman repo was transferred `ymylive/corlinman` →
> `sweetcornna/corlinman` in 2026-05. GitHub responds to every API
> request against the old owner with a `301` to the canonical numeric
> repo id. Two consumers silently broke on this:
>
> 1. `update_checker` — `httpx.AsyncClient` defaults to
>    `follow_redirects=False`, so every poll hit the 301 branch
>    (`update_check.unexpected_status` in the gateway log) and the
>    on-disk cache was never refreshed. The `/admin/system` page kept
>    showing whatever `latest_tag` was current at the moment of the
>    rename — observed on prod as "最新版本 1.8.1" displayed against
>    `current = 1.8.10`.
> 2. `corlinman-upgrader.sh` — `curl -fsS` (no `-L`) on the
>    `/releases?per_page=100` whitelist check returned the
>    `{"message":"Moved Permanently"}` stub instead of the JSON array.
>    `jq` then exits non-zero on `.[]`, the script writes
>    `tag_not_in_releases`, and the one-click upgrade refuses to start
>    even though the tag exists at the new location.

### Fixed

- `UpdateChecker._client()` now constructs `httpx.AsyncClient(
  follow_redirects=True, ...)`.
- `SystemUpdateCheckConfig.repo` default flipped to
  `"sweetcornna/corlinman"`. Saves one redirect on every poll and keeps
  the cache fresh even on hosts whose corporate proxy strips 3xx.
- `deploy/corlinman-upgrader.sh` — `curl -fsSL` (added `-L`) on the
  release whitelist; default `UPGRADER_REPO_OWNER` flipped to
  `sweetcornna`.

## [1.8.10] — 2026-05-28 — Fix: one-click native upgrader can find uv/pnpm

> User report: clicking "升级到 v1.8.x" on the admin /system page wrote
> the request file, the systemd helper fired, but the upgrade always
> ended in `install_sh_exit_1` with no clear reason in the UI. Root
> cause: `corlinman-upgrader.service` runs as `User=root` with systemd's
> restrictive default `PATH` (`/usr/local/sbin:/usr/local/bin:/usr/sbin:
> /usr/bin:/sbin:/bin`), which excludes `/root/.local/bin` where `uv`
> actually lives. `install.sh`'s `require uv` therefore died before the
> repo was even fetched. The same gap silently skipped the UI rebuild
> (no pnpm on PATH).
>
> Existing deployments need one manual upgrade (per `docs/deploy/...`
> runbook) to land the new scripts. From v1.8.10 onward the one-click
> flow completes end-to-end.

### Fixed

- `install.sh` now calls `augment_path` before any `require` check —
  prepends `$HOME/.local/bin`, `/root/.local/bin`, every
  `/home/*/.local/bin`, and `/usr/local/lib/node_modules/.bin` to PATH
  when they exist. Idempotent; no-op when PATH already contains them.
- `corlinman-upgrader.sh` performs the same probe at the top of the
  script (before invoking `install.sh`), so older units that pre-date
  the new `Environment=PATH=` line still recover at runtime.
- `install_native()` writes
  `Environment=PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:
  /usr/sbin:/usr/bin:/sbin:/bin` into the freshly emitted
  `corlinman-upgrader.service` — canonical fix for new installs.

## [1.8.9] — 2026-05-28 — Fix: journal-backed replay uses real facade methods

> v1.8.8 added the journal-backed replay path but called
> `AgentJournal.load_messages` which doesn't exist on the facade
> (the public method is the underscored `_load_messages`). The
> defensive try/except in `_replay_from_journal` swallowed the
> AttributeError and returned None, so the replay endpoint still
> 404'd. This release wires it to the actual facade surface
> (`list_session_turns` + `_load_messages`) and verifies end-to-end
> on the deployment journal.

### Fixed

- `_replay_from_journal` now calls `journal.list_session_turns(key, limit=500)`
  to get all turn metadata (including `started_at_ms`) in one pass,
  then `journal._load_messages(tid)` per turn for the actual
  message rows. Reverses to chronological order, filters to
  user/assistant/system roles, synthesises ISO ts from the turn's
  start time.

## [1.8.8] — 2026-05-28 — Fix: /admin/sessions/.../replay now reads from the actual journal

> User report: clicking a sidebar row surfaced "未找到该会话 — not in
> sessions database, may have been cleaned up". Root cause: the
> `/admin/sessions` listing reads from
> `<data_dir>/agent_journal.sqlite` (where every `/v1/chat/completions`
> turn lands), but `/admin/sessions/{key}/replay` was reading from a
> different file, `<data_dir>/sessions.sqlite`, which the OpenAI-compat
> path never writes to and is empty on this deployment. So every
> session listed in the sidebar would 404 when clicked.

### Fixed

- `_replay_for_request` now tries a new `_replay_from_journal` path
  first: it opens `agent_journal.sqlite` via the existing
  `AgentJournal` facade, pulls every `turn_id` for the requested
  `session_key`, calls `load_messages` per turn, filters to
  user/assistant/system roles, synthesises per-turn ISO timestamps
  from `turns.started_at_ms`, and returns a `ReplayOutput`-shaped
  dict that drops straight into `_replay_to_dict`. Falls back to
  the legacy `sessions.sqlite` store when the journal has nothing
  for the key (covers operators with pre-port history).
- `_replay_to_dict` passes dict inputs through unchanged so the
  journal path doesn't need to fabricate the legacy dataclasses.

### Tests

- `routes_admin_a` pytest suite: **76 passed**.
- Removed pre-existing stale `python/packages/corlinman-embedding/`
  directory (empty `src/` + `tests/` with no `pyproject.toml`)
  unblocking `uv lock` / `uv run pytest`.

## [1.8.7] — 2026-05-28 — Fix: legacy empty-session_key rows in sidebar + defensive fallback

> User report: clicking a row in the chat sidebar opens the
> conversation view with no messages. The journal still holds the
> pre-1.8.5 turns that were written under `session_key=""` (when the
> `metadata.session_key` plumbing bug meant every web chat aggregated
> into a single un-resumable row); the sidebar was rendering that
> aggregate as a normal row, but clicking it routed to
> `/chat?session=` with an empty value, which falls through to the
> empty state because `<ChatArea>`'s render guard treats it as
> "no session selected".

### Fixed

- **`ChatSidebar` hides legacy empty-`session_key` rows** — the
  journal still keeps them for audit, but they're un-clickable so
  surfacing them in the sidebar just confused operators. Once 1.8.5
  shipped, all new turns get proper keys so this filter only ever
  hides the legacy aggregate.

### Added

- **Defensive `useChatStream` fallback** — if `args.sessionKey` is
  ever empty/undefined (the page-level conditional already prevents
  this, but belt-and-braces), the hook now synthesises a fresh
  `corlinman:{ts}:{rand}` key on the fly and warns to the console.
  Guarantees the journal never receives an empty key from a frontend
  turn, regardless of how the page was navigated to.

### Tests

- Chat suite 58/58 passing; typecheck clean; `next build` succeeds
  with `/chat` at 23.5 kB.

## [1.8.6] — 2026-05-28 — Chat model picker (LLM + image, with custom names)

> Operators can now pick the chat composer's LLM model and the image
> model the agent should hand off to image-generation tools. Both
> default to upstream-probed values; both accept a free-text custom
> name for models the registry hasn't caught up to.

### Added

- **`ChatModelPicker` dialog** (`ui/components/chat/chat-model-picker.tsx`)
  — `kind="llm"` lists every alias in `/admin/models` + every model
  probed from each enabled provider via
  `/admin/providers/{name}/models`; `kind="image"` lists every
  image-capable provider's `image_model` config + their probed
  models, with `gpt-image-2` as the silent fallback. Both surfaces
  include a free-text input at the top so the operator can type any
  model name not in the list.
- **Image-model pill on the composer** — new optional pill next to
  the LLM and persona pills; clicking opens the image-model picker.
- **Per-operator overrides persisted to localStorage** — selections
  land under `corlinman:chat:llm-model` and `corlinman:chat:image-model`;
  empty values fall back to the upstream default (LLM = global
  `models.default`, image = `gpt-image-2`). The composer pills always
  reflect the effective value.
- i18n keys: `chat.modelPicker.{titleLLM, titleImage, currentBadge,
  defaultBadge, aliasBadge, customLabel, customPlaceholder,
  useCustom, filterPlaceholder, listAriaLabel, emptyList}` —
  en + zh-CN.

### Tests

- Chat suite 58/58 passing; typecheck clean; `next build` succeeds
  with `/chat` at 23.4 kB (+3 kB for the picker dialog).

## [1.8.5] — 2026-05-28 — Fix: session_key was being dropped; spurious "network error" on stream end

> User report: every finished turn briefly flashed a "network error"
> banner, and the sidebar's old conversations couldn't be re-opened.
> Two upstream bugs.

### Fixed

- **`session_key` silently dropped → every web turn landed under
  `session_key=""` in the journal** — the frontend sent the
  per-conversation id inside `metadata.session_key`, but the
  gateway's `ChatRequest` pydantic model only reads `session_key` at
  the top level (the `metadata` bag is `extra="allow"` and is
  silently discarded on the way to the reasoning loop). The journal
  then stored every web turn against the empty string, so
  `/admin/sessions` only showed one giant aggregated row and clicking
  it routed to an unmatchable empty session. Fix: move `session_key`
  to the top level of `ChatCompletionRequest`; only `agent_id` /
  `persona_id` stay in metadata.
- **"Network error" toast at the end of each turn** — some
  OpenAI-compat servers (including ours, under load) close the SSE
  connection abruptly after sending `data: [DONE]`. The fetch
  ReadableStream reader then throws a TypeError; we were surfacing
  that as `turn-errored` even though the model finished successfully.
  Fix: track a `finishReceived` flag during the for-await loop; if
  we already processed a `finish_reason` chunk, swallow any
  subsequent read errors as the expected post-completion close.

### Added

- **Sidebar refresh on turn completion** — `useChatStream` now grabs
  the React Query client and invalidates the
  `["chat", "sessions"]` query as soon as a turn commits, so a
  freshly-created conversation appears in the sidebar instantly
  instead of waiting for the 30 s polling interval to tick.

### Tests

- Chat suite 58/58 passing; typecheck clean; `next build` succeeds
  with `/chat` at 20.4 kB.

## [1.8.4] — 2026-05-28 — Fix: tool calls stuck "(pending)" + collapsible tool list

> User report: an assistant turn that fires many tool calls renders
> them all as `(pending)` with a spinning indicator that never
> resolves. Two underlying bugs and a missing UI affordance.

### Fixed

- **Tool name never appeared** — `chunkToChatEvents` skipped the
  first `/v1/chat/completions` chunk for every tool call because it
  only carries `function.name` (with empty `arguments`); the previous
  `if (argsDelta)` guard dropped it. The card was created later with
  the placeholder `"(pending)"` and never updated. Now: when
  `function.name` is present, we emit a `tool-running` event with
  the real tool name immediately; subsequent arg-only chunks still
  go through `tool-input-delta`.
- **Spinner spun forever** — the OpenAI-compatible
  `/v1/chat/completions` path doesn't emit the journal
  `ToolStateCompleted` event the hermes gRPC path does, so the
  reducer never had a signal to stop the spinner. New synthetic
  `tools-settle` event is now emitted alongside `turn-complete`
  whenever `finish_reason` arrives; the reducer demotes every
  still-`running` tool to a new `"settled"` status (neutral check
  icon, no spinner). Tools that get a journal-driven `ToolStateCompleted`
  keep their `"ok"` / `"error"` terminal status.

### Added

- **Hamburger collapse on the message bubble** — when an assistant
  message renders ≥ 1 tool call or sub-agent card, a small `Menu`
  hamburger appears under the message content. Clicking it hides
  every tool/sub-agent card and replaces the strip with a localised
  summary (`已隐藏 12 个工具调用` / `12 tool calls hidden`); clicking
  again expands. Auto-collapses on assistant messages with ≥ 8
  combined tool + sub-agent cards once streaming completes, so long
  agent loops don't drown the bubble.
- i18n keys: `chat.bubbleToggleToolsCollapse`,
  `chat.bubbleToggleToolsExpand`,
  `chat.bubbleToolsCollapsedSummary` (one/other),
  `chat.bubbleSubagentsCollapsedSummary` (one/other) — en + zh-CN.

### Tests

- Updated `event-merger.test.ts`: the `finish_reason` case now
  asserts both events (`tools-settle` + `turn-complete`); new test
  covers the `function.name` first-chunk case → `tool-running`.
- Chat suite 58/58 passing; typecheck clean; `next build` succeeds
  with `/chat` at 20.3 kB.

## [1.8.3] — 2026-05-28 — Full i18n for the /chat surface (zh-CN ↔ en)

> Every visible string in the chat surface — sidebar, composer,
> message bubbles, hover toolbars, tool / sub-agent / approval cards,
> reasoning blocks, artifact panel, search overlay, slash + mention
> menus — now goes through `useTranslation()`. The page picks up
> whichever language the operator chose in the language switcher,
> with no English leaks remaining in default Chinese mode.

### Changed

- New top-level `chat: { … }` block in `ui/lib/locales/en.ts` +
  `zh-CN.ts` with ~100 keys covering every chat-surface string.
- 11 chat components rewritten to consume `t("chat.…")` instead of
  hardcoded English: `empty-state`, `chat-sidebar`, `chat-area`,
  `composer`, `composer-attachments`, `composer-slash-menu`,
  `composer-mention-menu`, `message-list`, `message-bubble`,
  `markdown-message` (code-block actions), `tool-call-card`,
  `reasoning-block`, `subagent-card`, `approval-prompt`,
  `conversation-search`, `artifact-panel`.
- Slash command labels (`/clear`, `/reset`, `/model`, `/persona`),
  approval scope labels (once / session / always), sub-agent status
  pills (spawned / running / completed / errored), sidebar recency
  groupings (Today / Yesterday / Previous 7 / 30 / Older / Pinned /
  Archived), composer placeholder + send/stop buttons, jump-to-latest
  pill — all translated.
- `/chat` toast strings (delete + Undo + error labels) now use
  `t("chat.deletedToast")`, `t("chat.undo")`, and `t("common.saveFailed")`.

### Tests

- `chat-sidebar.test.tsx` + `artifact-panel.test.tsx` +
  `message-bubble.test.tsx` updated to assert Chinese strings (since
  vitest defaults to `zh-CN`).
- Whole chat suite: 57/57 passing. typecheck clean. `next build`
  succeeds with `/chat` static-exported.

## [1.8.2] — 2026-05-28 — Chat composer follows the global default model

> The 1.8.0 chat surface hardcoded `gpt-4o` as the model the composer
> sent to `/v1/chat/completions`. That ignored the global default set
> in `/admin/models` — confusing for operators who'd already picked a
> production-grade alias there. This release wires the chat page to
> the same `models.default` field every other surface reads.

### Changed

- `/chat` composer model now resolves from
  `fetchModels().default` (the same alias surfaced by
  `/admin/models`). React Query caches the value with a 60s stale
  window so a default-model swap in `/admin/models` propagates to the
  next composer turn without reloading the page. `gpt-4o` stays as a
  silent fallback for the (rare) case where no global default has
  been configured yet, so the surface is still usable on a fresh
  install.

## [1.8.1] — 2026-05-28 — Hotfix: static-export compatibility for /chat

> Build-only hotfix: 1.8.0 shipped `/chat/[sessionKey]` as a Next.js
> dynamic route, which `output: "export"` rejects without a build-time
> `generateStaticParams()` enumeration. The fix folds the dynamic
> segment into a query string — `/chat?session=…` — mirroring the
> existing pattern used by `/admin/sessions/detail`, so `pnpm build`
> succeeds and the UI ships as static assets again.

### Fixed

- `/chat` static export — merged `/chat/[sessionKey]/page.tsx` into the
  root `/chat/page.tsx`; the page now reads the active session key
  from `useSearchParams("session")`. All navigation that previously
  pushed `/chat/${key}` now pushes `/chat?session=${key}` (sidebar
  rows, sessions-row "Continue" link, branch fork in `chat-area`,
  empty-state suggestion picks, e2e fixture URLs). Tests adjusted in
  step (`session-row.test.tsx` and `chat-mvp.spec.ts`).

### Unchanged

- Backend / API contracts (`/admin/sessions/{key}/cancel`,
  `PATCH /admin/sessions/{key}`, `/api/channels/corlinman/*`),
  `CorlinmanChannel`, session metadata schema, and every chat surface
  feature from 1.8.0.

## [1.8.0] — 2026-05-28 — In-app /chat surface + corlinman channel

> Lands a **Claude.ai-grade conversation window** at `/admin/chat`
> driven by the existing hermes agent backend, plus the supporting
> `corlinman` channel so the web chat sits as a first-class member of
> the channels abstraction (sibling of telegram / qq / discord). Every
> existing session — telegram, qq, scheduled persona runs — can now be
> resumed in the browser with one click from `/admin/sessions` and the
> full historical transcript pre-loaded.
>
> The conversation surface is deliberately built on the *existing* live
> event stream (`/admin/sessions/{key}/events/live` SSE) merged with
> the OpenAI-compatible `/v1/chat/completions` token stream — so tool
> calls, sub-agent spawns, reasoning blocks, and approval prompts all
> render inline with the assistant turn without any new wire protocol.
>
> Naming convention is now uniform — every new identifier uses the
> `corlinman_*` prefix; the placeholder `web*` names the design doc
> proposed were renamed end-to-end before release.

### Added

- **In-app `/chat` surface** — Next.js admin route at `/admin/chat`
  (collapsed sidebar on the left, resizable artifact panel on the
  right). Conversations grouped by recency (Pinned / Today / Yesterday
  / Previous 7 / 30 / Older / Archived); fuzzy search; rename / pin /
  archive / delete-with-undo-toast.
- **Streaming UX with hermes-loop awareness** — token-by-token render
  with a smooth cursor; collapsible Claude-style reasoning blocks;
  tool-call cards (running / ok / error with args + result panes);
  nested sub-agent cards; inline approval prompts (Deny / Approve once
  / Always-session) wired to `POST /v1/chat/completions/{turn_id}/approve`.
  Stop button aborts the stream and posts `cancel`; Retry replays the
  last user message.
- **Composer (Cursor / Claude.ai parity)** — auto-grow textarea, Enter
  to send, Shift+Enter newline, paste / drag-drop file attachments
  (50 MB cap, image / pdf / audio / video / document MIME allowlist);
  `/` slash commands (`/clear`, `/reset`, `/model`, `/persona`);
  `@`-mention picker for agents / skills; reply-with-quote chip above
  the textarea; model + persona pills; send ↔ stop button swap.
- **Artifact panel** — code blocks ≥ 25 lines (or `html` / `svg` /
  `mermaid` / `markdown`) auto-surface as artifacts with tabs across
  the top, sandboxed iframe preview for HTML, inline SVG render,
  source view, version history when the same id is re-emitted, copy +
  download per artifact.
- **Message-level actions** — hover toolbar on every bubble: copy,
  regenerate (assistant), edit-in-place (user) → drops history after
  the edited message and re-runs the turn, branch fork → opens a new
  session pre-loaded with the slice up to that message, reply quote,
  jump-to-message via `id="chat-msg-{id}"`.
- **Token + cost meter** — header chip aggregates input/output tokens
  + estimated cost across all completed assistant turns of the
  session.
- **In-conversation search** — Cmd / Ctrl + F overlay, Enter / Shift +
  Enter walks next / prev match, Esc closes; scrolls the matching
  bubble into view.
- **"Continue" from sessions list** — new action on every row of
  `/admin/sessions` routes to `/admin/chat/{sessionKey}` and the
  `replaySession(mode=transcript)` call auto-hydrates the full
  conversation history before the composer accepts input. Operators
  can pick up any telegram / qq / scheduled chat in the browser.
- **`CorlinmanChannel` (channels-abstraction citizen)** — new module
  `corlinman_channels.corlinman` implementing the `Channel` Protocol
  (id `"corlinman"`, display name "Corlinman Chat"). Owns per-session
  `asyncio.Queue[CorlinmanOutboundFrame]` queues so a browser POST →
  `ingest()` and an assistant token → `send()` meet on the same
  thread. Registered into `ChannelRegistry.builtin()` iff
  `CORLINMAN_CHANNEL_ENABLED=1` (default off; legacy `[qq, telegram]`
  ordering preserved bit-for-bit when the flag is off).
- **`/api/channels/corlinman/*` (6 endpoints)** — `POST /send`,
  `GET /events` (SSE), `POST /typing`, plus Wave 4 stubs
  `POST /edit/{msg_id}`, `DELETE /delete/{msg_id}`,
  `POST /react/{msg_id}` returning typed 503 (`edit_not_supported`)
  so the frontend can degrade cleanly.
- **`POST /admin/sessions/{key}/cancel`** — calls
  `ReasoningLoop.cancel()` on the active loop registered via a
  `WeakValueDictionary` in `agent_servicer`; returns
  `{status: "cancelled" | "not_running" | "unknown_session", turn_id}`.
- **`PATCH /admin/sessions/{key}`** — `{title?, pinned?, archived?}`,
  returns the refreshed `SessionSummaryOut`. Sort order now is
  `pinned DESC, last_seen DESC`.
- **`session_meta` side table** — SQLite + Postgres backends gain a
  new `journal_session_meta` table (additive `CREATE TABLE IF NOT
  EXISTS`, zero `ALTER` on existing tables; LEFT JOIN with COALESCE
  defaults so pre-meta sessions round-trip unchanged).
- **i18n** — `nav.chat` ("Chat" / "聊天") and
  `sessions.continueInChat` ("Continue" / "继续聊天") added to en +
  zh-CN bundles.

### Changed

- **Sidebar nav order** — "Chat" inserted as the top operator entry,
  above the existing Playground / Approvals / Sessions rows.
- **Naming convention** — every new identifier across both planes
  uses the `corlinman_*` prefix (`CorlinmanChannel`,
  `corlinman_channel_enabled`, `CORLINMAN_CHANNEL_ENV_FLAG`,
  `/api/channels/corlinman/*`, `ChannelBinding("corlinman", …)`,
  session-key prefix `corlinman:{ts}:{rand}`,
  `sessionStorage` namespace `corlinman:chat:branch:{key}`); aligns
  the chat surface with the rest of the project.

### Tests

- **Python**: 47 new tests for `CorlinmanChannel` + route layer (34
  channel unit + 13 route); 21 new tests for the session cancel +
  PATCH endpoints; whole channels suite 693 passed, `routes_admin_a/b`
  suites combined 947 passed.
- **Frontend**: 57 new Vitest tests across the chat tree
  (`event-merger` × 13, `message-bubble` × 6, `composer` × 6,
  `chat-sidebar` × 6, `artifacts` × 7, `artifact-panel` × 6,
  `composer-mention-menu` × 8, `conversation-search` × 3) + 2 for the
  `SessionRow` "Continue" action.
- **E2E**: new `chat-mvp.spec.ts` (Playwright, stubs-only) covering
  the golden path — list / new chat / send + stream / tool-card / slash
  menu / sidebar collapse.

### Plan

[`docs/PLAN_IN_APP_CHAT.md`](docs/PLAN_IN_APP_CHAT.md) — full design
document with the 4-wave breakdown, architecture decisions, file
structure, and risk register that drove this release.


## [1.7.0] — 2026-05-28 — First-run wizard + 主聊天窗口 + image-provider probe

> Lands the **first-run wizard initiative**
> ([`docs/PLAN_FIRST_RUN_WIZARD.md`](docs/PLAN_FIRST_RUN_WIZARD.md))
> shipped by 6 parallel agents: a 6-step onboarding flow (API config →
> rename admin → change default password → persona choice → image API →
> done) that gates step order so the username-then-password change can
> never race; a `/sethome` slash command that pins a channel as the
> operator's home so server-restart heartbeats only fire there; a
> non-destructive image-capability probe that lets the wizard reuse
> an OpenAI-compatible chat endpoint as the image provider when it
> actually supports `/v1/images/generations`; and a sidebar rename
> ("系统" → "更新") that finally tells the truth about what the page
> does. The README now leads with the one-line installer command so
> newcomers don't have to scroll for it.

### Added

- **First-run wizard (6 steps, 6 agents, single PR)** — new admin
  endpoints `POST /admin/onboard/finalize-account`,
  `finalize-password`, `finalize-persona`, `finalize-image-provider`
  + `POST /admin/personas/use-default`; rewritten
  `ui/app/onboard/page.tsx` with strict forward-gating (clicking the
  indicator can never fast-forward past an uncompleted step) and an
  atomicity lock that disables "back to username" once the password
  step succeeds; persona step offers three cards (default `grantley` /
  custom `/persona` wizard / skip), image step offers reuse-current /
  configure-separate / skip, with a 409 fallback when the current
  provider doesn't support image generation. Persona skill grew a
  Stage -1 entry gate so most operators can opt out of the 7-stage
  voice interview in one click. ([`docs/PLAN_FIRST_RUN_WIZARD.md`])
- **`/sethome` + home-channel store** — new `home_channel_store`
  SQLite module (tables `home_channels`, `first_chat_tips_shown`);
  channel-side `/sethome` (`/主页`) handler pins the active
  `ChannelBinding` as the operator's home channel; first-chat tip
  injection in `chat_bootstrap` shows the hint exactly once per
  `(user_id, channel, thread)`; `/use-default-persona` (`/默认人格`)
  slash command seeds + selects `grantley` without entering the
  wizard; lifecycle entrypoint queues a "server restarted" heartbeat
  to every registered home channel on boot (best-effort, logged via
  `/admin/logs/stream`).
- **Image-provider capability probe** — new
  `corlinman_providers.capabilities.probe_image_capability` runs a
  non-destructive two-stage check (`GET /v1/models` regex scan first,
  `HEAD /v1/images/generations` fallback) and never calls the actual
  generation endpoint; new admin route
  `POST /admin/providers/{name}/probe-image` returns
  `{supported, evidence, models}`; `ProviderSpec` grew optional
  `image_capable` + `image_model` fields with full TOML
  backward-compat; `corlinman_agent.image.generate` now prefers a
  provider with `image_capable=true` and falls back to the chat
  default only when none is marked.
- **Sidebar "系统" → "更新"** — admin sidebar entry relabelled with new
  i18n keys `sidebar.updatesLabel`, `system.pageTitle`,
  `system.pageSubtitle` (zh-CN + en); `/system` route unchanged so
  bookmarks survive; page header copy tightened to describe version
  + upgrade actions, not generic "system settings".
- **One-command install prominence** — README + `docs/quickstart.md`
  now lead with a 🚀 callout for
  `curl -fsSL …/deploy/install.sh | bash` + `--upgrade`; version badge
  bumped to 1.7.0.

### Changed

- `routes_admin_b/__init__.py` now mounts `personas` and
  `image_provider` sub-routers alongside the existing 20.

### Tests

- 199 `routes_admin_b` + 625 channels + 81 lifecycle/chat
  substitution + 281 providers tests green; UI `tsc --noEmit` clean.



## [1.6.0] — 2026-05-26 — Persona Studio + frontend overhaul + QQ/Telegram fixes

> Lands the eight-wave **Persona Studio** initiative
> ([`docs/PLAN_PERSONA_STUDIO.md`](docs/PLAN_PERSONA_STUDIO.md)) plus a
> sweep of frontend repairs and prod-channel bug fixes that were
> blocking real bot traffic.
>
> Operators can now define any persona via `/admin/persona` (with
> drag-drop emoji + reference-image upload) **or** by typing `/persona`
> in any chat surface to launch a guided wizard. The persona's
> reference立绘 plug into a new `image_with_refs` tool that drives
> daily QQ-Zone publishing via the scheduler builtin
> `qzone.daily_publish`. The Grantley persona ships as the reference
> implementation (opt-in via "Enable Grantley daily 说说" in
> `/admin/scheduler/qzone`).
>
> The dashboard's old protocol-comparison playground was deleted; the
> Playground page is now a real system overview + chat that talks to
> `/v1/chat/completions` with SSE rendering. The Telegram channel page
> finally shows real numbers (the gateway grew 3 admin routes that
> were missing on the Python port). QQ added a NapCat account-online
> probe so the admin UI surfaces "需重新扫码" the moment Tencent kicks
> the bot.

### Added

- **Persona Studio (8 waves)** — `PersonaStore` + `PersonaAssetStore`
  with 8 MiB/asset + 200 MiB/persona caps, sha256-keyed dedup;
  `/admin/personas/{id}/assets` multipart upload + ETag-served fetch +
  cascade delete; `/admin/persona` editor extended with drag-drop emoji
  + reference-image sections (en + zh-CN i18n); 7 `persona_*` agent
  tools (list/get/create/update/delete/list_assets/attach_asset_from_url);
  new `image_with_refs` tool over OpenAI Responses API + `gpt-image-1`;
  new `qzone_publish` tool + OneBot HTTP client; `qzone.daily_publish`
  scheduler builtin + `/admin/scheduler/qzone` admin UI with cron
  preview + persona dropdown + "Run now"; per-channel humanlike toggle
  extended from QQ-only to QQ/Telegram/Discord/Slack/Feishu;
  `compose_persona_emoji_block` injects emoji manifest into the
  persona system prompt so agents can `send_attachment` flavour
  stickers; nullable `owner_user_id` column on `personas` for future
  multi-tenant auth.
- **`/persona` slash command + wizard skill** — channels router +
  web/admin chat_bootstrap now recognise `/persona` (+ `/角色` /
  `/人格` / `配置人格`) and substitute a system-inserted wizard
  prelude for the trailing user message. New bundled
  `configure-persona/SKILL.md` walks the agent through create →
  voice interview → persist → asset hint. `/help` + `/persona-list`
  shortcuts ride the same registry. Starter-skills seeder grew
  recursive subtree copy so multi-file skills auto-seed.
- **Playground page rebuilt** — `/admin/playground` is now a system
  overview (Plugins/Agents/Personas/Approvals + recent-activity tail
  via `/admin/logs/stream` SSE) plus a working chat composer that
  POSTs `/v1/chat/completions` with `stream:true` and renders OpenAI
  SSE chunks including tool_call chips. The protocol-comparison
  demo + its mock streams + helper components were deleted.
- **Telegram admin routes** — three new endpoints under
  `/admin/channels/telegram/`: `status` (online + message counts +
  p50/p95 latency + active chats), `messages` (ring buffer of recent
  inbound + outbound, capped 500), `send` (admin manual push). Backed
  by `TELEGRAM_HEALTH` + counter hooks fired from `handle_one_telegram`.
- **QQ account-online probe** — `_qq_probe_account_online` runs every
  60 s as a background task, fetches NapCat HTTP `/get_login_info`
  and writes `account_online` / `account_qq` / `account_nickname` into
  `QQ_HEALTH`. The `/admin/channels/qq` page renders an amber banner
  "QQ 账号已下线 — 需重新扫码" when `account_online === false`, wired
  to the existing ScanLoginDialog.
- **`channels_config` plumbed into `AdminState`** — the
  `/admin/channels/{qq,telegram}/status` routes finally see the live
  config the channels are running with (previously always returned
  `configured: false` because nothing wrote the dict into AdminState).

### Fixed

- **OpenAI tool-name regex** — renamed all 7 persona tools from
  dotted form (`persona.list`) to underscore (`persona_list`).
  OpenAI rejects names containing `.` via
  `^[a-zA-Z0-9_-]+$`, which broke every chat turn the moment Persona
  Studio was advertised. Updated constants, schemas, dispatchers,
  bundled skill body, command wizard prelude.
- **QQ image messages crashed every turn** — the channels-side
  `Attachment` dataclass carries a `data` field, but
  `chat_service._attachment_to_proto` reads `a.bytes_` (server-side
  field name). An inbound `[CQ:image,...]` raised `AttributeError`
  deep inside the async generator and surfaced as
  `RuntimeError("generator didn't stop after throw()")`. Added
  `_to_server_attachment_shape` converter on both QQ and
  Telegram/Discord/Slack/Feishu request builders.
- **`send_attachment` resolved relative paths against the gateway
  cwd, not the agent workspace** — every channel handler used
  `Path(path_str).exists()` so a `write_file("hello.html")
  + send_attachment("hello.html")` always failed with
  "⚠️ 发送文件失败: hello.html 不存在". New
  `resolve_attachment_path()` joins the path against
  `<DATA_DIR>/workspace` (the same resolution `write_file` uses).
- **QQ split-reply spammed `@user` on every chunk** — the group
  reply builder unconditionally prepended `AtSegment` for every
  chunk. Telegram's pattern is to anchor only `chunks[0]`; QQ now
  mirrors that.
- **`PersonaStore.open` failed on pre-W1 DBs** — `_SCHEMA` referenced
  the new `owner_user_id` column in a `CREATE INDEX IF NOT EXISTS`
  clause, which raises "no such column" on a legacy `personas` table.
  The migration now adds both column + index atomically and the
  schema script doesn't pre-declare the index.
- **VPS `ui-static/` was stale across deploys** — `install.sh` +
  `corlinman-upgrader.sh` never ran `pnpm build` nor placed
  `ui/out` into `$PREFIX/ui-static`, so admin pages drifted out of
  sync with the gateway version. Both installer scripts now have a
  `[ui]` stage that builds + rsyncs the static export, with a
  `--skip-ui` flag for headless deploys.

### Changed

- **Root version 1.2.0 → 1.6.0** — `pyproject.toml` +
  `python/packages/corlinman-server/pyproject.toml`. Other workspace
  packages keep their independent 0.x.x.
- **`send_attachment` tool description** rewritten so the model knows
  to reuse its `write_file` path verbatim and that relative paths are
  resolved against the workspace.

### Notes

- The `[Unreleased]` v1.5.0 / multi-agent / one-click-upgrade blocks
  below this section pre-date 1.6.0 and remain as historical work
  logs; their content was already on `main` before 1.6.0 was cut.

## [Unreleased] — Skill library v1.5.0

> `/admin/skills` goes from a static mock to a live two-tab surface.
> The **Installed** tab is wired to a new gateway endpoint and renders
> origin-tagged rows (`bundled` / `user` / `hub:<slug>@<ver>`) with pin
> + delete affordances. The **Browse Hub** tab proxies the
> [openclaw ClawHub](https://clawhub.ai) anonymous read surface
> server-side so an operator can search, preview, and install community
> skills without leaving the admin UI. The install pipeline is
> SSE-driven (`download.started → extract.started → installed`) with
> path-traversal + 25 MiB total / 10 MiB per-file size caps + a
> `.openclaw-meta.json` sidecar that gates the uninstall. Bundled
> starter skills (the 16 in-wheel defaults from v1.4) stay read-only:
> the UI disables the Delete button and the server returns 409
> `bundled_protected` on bypass. Plan at
> [`docs/PLAN_SKILL_HUB.md`](docs/PLAN_SKILL_HUB.md); operator deep-dive
> at [`docs/skill-hub.md`](docs/skill-hub.md).

### Added

- **ClawHub browse + install via `/admin/skills`** — new Browse Hub
  tab carries a debounced 300ms search input, a Trending /
  Downloads / Stars / Updated sort dropdown, a card grid, and a
  detail drawer with versions list + scan-summary chip + SKILL.md
  README preview. Click Install → SSE-driven 3-stage progress modal →
  toast + Installed-tab refetch on success.
- **`/admin/skills` Installed tab wired to the live curator** —
  replaces the previous static mock import. Each row carries an
  `origin` tag we render as a three-tone badge (bundled / user / hub);
  bundled rows have a disabled Delete button with a "ships with
  corlinman" tooltip and the server enforces the same gate as a
  defence-in-depth check.
- **`system/skill_hub/` server module** — `client.py` (async httpx
  with per-instance circuit breaker on `X-RateLimit-Remaining` +
  `Retry-After`, 60s LRU+TTL cache for list/search, 5min for detail)
  and `installer.py` (download → tarball verify → path-traversal +
  size-cap guards → extract under
  `<data_dir>/profiles/<slug>/skills/<hub-slug>/` → sidecar write →
  audit log). Configurable via `CORLINMAN_SKILL_HUB_BASE_URL` for
  air-gapped mirrors.
- **Eight `/admin/skills/hub/*` endpoints** — `search`, `featured`,
  `skills/{slug}`, `skills/{slug}/file`, `install` (POST → 202),
  `install/{id}` (status snapshot), and `install/{id}/events/live`
  (SSE with `event: phase` frames). Offline upstream surfaces as
  `{rows: [], offline: true}` so the UI renders a banner + Retry
  button instead of a thrown error.
- **`.openclaw-meta.json` sidecar** — written next to the extracted
  `SKILL.md` on install, carries `{slug, version, installed_at,
  source, content_hash}`. Uninstall refuses any directory missing
  this file — that's how bundled starter skills (which never get a
  sidecar) stay protected from an `rm -rf` even on UI bypass.
- **Audit log entries** — `skill.installed` (with `slug`, `version`,
  `files_written`) and `skill.uninstalled` (with `slug`) join the
  existing one-click-upgrade + subagent lines in
  `$DATA_DIR/system-audit.log`.
- **88 new i18n keys** across `skills.installed.*`, `skills.origin.*`,
  `skills.hub.*` (en + zh-CN), plus 3 under `playground.skills.hint.*`
  for the low-skill nudge. Both bundles mirror exactly (enforced by
  `satisfies LocaleBundle` + a per-key test that asserts the zh-CN
  value is not equal to its dotted key).
- **Playground low-skill hint** — `/admin/playground/protocol` carries
  a `<PlaygroundSkillsHint>` that fetches the active profile's
  installed-skill count and renders a "browse hub" CTA when fewer
  than 5 skills are loaded.
- **Operator-facing docs at `docs/skill-hub.md`** — what is it,
  layout (`<data_dir>/profiles/<slug>/skills/<name>/` + sidecar),
  admin UI walkthrough + curl recipe + audit log, ClawHub API
  summary with rate limits + offline behaviour, safety guarantees,
  the 16 bundled starter skills, and a troubleshooting section for
  `HubUnavailableError` / `SkillAlreadyInstalledError` /
  `UnsafeTarballError`. `docs/quickstart.md` cross-links it.

### Changed

- **`<HubTab>` watches `response.offline === true`** rather than
  catching exceptions, so the banner reflects the proxy's
  documented offline contract (no stale-cache fallback — Retry only).
  Locked in per the W1.4 design decision recorded in the plan.

### Security

- **Path-traversal guards** at the tarball-member layer — refuses
  `..` segments, absolute paths, and symlinks on every entry before
  any bytes hit disk.
- **25 MiB total / 10 MiB per-file caps** prevent zip-bombs from
  exhausting the gateway. The streaming extractor aborts with
  `UnsafeTarballError` the moment either cap trips.
- **Sidecar-gated uninstall** — `uninstall_skill` refuses any
  directory missing `.openclaw-meta.json`. Bundled starters never
  get a sidecar, so even a compromised admin session can't `rm -rf`
  the in-wheel defaults.
- **`DELETE /admin/skills/{name}` returns 409** on bundled rows. UI
  disables the button client-side; the server check is the
  authoritative gate.

---

## [Unreleased] — multi-agent dispatch

> The main model can now pick a topic-specific agent on the fly —
> `subagent.spawn` grows `subagent_type / description / run_in_background
> / model` fields, and a new built-in `general-purpose` card fills in
> when no name is given. The agent registry is a three-tier stack
> (built-in / `$DATA_DIR/agents/` / `./.corlinman/agents/`, last wins),
> so operators can author their own cards without committing to the
> repo. Background dispatch mirrors the one-click upgrade pattern: the
> tool returns a `request_id` immediately and the child runs detached,
> bubbling events via the existing `BubbleEmitter`. On terminal state
> a synthetic `user`-role notification lands in the parent journal so
> the next turn sees the result.
>
> Operator surfaces: `/admin/agents` grows source badges + a Create
> modal + Delete gating; `/admin/playground/protocol` gets an
> `<AgentPicker>` (auto-route by default); a new `/admin/subagents`
> page is an SSE-driven live table with per-row Kill and a click-row
> drawer that reuses the live `<EventTimeline>`.
>
> Caps: 3 children per parent, 15 per tenant, max depth 2, 60s wall.
> Tool whitelist enforcement is unchanged — child tools ⊆ parent
> tools. The wildcard `"*"` is honoured only on a card's
> `tools_allowed`; caller-side `tool_allowlist` rejects `"*"`
> literally. Plan at
> [`docs/PLAN_MULTI_AGENT.md`](docs/PLAN_MULTI_AGENT.md); operator
> deep-dive at [`docs/multi-agent.md`](docs/multi-agent.md).

### Added

- **`subagent.spawn` tool** extended with `subagent_type`, `description`,
  `run_in_background`, and `model` fields. The main model can now dispatch
  topic-specific agents from the registry (researcher, editor, mentor,
  ...) or fall through to the new `general-purpose` card. Setting
  `run_in_background: true` returns a `request_id` immediately and the
  child runs detached; a synthetic `user`-role notification lands in
  the parent journal on terminal state.
- **`general-purpose` built-in agent card** — wildcard `tools_allowed:
  ["*"]` semantics: child inherits the parent's full tool set, subject
  to the existing escalation check.
- **Three-tier agent registry** — built-in (repo `agents/`) + user
  (`$DATA_DIR/agents/`) + project (`./.corlinman/agents/`). Last wins;
  shadows logged.
- **Markdown-with-frontmatter card format** — recommended for new
  agents; legacy YAML still parses. Unknown fields (`maxTurns`,
  `background`) silently dropped.
- **Five `/admin/subagents` endpoints** — list, status, per-child SSE,
  global overview SSE, kill.
- **Three `/admin/agents` CRUD endpoints** — create / delete / reload.
- **`AsyncSubagentDispatcher`** + `SubagentTaskStore` mirroring the
  one-click upgrade pattern. Cap = 15 in-flight per tenant; state
  persisted at `$DATA_DIR/.subagent-state.json` with atomic JSON writes.
- **`<AgentPicker>` on `/admin/playground/protocol`** — auto-route or
  explicit pin. Threads `agent_id` into the chat request body so the
  backend's `_peek_agent_binding` honors operator intent over the
  heuristic.
- **`<CreateAgentModal>` on `/admin/agents`** — name regex + format
  radio + clone-from dropdown + force-override-built-in checkbox.
- **`/admin/subagents` live activity panel** — SSE-driven table with
  state pills, elapsed counter, Kill button, click-row drawer with
  per-child `<EventTimeline mode="live">`.
- **Audit log entries** — `subagent.dispatched / .completed / .failed
  / .killed` join the existing one-click-upgrade lines in
  `$DATA_DIR/system-audit.log` and surface in `/admin/system`.
- **i18n** — ~40 new keys across `agents.create.*`, `agents.source.*`,
  `subagents.*`, `playground.agentPicker.*` (en + zh-CN).

### Changed

- `_peek_agent_binding` prefers `start.extra["agent_id"]` over the
  message-peek heuristic when set. Unknown id logs a warning and falls
  back to the heuristic — backwards compatible.
- Sidebar — new `Sub-agents` entry (icon `GitFork`) between `/logs` and
  `/credentials`.
- `AgentCardRegistry.load_from_dir_stack(dirs)` replaces the single-dir
  loader. The gateway boots the registry from the three-tier stack and
  the `POST /admin/agents/reload` endpoint flushes it.

### Security

- Tool whitelist enforcement: child tools ⊆ parent tools. Wildcard
  `"*"` is honored ONLY on the card's `tools_allowed`; caller-side
  `tool_allowlist` rejects `"*"` literally (no widening attack).
- Background dispatcher rejects requests over the per-tenant cap
  (default 15) with a clear sentinel rather than queueing.
- `DELETE /admin/agents/{name}` returns 409 on built-in cards. The
  UI disables the button on `built-in`-badged rows; the backend
  enforces it even if the UI is bypassed.

---

## [Unreleased] — one-click upgrade

> The `/admin/system` page goes from "copy these commands and paste them
> in your VPS shell" to "click Upgrade, type the tag to confirm, watch the
> live progress panel." No more tab-switching to a terminal. Two
> privileged paths under the hood, picked by `CORLINMAN_RUNTIME_MODE`:
>
> - **Docker** — `DockerUpgrader` opens `/var/run/docker.sock`, pulls
>   the new image, recreates the corlinman container (compose CLI
>   preferred, SDK mirror as fallback). Opt-in via
>   `install.sh --enable-one-click-upgrade` because the socket mount is
>   root-equivalent on the host.
> - **Native systemd** — `NativeUpgrader` writes
>   `$DATA_DIR/.upgrade-request`; `corlinman-upgrader.path` watches it,
>   fires `corlinman-upgrader.service` (Type=oneshot, User=root) which
>   validates the tag against GitHub's release list and calls
>   `install.sh --upgrade --version vX.Y.Z`. Always installed by
>   `install_native()` — no flag.
>
> Safety: admin session cookie + typed-confirmation dialog (operator
> must retype the exact tag) + tag whitelisted against GitHub releases
> + no downgrade by default + single in-flight + structured audit log
> in the UI. Plan at
> [`docs/PLAN_ONE_CLICK_UPGRADE.md`](docs/PLAN_ONE_CLICK_UPGRADE.md);
> ops doc cross-link from `docs/system-updates.md`.

### Added

- **One-click upgrade UI** on `/admin/system` — primary "Upgrade to
  vX.Y.Z" CTA replaces the copy-paste tabs as the recommended action.
  Manual upgrade commands stay accessible as a collapsed accordion.
- **`<UpgradeConfirmModal>`** — Dialog gating the upgrade behind
  typed-confirmation (operator must type the exact tag for the Upgrade
  button to enable). Inline 409 surfaces the in-flight request_id
  without closing the modal.
- **`<UpgradeProgress>`** — SSE-driven progress panel: phase pills
  (validating → pulling → recreating → healthcheck → done), elapsed
  counter, live log tail, terminal success/failure banners with
  auto-reload-in-5s on success. EventSource with 2s polling fallback
  for environments where SSE is blocked.
- **`<AuditCard>`** — paginated `system-audit.log` reader at the
  bottom of `/admin/system`. Newest-first table with relative
  timestamps, color-coded event badges, expandable details JSON,
  cursor-paginated "Load more".
- **`POST /admin/system/upgrade`** — 202 starts the upgrade, 400 on
  typed_confirmation mismatch, 503 if upgrader unavailable, 400 on
  downgrade refusal, 409 on in-flight collision. 1/min server-side
  rate limit.
- **`GET /admin/system/upgrade/{request_id}/status`** — read-once
  snapshot of an upgrade.
- **`GET /admin/system/upgrade/{request_id}/events`** — SSE stream
  with `event: status` frames + 10s keepalive.
- **`GET /admin/system/audit`** — paginated audit log API.
- **`UpgraderProtocol`** + `DockerUpgrader` + `NativeUpgrader` impls
  in `corlinman_server/system/upgrader/`. Docker side opens the
  socket lazily (no import of `docker` at module load time); native
  side talks to systemd via a file-watched helper.
- **`deploy/corlinman-upgrader.sh`** — privileged one-shot helper.
  Validates JSON schema, semver-regex on tag, UUID-regex on
  request_id, live GitHub release whitelist via `curl + jq`, sort-V
  downgrade gate, atomic status writes. `UPGRADER_ALLOW_DOWNGRADE=1`
  override for emergency rollbacks.
- **`corlinman-upgrader.{path,service}` systemd units** — rendered by
  `install_native()` alongside the main corlinman.service. The path
  unit watches `$DATA_DIR/.upgrade-request` and triggers the
  oneshot service.
- **`install.sh --enable-one-click-upgrade`** — Docker-mode flag.
  Mounts `/var/run/docker.sock` RW and adds the in-container
  corlinman user to the host's `docker` group (auto-detected GID).
- **`CORLINMAN_RUNTIME_MODE` env** — set to `native` by the
  systemd unit, `docker` by the compose env. The gateway
  `resolve_upgrader()` reads this to pick the right impl.
- 12-key i18n block under `system.upgrade.{confirm,progress,phases,
  succeeded,failed,stalled,manual}` + `system.audit.*` (en + zh-CN).
- 56+ new tests (W1.1 docker upgrader: 23; W1.2 native + bash: 11
  bash + 8 python; W1.3 endpoints: 8; audit log: 8; AuditCard
  component test: 3 + W2.1 modal/progress as built manually).

### Changed

- `/admin/system` page restructured: when an update is available, the
  primary "Upgrade to vX.Y.Z" button is the prominent CTA; the
  existing "Manual upgrade — copy these commands" tabs become a
  collapsed accordion below.
- `system.upgrade.note` and surrounding strings are reused — no
  in-page i18n breakage.

### Security

- The endpoint chain enforces typed-confirmation + tag whitelist + no
  downgrade + single in-flight. A compromised admin session at worst
  pulls a real upstream corlinman release — there is no path to
  install an arbitrary image or arbitrary ref.
- The native helper script never trusts the JSON file's content
  unvalidated — semver regex, UUID regex, and a live GitHub release
  list check all gate the call to `install.sh`.
- Audit log records every state transition (`system.upgrade.requested
  / .started / .completed / .failed`) with the actor + tag + details
  before the upgrade itself starts.

---

## [1.2.0] — 2026-05-25 — `/admin/system` + auto-update + observability overhaul + admin UI fixes

> Big release. Three threads land together:
>
> 1. **Auto-update detection** (this section below) — gateway polls GitHub
>    releases, surfaces a TopNav bubble + `/admin/system` upgrade page with
>    sanitized release notes and copy-paste upgrade commands.
> 2. **Task observability overhaul** (subsection further down) — typed
>    event taxonomy + SSE live timeline + tool widgets + cost footer +
>    sub-agent tree, replacing the prior "agent runs, user can't see what
>    it's doing" gap.
> 3. **Admin UI fixes** (folded in) — `/admin/sessions/{detail,turn}`
>    query-string routes (replaces dynamic `[key]` that broke `output:
>    "export"`), provider test endpoint, model-picker dialog, hermes-style
>    credentials page, channel reply chunking (no more `[…回复过长,已截断]`).
>
> Release notes are grouped by thread.

### Auto-update detection

> The gateway now knows when a new release ships and tells the operator.
> A 30s-polling `<UpdateBubble>` in the admin TopNav lights up amber when
> the latest GitHub release tag outranks `importlib.metadata.version`;
> clicking it lands on `/admin/system`, which renders sanitized release
> notes plus copy-paste upgrade commands (Native / Docker / Docker + QQ).
> No in-app one-click upgrade — the gateway can't sudo into the host —
> but the operator-driven flow is now first-class instead of "check the
> repo by hand." Plan at
> [`docs/PLAN_AUTO_UPDATE.md`](docs/PLAN_AUTO_UPDATE.md); operator doc
> at [`docs/system-updates.md`](docs/system-updates.md).

### Added

- **`<UpdateBubble>` in the admin TopNav** — quietly polls
  `/admin/system/info` every 30s; renders an amber chip with the new
  tag when one is available; dismissable per-tag via `localStorage`
  (the chip stays hidden for that tag, reappears on the next release).
- **`/admin/system` page** — three cards: current vs. latest version
  with deploy-mode hint (`docker` / `native`, sniffed from env),
  sanitized release-notes markdown (`react-markdown` + `rehype-
  sanitize`), and tabbed upgrade commands (Native / Docker / Docker +
  QQ) with copy buttons. Sidebar entry **System** under the settings
  group (icon: `MonitorCog`).
- **Three admin endpoints** —
  `GET /admin/system/info` (current `UpdateStatus` + deploy mode),
  `POST /admin/system/check-updates` (force-poll, server-side rate-
  limited to 1/min, returns fresh `UpdateStatus`),
  `GET /admin/system/upgrade-commands` (returns
  `{native, docker, docker_with_qq}` strings pre-filled with the
  target tag).
- **`UpdateChecker`** — polls
  `api.github.com/repos/ymylive/corlinman/releases/latest` with stored
  `If-None-Match` so a no-change poll costs zero against the GitHub
  rate-limit budget. 6h TTL, semver compare via
  `packaging.version.Version`, optional `CORLINMAN_GITHUB_TOKEN` for
  higher rate limits, prerelease channel opt-in.
- **`[system.update_check]` config stanza** in
  `docs/config.example.toml` — `enabled` / `interval_hours` /
  `include_prereleases` / `repo` / `github_token`, fully commented.
- **`system.update_check` scheduler builtin** — registered with the
  scheduler tool registry but pending a lifespan `scheduler.spawn()`
  wire-up; in the meantime `<UpdateBubble />`'s 30s poll and the on-
  page-load fetch on `/admin/system` keep detection live whenever an
  admin tab is open.
- **30 i18n keys** across `system.*` and `update.bubble.*` (`en` +
  `zh-CN`).
- **`docs/system-updates.md`** — operator-facing doc covering
  configuration, security model, GitHub rate-limit math, air-gapped
  deploys, and troubleshooting. `docs/quickstart.md` cross-links it
  from the "Watching the agent work" section.

### Changed

- **BREAKING: version unified to `1.1.1`** across the workspace
  `pyproject.toml`, `corlinman-server`'s own `pyproject.toml`, and
  `ui/package.json`. The git tag was already `v1.1.1`; this commit
  collapses the three previous version-of-truth sources so
  `importlib.metadata.version("corlinman-server")` matches the
  deployed tag — which the update checker depends on for the
  current-vs.-latest comparison to be meaningful.
- **`<ReleaseNotes>` renders GitHub release bodies through
  `rehype-sanitize`** — `<script>`, `javascript:` URLs, inline event
  handlers (`onclick=`, …), and `<iframe>`/`<object>`/`<embed>` are
  stripped; a unit test asserts a `<script>` payload in the release
  body doesn't reach the DOM.

---

### Task observability overhaul (shipped in 1.2.0)

> Makes the agent's work visible. Today nobody can see what tools fired
> in a turn, what args went in, what came back, how long anything took,
> or what happened on a turn 10 minutes ago — even though the gateway
> collects most of that data. This release ports proven UX patterns
> from Claude Code, opencode, and hermes-agent into a single typed
> event stream that drives both the admin UI and the channel adapters.

### Added

- **Typed `EventEnvelope` event stream** — 14 events
  (`TurnStart` / `BlockStart` / `TextDelta` / `ReasoningDelta` /
  `ToolInputDelta` / `BlockStop` / `ToolStateRunning` /
  `ToolStateHeartbeat` / `ToolStateCompleted` / `SubagentSpawned` /
  `SubagentEvent` / `SubagentCompleted` / `Cancelling` /
  `TurnComplete` / `TurnErrored`) emitted by `ReasoningLoop` +
  `runner_pool` + `subagent.supervisor`. The legacy gRPC `ServerFrame`
  keeps emitting alongside so existing channel adapters and SDK
  consumers don't break.
- **`turn_events` SQLite table** (journal migration `004_turn_events`)
  — every emitted envelope is journaled (`turn_id` / `sequence` /
  `event_type` / `payload_json` / `timestamp_ms`). Replays from this
  table render identically to the live stream. TTL prune at boot +
  daily; configurable via `CORLINMAN_TURN_EVENTS_TTL_DAYS` (default 30
  days).
- **Three admin SSE/JSON routes** —
  `GET /admin/sessions/{key}/events/live` (SSE, 10s keepalive,
  `Last-Event-ID` resume + `?last_event_id=…` proxy fallback),
  `GET /admin/sessions/{key}/turns/{turn_id}/events` (paginated JSON
  replay), `GET /admin/sessions/{key}/cost` (aggregated cost / turn
  count / tool-call total).
- **`/admin/sessions/{key}` event timeline** — live SSE-driven turn
  cards. `ReasoningBlock` shimmer while streaming; `ToolWidget` with
  pending → running → completed/error state machine, live-ticking
  elapsed counter, expandable args + result through per-tool renderers
  (`bash` / `read_file` / `write_file` / `webfetch` / `grep` /
  fallback `generic`). rAF-batched merges so a fast-streaming turn
  doesn't tank rendering.
- **`/admin/sessions/{key}/turns/{turn_id}` drill-down** — same
  timeline component in replay mode, seeded from the JSON replay
  endpoint. Top-of-page `TurnSummaryCard` with elapsed / tool count /
  cost / finish reason.
- **Sticky cost footer** — five pills (total USD, turn count, avg
  turn time, tool calls, last-turn-N-ago); 15s polling + a
  `visibilitychange` refetch on tab focus. Session list grows three
  columns (total / avg / last tool used).
- **Sub-agent tree** — `BubbleEmitter` bubbles child envelopes into
  the parent stream; the UI renders the child's events nested inside
  the spawning tool widget, depth cap 3.
- **Tool heartbeat** — `ToolStateHeartbeat` fires every 10s while a
  tool runs so a `sleep 60` no longer leaves the UI quiet (configurable
  via `CORLINMAN_TOOL_HEARTBEAT_INTERVAL_MS`).
- **Channel post-turn footer + cancel/heartbeat consumer** — channel
  `_status.py` now subscribes to `EventEmitter` directly. Heartbeats
  refresh the spinner with `🔧 {tool} … {elapsed_s}s`; cancellation
  shows `⏹ 正在取消…` within ~1s instead of waiting for the next round;
  every reply gets a one-line footer `(elapsed: 12.4s · 3 tool calls ·
  ~$0.012)` (the `~` drops to `$` when `cost_status == "billed"`).
- **`ui/tests/e2e/task-observability.spec.ts`** — Playwright spec
  covers the live timeline (reasoning, two tool widgets, expand-to-
  see-args, cost footer pills) plus the drill-down replay.
- **Docs** — `docs/observability.md` now leads with the task event
  stream (taxonomy table, API endpoints with curl examples,
  configuration env vars); `docs/quickstart.md` gains a "Watching the
  agent work" section.

### Changed

- **`Cancelling` event is emitted the moment `ReasoningLoop.cancel()`
  is called** — previously the user had to wait for the next reasoning
  round to see anything change. Same emit point now feeds the UI
  badge + the channel spinner.

---

> Admin UI fixes — credentials, model picker, sessions navigation.
> Reconciles a split-brain state between `main` and the live
> deployment at `corlinman.cornna.xyz` (legacy endpoints existed on
> live but never landed in main; new observability endpoints exist in
> main but not yet on live), then ports the hermes `EnvPage` paste-
> only credentials pattern + two-column `ModelPickerDialog`. Plan at
> [`docs/PLAN_UI_FIXES.md`](docs/PLAN_UI_FIXES.md).

### Added

- **Provider test-connection endpoint** —
  `POST /admin/providers/{name}/test`. Zero-cost probe: hits
  `/v1/models` for openai-compatible kinds; returns
  `ok=true` + `note` for anthropic / google (no free probe surface).
  Latency capped at 5s; the api key is never echoed in the response or
  the access log. UI surfaces it as a per-row "Test connection" button
  with toast feedback.
- **Provider model discovery endpoint** —
  `GET /admin/providers/{name}/models`. Proxies upstream `/v1/models`
  for openai-compatible providers, returns a hardcoded list from
  `corlinman_providers.specs` for anthropic / google. 30s in-memory
  cache. Feeds the new `<ModelPickerDialog>`.
- **Provider kinds descriptor endpoint** —
  `GET /admin/providers/kinds`. Returns
  `{kinds: [{kind, label, description, params_schema}]}` so the custom-
  provider creation form can render itself from JSON-Schema instead of
  hard-coding the per-kind shape.
- **Session turns listing endpoint** —
  `GET /admin/sessions/{key}/turns?limit=50&before_id=...`. Paginated
  cursor over the `turns` SQLite table. Powers the past-turns pill row
  above the EventTimeline so the session detail page is reachable
  beyond deep links.
- **Credential reveal endpoint** —
  `GET /admin/credentials/{provider}/{key}/reveal`. Admin-only, auth-
  gated, return body redacted in access log. Backs the eye-icon UX on
  the credentials page.
- **Session replay endpoint backported** —
  `POST /admin/sessions/{session_key}/replay` with
  `{mode: "transcript" | "rerun", since_turn_id?}`. Lives on the live
  deployment today; brought back into main so a redeploy doesn't
  regress the existing `<ReplayDialog>` consumer.
- **`<ModelPickerDialog>`** — two-column provider / model picker with
  a single search filter (port of hermes-agent's
  `ModelPickerDialog.tsx`). Mounted on `/admin/models` (add-alias) and
  `/admin/agents/[name]` (per-agent model override).
- **`<EnvVarRow>` + `<ProviderGroupCard>`** — hermes-style credentials
  UI: paste-only secret input, eye-icon reveal with per-row client-
  side cache (toggle doesn't re-fetch), replace / clear buttons,
  prefix-grouped collapsible cards.
- **`<PastTurnsPills>`** — horizontal turn navigator above
  `EventTimeline` on `/admin/sessions/{key}`. ≤10 pills with
  `(turn_id, status, elapsed)`, "Load more" pagination.
- **`<TestConnectionButton>`** — per-provider one-click probe with
  toast feedback (latency on success, upstream error message on
  failure).
- **E2E smoke** — `ui/tests/e2e/admin-pages-smoke.spec.ts` visits
  seven admin surfaces (`sessions`, `logs`, `providers`, `credentials`,
  `models`, `agents`, session detail), fails on 404 XHRs and console
  errors. Catches "UI calls missing endpoint" regressions before
  deploy.

### Changed

- **BREAKING:** `GET /admin/providers/kinds` response shape changed
  from `{kinds: [string]}` to
  `{kinds: [{kind, label, description, params_schema}]}`.
  `<AddCustomProviderModal>` migrated; downstream consumers reading
  just the `kind` string need to map over the new array.
- **`/admin/providers/{name}/test` for anthropic / google** returns
  `ok=true` plus a `note` flag rather than a real round-trip. Those
  vendors don't expose a free models endpoint without a billed token;
  flagging the response keeps the UI honest about what it actually
  verified.
- **`<EnvVarRow>` eye-icon reveal** caches the fetched cleartext per
  row; subsequent toggles render from cache instead of re-hitting
  `/admin/credentials/{provider}/{key}/reveal`. Cache scope is the
  component instance; navigating away clears it.

### Fixed

- **`/admin/sessions/{key}` had no way in beyond deep links.** The
  detail page assumed you arrived with a `turn_id` in the URL.
  The past-turns pill row above the timeline now exposes every turn
  in the session, paginated.
- **Live deployment regressed when consuming a stale UI bundle** —
  the live UI calls SSE / cost / replay endpoints that the live
  backend either didn't ship yet (new) or shipped under a different
  path (legacy). Documented the deployment ordering in
  [`docs/observability.md`](docs/observability.md) §"Admin UI fixes
  (May 2026)".



> 4 commits on top of v1.1.0. Focuses on the per-turn hot path
> (~500-800 ms shaved off a 10-round task), adds hermes-agent-style
> auto-resume of in-progress turns at gateway boot, and tightens the
> live status streaming so a `todo_write` no longer hides the
> current tool being called.

### Added

- **Hermes-style auto-resume at boot** — when the gateway / agent
  process starts, `AgentResumeService` scans the journal for
  `in_progress` turns within a 10-minute window, sweeps anything
  older to `errored`, and either lets the channel's existing inbox
  drain re-deliver (QQ family) or seeds a fresh `pending` inbox row
  with `message_id="resume:<turn_id>"` for future channel drains
  (Telegram / Discord / Slack / Feishu). The chat handler's
  `find_resumable_turn` matcher then replays the journaled
  `(tool_call, tool_result)` pairs so the agent picks up where it
  left off. Boot log line: `agent.resume.scan_complete found=N
  resumed=M skipped=K window_minutes=10`.
- **`channel` column on `journal_turns`** — SQLite gets an
  idempotent `ALTER TABLE` at next open; Postgres gets
  `migrations/journal_postgres_v3.sql` (also inlined as a no-op
  `IF NOT EXISTS` so fresh deployments don't need a separate
  migration step).

### Changed

- **Telegram spinner keeps the op-flow line visible under the todo
  list.** Previously when the agent called `todo_write`, the
  placeholder switched to showing JUST the checkbox list and the
  user lost visibility of the current tool. Now the placeholder
  shows both, separated by a blank line:
  ```
  📋 任务清单 (1/4):
  ☑ Search market data
  ▣ Drafting decision memo
  ☐ Build chart

  🔧 web_search  'gpt-5.5 news'
  ```
- **QQ-family summary block drops the ☐ pending todo list.** QQ /
  QQ-official / WeChat-official can't edit messages, so a list of
  pending future work appearing in the reply preamble is visual
  noise. The block reverts to the legacy `📋 本次操作:` header with
  just the operation log (`✅ web_search …`, `📎 已发送文件 …`).
  The `format_todo_list` helper stays — Telegram + other edit-
  capable channels still use it.

### Performance

- **`_builtin_tool_schemas()` cached at module load** — the 13-tool
  schema list was rebuilt every round. Now resolved once into
  `_CACHED_BUILTIN_TOOL_SCHEMAS` and reused. Saves ~30-50 ms × N
  rounds (potentially ~500 ms on a 10-round task).
- **`ReasoningLoop._estimate_tokens` incremental cache** — was
  walking the entire message list every round (O(N) per call,
  effectively O(N²) over a long task). Now keeps a running
  character total + invalidates on compaction / list shrink /
  seed-message mutation. Saves ~5-15 ms × N rounds.
- **`AgentJournal.append_messages` batched transaction** — the
  `(assistant tool_call, tool_result)` pair was two separate
  `BEGIN IMMEDIATE` / `COMMIT` cycles per tool call. Now one
  transaction wraps both inserts. Saves ~5 ms × tools-per-round.
- **`SkillRegistry.refresh()` 30-second debounce** — was
  `rglob() + stat()`-ing every `.md` file on every turn. Now
  gated by a monotonic interval (env-overridable via
  `CORLINMAN_SKILL_REFRESH_INTERVAL_MS`, default 30 000). Saves
  ~5-10 ms / turn after the first turn.
- **Workspace snapshot drops the `rev-parse` subprocess** —
  `_snapshot.snapshot()` was forking three times (`git add` +
  `git commit` + `git rev-parse`). The third call is now replaced
  by a direct `.git/HEAD` parse (handles `ref:` indirection +
  loose refs + `packed-refs` fallback). Saves ~2-3 ms / turn.

### Fixed

- **gRPC client message-size limits asymmetric with server.** The
  agent server set `max_send_message_length = max_receive_message
  _length = 64 MB`, but the client at `corlinman_grpc.agent_client
  .connect_channel` left both at gRPC's 4 MB default. Large tool
  results (>4 MB shell output / file reads) silently failed with
  `RESOURCE_EXHAUSTED` despite the server happily sending them.
  Client now mirrors 64 MB on both sides.

## [1.1.0] — 2026-05-24 — channel parity + Claude-Code-style task UX

> 10 commits on top of v1.0.0. Brings the new chat channels to feature
> parity with Telegram (status streaming + file replies), adds two
> brand-new channels (QQ official bot + WeChat 公众号), fixes the
> session-management page (it was reading from the wrong store),
> simplifies the admin UI by ~16 pages, ports Claude Code's summary-
> based context compaction + mid-turn user-message injection, and
> renders the agent's task list as a live ☑/▣/☐ checkbox view.

### Added

- **QQ 官方机器人 channel** — Tencent 官方 bot platform (api.sgroup.qq.com).
  WebSocket gateway + REST sender + Ed25519 webhook sig + access-token
  single-flight refresh. Image attachments via `send_attachment`; non-
  images render an explanatory line (platform limitation).
- **微信公众号 channel** — webhook with sha1 signature verification +
  4.5 s passive-reply window with automatic fallback to customer-
  service messages over the 48 h reply window. Temp-media upload for
  image / voice replies. AES encryption is a documented v1 gap.
- **Discord / Slack / Feishu mutable-spinner status** — the three
  channels now render the same Telegram-style "🧠 思考中 → 🔧 调用工具
  → ✅ 完成 → ✍️ 生成回复 → final reply" mutable placeholder, with
  per-channel file uploads via `send_attachment` (Discord 25 MiB
  multipart, Slack `files.upload`, Feishu two-step `/im/v1/files`).
- **QQ tool-activity summary block** — QQ can't edit messages, so when
  a turn used ≥1 tool the agent's reply is now prepended with a
  compact `📋 本次操作: …` block listing every tool call + duration +
  outcome + file uploads. Env-gated via `CORLINMAN_QQ_TOOL_SUMMARY=0/1`.
- **Hermes-style detailed status** — Telegram spinner now shows arg
  previews (`🔧 web_search 'gpt-5.5 news'`), durations
  (`✅ web_search (302ms)`), errors (`❌ run_shell 失败 (42ms): perm…`),
  and reasoning deltas (`💭 推理: …` lines from Anthropic thinking
  blocks + DeepSeek-R1 reasoning_content). Mirrors hermes-agent's
  `_last_activity_desc` mutable spinner line.
- **`send_attachment` everywhere** — Discord, Slack, Feishu, QQ-official
  joined the existing Telegram + QQ-OneBot support. The agent calls
  `send_attachment(path=...)` and each channel picks the right transport.
- **Live task-list rendering** — `todo_write` tool calls now render as
  `📋 任务清单 (3/5): ☑ Search… ▣ Drafting… ☐ Build…`. Telegram
  spinners edit in place; QQ / QQ-official / WeChat prepend the final
  snapshot to the reply.
- **Claude-Code-style context compaction** — when token estimate ≥ 95 %
  of `CORLINMAN_CONTEXT_BUDGET` the reasoning loop now runs a
  summarization sub-call (same model, no tools, ≤1500 output tokens),
  replacing older messages with one synthetic system block:
  `PRIOR CONVERSATION SUMMARY: …`. Failure falls back to the existing
  elision path. The naive elision threshold dropped from 100 % to 60 %
  of budget so it fires earlier.
- **Mid-turn user-message injection** — while the agent is processing
  turn N for session-key X, a NEW message arriving for the same
  session is INJECTED into the running turn as additional user
  context (Claude Code's "supplemental message" UX). The second RPC
  returns `Done(finish_reason="supplemented")` and the channel
  silently keeps the typing indicator alive; no parallel turn is
  spawned. New `HookEvent.UserSupplemented` event fires for audit.
  `ReasoningLoop.inject_user_message(text)` is the public surface.
- **AgentJournal session APIs** — `list_session_summaries(*, limit)`
  + `delete_session(session_key)` on both the SQLite and Postgres
  backends. Aggregates chat history per session, returns
  `(session_key, first_seen, last_seen, turn_count, message_count,
  last_user_text, last_status)`. The Sessions admin page now reads
  this surface and operators can finally see + delete real chat
  history.
- **Sessions admin page rework** — Delete per row + Clear-all button
  + AlertDialog confirmations + last-seen column + empty-state copy.
  `DELETE /admin/sessions/{session_key}` and `DELETE /admin/sessions`
  routes on the backend with audit logs.
- **`useDevMode()` hook + Developer Settings page** — admin sidebar
  now shows 10 operator items by default with a toggle on
  `/admin/dev-settings` to surface the 11 developer-only pages (Config,
  Tenants, Credentials, Agents, Skills, Plugins, RAG, Profiles,
  Evolution, Hooks, Nodes). Preference persists in `localStorage`
  (`corlinman.devMode.v1`).
- **Per-channel concurrency cap** — every chat channel now caps
  in-flight turns at `CORLINMAN_<CHANNEL>_MAX_CONCURRENCY` (default 8),
  preventing a 100-message burst from spawning 100 parallel LLM
  streams.
- **gRPC keepalive aligned** — client + both server bind sites use the
  same `keepalive_time_ms=30s` + `max_ping_strikes=0` to stop the
  intermittent "UNAVAILABLE: Too many pings" on long agent turns.

### Changed

- **Sidebar trimmed** — removed 6 niche admin pages
  (`embedding`, `tagmemo`, `canvas`, `diary`, `characters`,
  `federation`) along with their backend routes. ~9 400 lines deleted.
  Provider-runtime embedding code is unaffected (just the deleted
  admin UI for it).
- **`JournalBackend.find_resumable_turn` / `begin_turn`** gained a
  `user_id` kwarg so group-chat members can't replay each other's
  tool side effects (default preserves legacy single-user behavior).
- **Sessions route data source** — `GET /admin/sessions` now reads
  from `agent_journal.sqlite` (the source of truth) instead of the
  unused legacy `sessions.sqlite` (which has been empty since 0.7.x).
  Legacy file is still consulted as a fallback if the journal is
  unavailable.

### Fixed

- **`/admin/sessions` returned empty** because it was reading the
  wrong store; see "Changed" above.
- **Long tasks loop until `_MAX_ROUNDS`** because the old elision-only
  compaction kept feeding the same `tool_calls` skeletons to the
  model. Summary-based compaction collapses redundant retries into a
  single sentence so the model has room to plan.
- **Discord / Slack / Feishu had no typing-indicator parity** — now
  fired (Discord `/typing`; Slack stub for missing-API; Feishu stub).

### Removed

- Admin UI pages: `embedding`, `tagmemo`, `canvas`, `diary`,
  `characters`, `federation`. Matching backend admin routes too.

## [1.0.0] — 2026-05-24 — Python port complete + production-ready edge

> Major release. Cuts the umbilical to the Rust gateway and finishes the
> Python port that started in the 0.6.x line. Adds Telegram + three more
> chat channels, real-time status streaming, file replies, multi-gateway
> HA via shared Postgres, a pluggable hook event bus, context-aware
> permissions, and hardens every I/O edge (SSRF + sandbox + reactive
> token refresh). 128 commits since `v0.6.8`.

### Added

- **Telegram channel** — long-poll bot adapter for private + group
  chats with keyword filter, `require_mention_in_groups`, allowed-
  chat allowlist, and graceful 429 back-off on the decorative
  endpoints.
- **Discord / Slack / Feishu channels** — text-only adapters with the
  same router + rate-limit + chat-service plumbing as QQ + Telegram.
- **Real-time status streaming** — Telegram clients see a live "is
  typing…" indicator + a placeholder that edits in place as the agent
  runs tools (`🧠 思考中... → 🔧 调用工具: write_file → 📎 已发送文件
  → ✍️ 生成回复中... → final reply`). QQ private chats get NapCat's
  `set_input_status` indicator. Mirrors hermes-agent's
  `_last_activity_desc` mutable spinner.
- **`send_attachment` builtin tool** — agent can reply with files
  (HTML / PDF / images / voice) instead of dumping raw text.
  Telegram picks document / photo / voice by MIME; QQ uses NapCat's
  `upload_private_file` / `upload_group_file` extensions.
- **Per-turn journal resume** — `AgentJournal.find_resumable_turn`
  matches a fresh Chat RPC against an in-progress turn (within ~5 min)
  and replays the journaled `(assistant tool_call, tool_result)` pairs
  so a gateway/agent restart picks up where it left off. Resume key
  scoped by `user_id` so group-chat members can't replay each other's
  tool side-effects.
- **`PostgresJournalBackend`** — multi-gateway HA via shared Postgres.
  Race-safe `INSERT ... ON CONFLICT DO NOTHING RETURNING turn_id`
  with a partial unique index on
  `(session_key, user_text, user_id)` WHERE `status='in_progress'`.
  SQLite remains default; switch via `CORLINMAN_JOURNAL_BACKEND=postgres`
  + `CORLINMAN_JOURNAL_POSTGRES_DSN`. Migrations at
  `migrations/journal_postgres_v{1,2}.sql`. asyncpg +
  pytest-postgresql are optional extras.
- **`HookBus` push subscribers** — register `(predicate, callable)` to
  receive `UserPromptSubmit` / `PreToolDispatch` / `ToolCalled` /
  `TurnComplete` / `TurnErrored` events. Sync + async, exception-
  isolated.
- **Context-aware `PermissionGate.decide_with_context(tool, model,
  session_key, user_id)`** with fnmatch rules
  (`{model: "claude-*", user_pattern: "guest*"}`). Legacy
  `decide(tool)` still works.
- **Dynamic skill reload** — `SkillRegistry.refresh()` runs per chat
  turn, picking up new / updated / removed `*.md` from
  `~/.corlinman/skills/` without a restart. Emits
  `agent.skills.refreshed added=... updated=... removed=...`.
- **Reactive 401 refresh** — OpenAI / OpenAI-compatible / Azure /
  Google / Bedrock / DeepSeek / GLM / Qwen all self-heal on env-var
  key rotation. Codex + Anthropic were already self-healing; Codex now
  single-flights via `asyncio.Lock` and serializes RMW of
  `~/.codex/auth.json` with `fcntl.flock`.
- **Durable QQ inbox (`inbox.sqlite`)** — every accepted QQ message
  recorded `pending → dispatched → done/dead`. Boot drainer flips
  stale `dispatched` rows back to `pending`.
- **NapCat heartbeat watcher** — detects bot-QQ kicked offline (>120 s
  silence) with a structured warning naming the ws endpoint.
- **Per-channel concurrency cap** — default 8, env-overridable via
  `CORLINMAN_{QQ,TELEGRAM,DISCORD,SLACK,FEISHU}_MAX_CONCURRENCY`.
- **`SIGTERM` close path** — gateway shutdown drains the Postgres
  pool, aiosqlite WAL, inbox, blackboard, and HookBus before exit.
- **Tier 2 coding tools** — per-turn file-state cache, fuzzy edit
  matcher with staleness guard, token-aware context compaction,
  workspace `git`-backed snapshot + `revert_changes` tool.

### Changed

- **BREAKING:** `JournalBackend.begin_turn(...)` return type is now
  `int | None`. SQLite always returns an int; Postgres may return
  `None` on conflict so the caller re-runs `find_resumable_turn`.
- **BREAKING:** `JournalBackend.begin_turn` + `find_resumable_turn`
  gained `user_id: str | None = None` (default preserves legacy).
- **BREAKING:** Removed the embedded new-api onboard/admin surface.
  `[providers.<name>]` blocks with `kind = "newapi"` migrate silently
  to `kind = "openai_compatible"` at load. The
  `corlinman-newapi-client` package, `/admin/newapi*` router,
  `/admin/onboard/newapi/{probe,channels}` endpoints, and
  `corlinman config migrate-sub2api` CLI helper are gone.
- gRPC keepalive aligned client ↔ both server bind sites
  (`keepalive_time_ms=30s` + `max_ping_strikes=0`) — fixes
  `UNAVAILABLE: Too many pings` on long agent turns.
- `_builtin:` sentinel namespace extracted to a shared
  `_BUILTIN_OBSERVATION_PREFIX` constant. In-process builtin tools
  now emit observation-only `ToolCall` frames so channel UIs can
  render the mutable spinner without double-feeding `tool_result`s.
- LRU cap (4096 entries, env-overridable
  `CORLINMAN_MAX_SESSION_CACHE`) on `_session_locks` and the cost
  meter's session map.

### Fixed

- Channels passed `dict` to `chat_service.run` causing
  `AttributeError: 'dict' object has no attribute 'model'` on every
  Telegram inbound. Switched to `SimpleNamespace`.
- Telegram typing pulse leak on placeholder send failure (pulse task
  now lives inside the `try/finally`).
- Telegram final `edit_message_text` / `send_message` unwrapped —
  failures now degrade with a warning log instead of stranding the
  placeholder on "✍️ 生成回复中...".
- Telegram `editMessageText` ignored HTTP 429 — now parses
  `parameters.retry_after` into a shared back-off deadline.
- OneBot writer dropped actions on transient WS send failure — now
  requeues to a front buffer and raises for reconnect.
- Telegram long-poll committed `offset` before `put` — could lose
  updates on cancel mid-batch. Now commits post-put.
- OneBot `_inbound_q` blocking put caused WS 1009 + reconnect storm
  under burst — switched to `put_nowait` + drop-oldest.
- Reasoning loop ignored `signal_input_closed` — half-closed bidi
  streams timed out at 30 s instead of terminating promptly.
- Out-of-order `tool_result` envelopes polluted next-round
  collection — now drained + dropped with
  `reasoning_loop.stale_tool_result`.
- aiosqlite BEGIN+ROLLBACK left the connection in an undefined tx
  state, silently no-op'ing subsequent writes. Switched to
  `async with conn:`.
- `send_attachment` size unguarded — added a 45 MiB pre-flight check.
- Built-in tool calls never visible to channels — Telegram status
  placeholder stuck on "🧠 思考中..." the whole turn. Observation-
  only `_builtin:` frames now flow through.
- Heartbeat watcher rendered `None` as the literal "Nones" — split
  into a distinct "received yet" branch naming the ws endpoint.
- Codex `_ensure_fresh` + `_attempt_token_recovery` raced on
  concurrent refresh — now share an `asyncio.Lock`.

### Security

- **`web_fetch` SSRF guard** — `is_safe_host` resolves the host via
  `socket.getaddrinfo` and rejects any IP that's private / loopback /
  link-local / multicast / reserved / metadata
  (`169.254.169.254` / `fd00:ec2::254`). Manual 5-redirect loop re-
  validates each hop. Dev-only override
  `CORLINMAN_WEB_FETCH_ALLOW_PRIVATE=1` (never opens the metadata
  endpoints).
- **`run_shell` sandbox** — POSIX `RLIMIT_CPU=60s`,
  `RLIMIT_FSIZE=100 MiB`, `RLIMIT_NPROC=64`, `RLIMIT_NOFILE=256`,
  `RLIMIT_AS=2 GiB` (Linux). `setsid()` + `os.killpg(SIGKILL)` so
  shell-spawned forks die with the parent. Minimal env whitelist
  (no provider keys / gRPC creds reach the subprocess). Hard
  timeout cap lowered from 120 s → 60 s.
- **Coding-tool symlink escape** — `resolve_in_workspace` walks each
  ancestor with `os.lstat`, refusing symlink components. Every write
  site opens with `O_NOFOLLOW`, catching the TOCTOU race at the
  syscall layer.
- `_codex_oauth.persist_codex_credential` now holds `fcntl.flock`
  around its read-modify-write window so the Codex CLI + gateway
  can't garble `auth.json`.

### Removed

- `corlinman-newapi-client` package and the `/admin/newapi*` surface.

## [0.7.1] — 2026-05-17 — warm pool

Adds the warm-pool surface that v0.7.0 deferred. Architectural note:
the Rust gateway talks gRPC to a long-running Python servicer, so the
literal OpenClaw "container per session" doesn't apply. Instead the
pool ships Python-side with a boot-time pre-warm hook so the upstream
provider SDK's auth handshake happens before the first user chat,
not on the user-facing hot path.

### Added

- **`corlinman_server.runner_pool.RunnerPool[T]`** — bounded warm
  pool with `max_warm_per_key` + `max_active_total` and oldest-idle
  eviction. Generic on the pooled type; ships with provider warming
  as the first caller, designed to grow to per-tenant / sandboxed
  resources in v0.8.
- **`CorlinmanAgentServicer.prewarm_providers(model_names)`** —
  resolve each model alias at boot, park the result warm. Failures
  log and skip (best-effort; the cold path stays intact).
- **`pool_stats()`** accessor for operator tooling.
- env: `CORLINMAN_RUNNER_POOL_WARM` (default 2),
  `CORLINMAN_RUNNER_POOL_MAX` (default 8).

### Added (v0.7.0 hygiene)

- 4 v0.7 smoke tests: end-to-end orchestrator `spawn_many` round-trip,
  `parent_tools` threading via the runner's allowlist-escalation
  reject, and pool prewarm contracts.

## [0.7.0] — 2026-05-17 — multi-agent

Headline: parallel sibling agents, a shared trace-scoped blackboard,
a deterministic Pareto scorer for prompt-template variants, and
BuildKit cache mounts that drop incremental Docker rebuilds from
~12 min to ~90 s. Inspired by Nous Research's
[hermes-agent](https://github.com/NousResearch/hermes-agent) (true
multi-agent + GEPA prompt evolution) and
[openclaw](https://github.com/openclaw/openclaw) (pre-warmed pool
pattern). Full notes:
[`docs/release-notes-v0.7.0.md`](docs/release-notes-v0.7.0.md).

### Added

- **`subagent.spawn_many`** tool. Dispatches up to 3 sibling children
  concurrently under one parent context via `asyncio.gather`. The
  supervisor's existing per-parent concurrency cap (default 3)
  still governs live siblings; fan-outs exceeding the cap reject
  up-front with a clean args-invalid envelope.
- **Shared blackboard** (`blackboard.read` / `blackboard.write`).
  Trace-scoped, append-only sqlite scratchpad for sibling agents to
  coordinate. Writes never overwrite; reads return the latest value at
  call time; trace isolation is the security boundary.
- **`agents/orchestrator.yaml`**: new planner persona that
  decomposes → dispatches → reduces.
- **GEPA-lite Pareto scorer** (`corlinman_evolution_engine.score_variants`).
  Deterministic, no LLM-judge, no DSPy dependency — token Jaccard
  against the episodes that already succeeded.
- **Builtin-tool interception** in the agent servicer routes the four
  new tools in-process rather than through the Rust plugin registry.
- **BuildKit cache mounts** on the rust-builder + py-builder stages
  for cargo registry / git / target and uv wheel cache.

### Deferred to v0.7.1

- Pre-warmed Python agent runner pool (OpenClaw-style). Designed in
  [`docs/multi-agent-release-plan.md`](docs/multi-agent-release-plan.md) §2.3.

## [Unreleased] — targets v0.5.0

Free-form named providers + 7 new market `kind`s, **plus a BREAKING swap
from `sub2api` to `newapi`** as the channel-pool sidecar. Full notes:
[`docs/release-notes-v0.5.0.md`](docs/release-notes-v0.5.0.md).

### Removed (BREAKING)

- **`ProviderKind::Sub2api` removed.** The `kind = "sub2api"` provider entry
  is no longer recognised. Replace with `kind = "newapi"` pointing at a
  [QuantumNous/new-api](https://github.com/QuantumNous/new-api) instance.
  Run `corlinman config migrate-sub2api --apply` to rewrite legacy entries
  automatically. See [`docs/migration/sub2api-to-newapi.md`](docs/migration/sub2api-to-newapi.md).

### Added

- **`ProviderKind::Newapi`** + new-api admin client crate
  (`corlinman-newapi-client`). MIT-licensed sidecar that pools channels
  (LLM / embedding / audio TTS) behind one OpenAI-wire endpoint. Replaces
  the LGPL-3.0 sub2api integration.
- **4-step interactive onboard wizard** (account → newapi connect →
  pick defaults → confirm). The gateway calls new-api's `/api/channel`
  to populate model dropdowns; the operator only types the URL + token
  once.
- **`/admin/newapi` connector page** with live channel health, usage
  quota, token TTL, and a 1-token round-trip test button.
- **`corlinman config migrate-sub2api [--dry-run|--apply]`** CLI
  subcommand that rewrites legacy `kind = "sub2api"` entries to
  `kind = "newapi"` in place (with backup).
- **Full i18n coverage (zh-CN + en)** for the new onboard wizard and
  admin newapi page.
- **Free-form `[providers.*]` configuration**: the providers section is
  now a `BTreeMap<String, ProviderEntry>` keyed by an operator-chosen
  name. Add OpenRouter, SiliconFlow, Ollama, vLLM, or any other
  OpenAI-wire-compatible vendor by writing two TOML lines — no Rust
  patch required. The six legacy slot names (`anthropic`, `openai`,
  `google`, `deepseek`, `qwen`, `glm`) continue to infer their `kind`
  for backwards compatibility.
- **Seven new `ProviderKind` variants**: `mistral`, `cohere`,
  `together`, `groq`, `replicate`, `bedrock`, `azure`. The first five
  route through the shared `OpenAICompatibleProvider` Python adapter
  with documented default base URLs; `bedrock` and `azure` are
  declared but raise `NotImplementedError` at build time pending real
  SigV4 / deployment-routing support.
- **Validator**: free-form names without an explicit `kind` produce a
  `missing_kind` error pointing at the offending entry, listing every
  valid kind in the message.

### Docs

- New: [`docs/providers.md`](docs/providers.md) — provider model + 14
  supported `kind`s + four end-to-end recipes (OpenRouter + OpenAI
  embedding, fully-local Ollama, CN-resident SiliconFlow, Groq
  alongside OpenAI).
- Updated: [`docs/config.example.toml`](docs/config.example.toml) leads
  with `[providers.openai]` plus six commented-out vendor recipes; adds
  named-provider `[embedding]` and full-form `[models.aliases.*]`
  examples.
- Updated: [`docs/architecture.md`](docs/architecture.md) §7 inline
  sample reflects the free-form shape; reading list links the new
  providers reference.
- Updated: [`README.md`](README.md) Configuration section shows the
  new `kind = "..."` shape; documentation map links the new doc.

### Migration notes

- No data migration. Existing configs with first-party slot names
  parse unchanged.
- New entries MUST set `kind` explicitly; `corlinman config validate`
  surfaces any missing `kind` field with a one-line fix hint.
- `bedrock` and `azure` parse and validate but raise at adapter-build
  time today — declare `kind = "openai_compatible"` against a
  compatible proxy until the real adapters ship.

## [0.4.0] — 2026-04-23

Admin UI redesign: **Tidepool** design system. Warm-amber glass
aesthetic, day+night themes, and a reusable primitive library power a
from-scratch re-skin of all 15 admin pages. Backend and API unchanged —
this is a pure frontend release.

### Added

- **Design tokens** (`ui/app/globals.css`): `--tp-*` namespace for
  amber / ember / peach accents, ink ramp, glass layers, edge colours,
  gradients, shadows, and row alternation. Day and night palettes share
  every variable name; `data-theme="light|dark"` (mirrored to the
  `.dark` class for Tailwind compatibility) selects the active set.
- **12 new UI primitives** (`ui/components/ui/`):
  `<GlassPanel>` (soft/strong/subtle/primary variants respecting the
  ≤5 blur-layer/viewport budget), `<AuroraBackground>`,
  `<ThemeToggle>` (sun/moon pill with no-FOUC boot script),
  `<MiniSparkline>`, `<StreamPill>`, `<FilterChipGroup>`,
  `<StatChip>` (tick-up animation + ambient sparkline),
  `<JsonView>` (syntax-highlighted), `<LogRow>`, `<DetailDrawer>`,
  `<CommandPalette>` (configurable via `PaletteGroup[]`), plus
  `<UptimeStreak>`.
- **Motion tokens** (`ui/lib/motion.ts`): `tickUp` and `paletteIn`
  framer-motion variants alongside existing `fadeUp` / `stagger` /
  `springPop`. Continuous ambient animations (breathing, draw-in,
  just-now fades, badge pulses) live as CSS keyframes under `.tp-*`
  utility classes — cheaper than per-frame React work.
- **Typography**: Instrument Serif (display) loaded via `next/font`
  as `var(--font-instrument-serif)`, paired with existing Geist sans
  and Geist mono.
- **Theme persistence**: shared `corlinman-theme` storage key between
  `next-themes` and the inline boot script in `app/layout.tsx`.
  Hydration is race-free because the boot script writes
  `data-theme` + `.dark` before React mounts.
- **UI docs**: new "Tidepool design system" section in `ui/README.md`
  documenting tokens, primitive APIs, motion patterns, performance
  budget, and a new-page quick-start.

### Changed

- **All 15 admin pages retokened** onto Tidepool: Dashboard, Logs,
  Plugins, Approvals, Skills, Characters, Hooks, Scheduler, Nodes,
  Playground, Canvas, Tag Memo, Diary, Channels (QQ + Telegram),
  Config, Login, Models, Providers, Embedding, RAG, Agents. Direct
  colour/background classes replaced with `tp-*` tokens, `<Card>`
  uses swapped for `<GlassPanel>` where the glass treatment applies.
- **Admin layout** (`app/(admin)/layout.tsx`): `<AuroraBackground>`
  mounted once behind the sidebar + main grid; container spacing
  normalised to `gap-4 p-4`.
- **Command palette** (`components/cmdk-palette.tsx`): inner
  rendering delegated to the new `<CommandPalette>` primitive via a
  declarative `PaletteGroup[]` config. `useCommandPalette` hook,
  `CommandPaletteProvider`, `NAV_CMDS` registry, recent-routes, and
  test-chat drawer preserved.
- **i18n**: pages that gained Tidepool prose (hero copy, empty
  states, filter chips) now partition their new keys under a
  `<page>.tp.*` sub-namespace to keep diffs legible.

### Fixed

- **WCAG AA contrast**: darkened day-mode `--primary` to amber-800
  (`hsl(20 82% 33%)`) after `<Button>` primary text failed 4.5:1
  against foreground on the warm base. Night mode uses amber-400
  (`hsl(35 90% 65%)`) on dark ink.
- **Aurora visibility**: removed `bg-background` from `<body>` in
  `app/layout.tsx`; the admin layout now owns the backdrop, while
  the login route re-adds `bg-background` on its own root.
- **Offline-state HTML dumps**: plugins and scheduler pages detected
  backend HTML error responses (rather than JSON) and rendered the
  raw markup; `OfflineBlock` now suppresses dumps whose first line
  starts with `<`.
- **Telegram page `<dl>` a11y**: nested `<FilterStatCell>` broke
  definition-list semantics. Converted the wrapper to
  `<div>/<div>/<div>` so axe passes.

### Performance

- Dashboard blur-layer count dropped from 7 → 4 per viewport by
  defaulting non-primary `<StatChip>` instances to `<GlassPanel
  variant="subtle">` (tp-glass-inner, no `backdrop-filter`). Primary
  chip retains the full glass treatment to anchor the eye.
- All continuous animations (breathing dots, draw-in underlines,
  badge pulses, just-now fades) run as CSS keyframes gated by
  `@media (prefers-reduced-motion: reduce)`.

### Migration notes

- No backend changes. Existing deployments can upgrade by pulling the
  new `ui-static/` bundle only.
- Custom pages that used raw `bg-card` / `text-muted-foreground`
  continue to render — Tidepool tokens compose alongside legacy
  shadcn tokens rather than replacing them.
- Users with persisted theme preferences from the previous
  `next-themes` default key will see a one-time flip to dark on
  first visit; the new `corlinman-theme` key is then used
  consistently.

[0.4.0]: https://github.com/ymylive/corlinman/releases/tag/v0.4.0

## [0.3.0] — 2026-04-23

Sprint 9 (Batch 1–4) rollup: hierarchical tags + EPA cache in the
vector store, manifest v2, reserved placeholder namespaces, and
dual-track tool-call protocol. All additions are backwards-compatible.
Upgrade guide: [`docs/migration/v1-to-v2.md`](docs/migration/v1-to-v2.md).

### Added

- **Manifest v2** (`corlinman-plugins`): new `manifest_version`,
  `protocols`, `hooks`, `skill_refs` fields. Absent `manifest_version`
  is treated as v1 and auto-migrates to v2 in memory with default
  protocols `["openai_function"]`. Unknown `protocols` values are
  rejected at load; unknown `hooks` names warn but don't fail.
- **Vector schema v6** (`corlinman-vector`): new `tag_nodes`
  (hierarchical tag tree: `id / parent_id / name / path / depth`) and
  `chunk_epa` (per-chunk EPA projection cache). `chunk_tags` retargets
  its FK to `tag_nodes.id`; flat v5 tags materialise as depth-0 nodes
  so legacy queries keep working. Migration is idempotent and runs
  in-transaction on first open.
- **Config sections**: `[hooks]`, `[skills]`, `[variables]`,
  `[agents]`, `[tools.block]`, `[telegram.webhook]`, `[vector.tags]`,
  `[wstool]`, `[canvas]`, `[nodebridge]`. All `#[serde(default)]` —
  existing `config.toml` loads unchanged.
- **Placeholder namespaces**: reserved `var / sar / tar / agent /
  session / tool / vector / skill`. Cycle detection, async resolution,
  `{{角色}}` agent-card expansion with single-agent-gate semantics.
- **On-disk authoring surfaces**: `skills/*.md` (openclaw-style YAML
  frontmatter + Markdown), `agents/*.yaml` (character cards),
  `TVStxt/{tar,var,sar,fixed}/*.txt` (four-tier cascade variables).
  Sample files ship in-repo.
- **New Rust crates**: `corlinman-hooks` (in-process hook bus),
  `corlinman-skills` (openclaw skill loader + system-prompt injector),
  `corlinman-wstool` (local WebSocket tool bus), `corlinman-nodebridge`
  (Node.js worker bridge listener).
- **New Python package**: `corlinman-tagmemo` (EPA basis fitting +
  pyramid build; feeds `chunk_epa` cache).
- **Admin UI pages**: `/skills`, `/characters`, `/hooks`,
  `/playground/protocol`, `/channels/telegram`, `/nodes`, plus
  tagmemo / diary / canvas surfaces.
- **Dual-track tool invocation**: agents may emit tool calls as
  `<<<[TOOL_REQUEST]>>>` structured blocks (with `「始」…「末」`
  value fencing) in addition to OpenAI function-call JSON. Opt in per
  agent via manifest `protocols = ["block"]` + `[tools.block].enabled
  = true`. Legacy plugins remain reachable via
  `fallback_to_function_call = true`.

### Migration notes

- Legacy v1 plugin manifests parse unchanged.
- v5 vector DBs migrate forward on first open; there is no shipped
  down-path — rollback is "restore the pre-upgrade data-dir backup".
- Existing `config.toml` needs no edits.

[0.3.0]: https://github.com/ymylive/corlinman/releases/tag/v0.3.0

## [0.2.0] — 2026-04-21

Major release. Dynamic provider registry, per-alias model params,
first-class embedding config, and admin UI to manage all of it.
Full notes: [`docs/release-notes-v0.2.0.md`](docs/release-notes-v0.2.0.md).

### Added

- **Config**: `[providers.<name>].kind` enum + `params` map;
  `[models.aliases.<name>].params`; new `[embedding]` section.
  Backward-compatible — configs without `kind` on first-party
  providers still parse via inferred-kind defaults.
- **Rust admin routes**: `/admin/providers` (CRUD + 409 reference
  guard); `/admin/embedding` (GET/POST, benchmark stubbed to 501);
  `/admin/models/aliases` extended with single-row upsert + delete.
- **Python**: dynamic `ProviderRegistry` driven by `[providers.*]`
  specs; `params_schema()` on every provider; new
  `CorlinmanEmbeddingProvider` ABC with OpenAI-compatible + Google
  implementations; `benchmark_embedding()` helper (p50/p99 latency +
  cosine matrix).
- **UI**: `/providers` + `/embedding` pages, `/models` inline-accordion
  for params, hand-rolled `<DynamicParamsForm>` JSON-Schema renderer,
  ~145 new i18n keys across zh-CN + en.

### Fixed

- `/admin/approvals` returned 503 in production because `ApprovalGate`
  was never constructed at boot. `build_runtime_with_logs` now wires
  it from the live config handle + the RAG SQLite.

### Changed

- Docker image drops the `ui-builder` stage. Production serves the
  Next.js static export via nginx from `/opt/corlinman/ui-static/`;
  bundling it was dead weight and segfaulted node under Rosetta 2
  cross-builds.

### Known issues

- `/admin/embedding/benchmark` is a 501 stub until the Python helper
  is reachable over gRPC from Rust. UI handles the fallback.
- Rust gateway doesn't yet export `CORLINMAN_PY_CONFIG` to the Python
  subprocess; the legacy prefix-matching path keeps chats working
  while the config-driven registry integration lands.

[0.2.0]: https://github.com/ymylive/corlinman/releases/tag/v0.2.0

## [0.1.3] — 2026-04-21

zh-CN / en internationalisation + static-bundle API fix. Pure frontend
release — no Rust, Python, or Dockerfile changes.

### Added

- Full zh-CN / en i18n across every admin page, layout, login, dashboard,
  and `⌘K` palette. `react-i18next` + two TypeScript locale bundles
  (378 keys each, compile-time parity enforced).
- Language toggle in the topnav + command-palette action. Choice persists
  in `localStorage`; first-visit detection falls back to
  `navigator.language` (`zh*` → Chinese, else English).
- Inline pre-hydration boot script sets `<html lang>` so language
  selection applies before React mounts (no FOUC).

### Fixed

- **`GATEWAY_BASE_URL` default**: changed from `"http://localhost:6005"`
  to `""`. The static export used to bake localhost into the visitor's
  bundle, making every `/admin`, `/health`, `/v1` call from a deployed
  origin fail with `ERR_CONNECTION_REFUSED`. Relative URLs now resolve
  through the current origin, which nginx already reverse-proxies to
  the gateway. `NEXT_PUBLIC_GATEWAY_URL` remains the local-dev
  override; mock-server paths untouched.

### Dependencies

- Added: `i18next`, `react-i18next`, `i18next-browser-languagedetector`.

[0.1.3]: https://github.com/ymylive/corlinman/releases/tag/v0.1.3

## [0.1.2] — 2026-04-21

Admin UI redesign. Pure frontend release — no Rust, Python, or
Dockerfile changes.

### Changed

- **Admin UI fully redesigned in a Linear / Vercel aesthetic**: dark-first
  with a single indigo accent, Geist Sans / Mono typography, borders-over-shadows,
  compact 6–8 px radii. `next-themes` light/dark toggle preserved.
- **New dashboard landing page** (`/`): four stat cards with inline
  sparklines, SSE-driven recent-activity feed, and a 7-check system health
  panel backed by `/health`.
- **Sidebar + topnav**: 240 ↔ 56 px collapsible sidebar with an animated
  active-indicator (framer-motion `layoutId`); topnav adds auto
  breadcrumb, live health dot, theme toggle, and a `⌘K` search pill.
- **Global command palette** (`cmdk`): fuzzy navigation over all
  destinations, a test-chat drawer that POSTs to `/v1/chat/completions`,
  plus theme-toggle and logout actions. Recent commands persist in
  `localStorage`.
- **Motion language**: 200 ms page-transition fades, skeleton shimmers,
  `sonner` toasts, slide-up issues drawer on the config page. No bouncy
  spring animations.
- **Refined pages**: Plugins, Agents, RAG, Channels, Scheduler, Approvals,
  Models, Config, Logs — consistent status dots, inline-edit affordances,
  virtualised logs list with pause-stream toggle, live scheduler countdowns.
- **New login page**: two-column layout with a constellation backdrop
  SVG and inline error with shake micro-animation.

### Added

- `framer-motion`, `cmdk`, `geist`, `sonner` as UI dependencies.
- `fetchHealth()` + `HealthStatus` type in `ui/lib/api.ts`.

### Stability

- Playwright E2E selectors audited and preserved.
- Vitest suite (including Chinese login-form labels) still green.
- No API contracts changed.

[0.1.2]: https://github.com/ymylive/corlinman/releases/tag/v0.1.2

## [0.1.1] — 2026-04-21

Deployment hotfix. Surfaced the first time the 1.0 image was built
against a real server. All changes are docker / runtime fixes — no
code behaviour changes outside the boot path.

### Fixed

- **`docker/Dockerfile`**: drop stale `pnpm -C ui export` step —
  Next.js 14 removed the `next export` command; `output: "export"` in
  `ui/next.config.ts` already emits the static bundle during
  `next build`.
- **`docker/Dockerfile`**: bump rust base from `1.85-slim` to
  `1.95-slim` to match the project's `rust-toolchain.toml`.
  `cargo-chef 0.1.77` transitively raised its MSRV to `rustc 1.88`.
- **`docker/Dockerfile`**: add `binutils` + `g++` to the rust-builder
  apt layer (required by `link-cplusplus`) and force the BFD linker via
  `RUSTFLAGS=-C link-arg=-fuse-ld=bfd`. `lld` SIGSEGVs under Rosetta 2
  / QEMU user-mode emulation when cross-building amd64 images from
  Apple Silicon hosts.
- **`docker/Dockerfile`**: correct runtime `COPY` of the CLI binary —
  cargo emits `/build/target/release/corlinman` (per `[[bin]] name`),
  not `corlinman-cli`.
- **`rust/crates/corlinman-gateway/src/main.rs`**: honour `BIND` env
  var (default `127.0.0.1`, containerised deploys set `0.0.0.0`).
  Previously the listener was hard-bound to `127.0.0.1` and docker
  port-publishing never reached it.
- **`docker/Dockerfile`**: carry the python source tree into the
  runtime image. `uv sync --no-editable` ignores workspace members, so
  venv `.pth` shims pointed at `/build/python/packages/*/src/` which
  don't exist in runtime — `corlinman-python-server` died at
  `ModuleNotFoundError`. Adding `COPY --from=py-builder /build/python
  /build/python` resolves the editable paths.

### Added

- **Runtime env knobs**: `BIND` (listener address) and `OPENAI_BASE_URL`
  (consumed by `AsyncOpenAI` when `[providers.openai].base_url` isn't
  threaded through — see Known Issues).

### Known issues carried over

- `corlinman_providers.registry.resolve()` still ignores `[providers.*]`
  settings from `config.toml`. Until a deeper fix lands, point non-default
  OpenAI-compatible backends at the right host via `OPENAI_BASE_URL`.
- Docker image does not supervise the python agent out of the box;
  production deploys use a startup script (`docker/start.sh` pattern)
  that spawns `corlinman-python-server` alongside `corlinman-gateway`.

[0.1.1]: https://github.com/ymylive/corlinman/releases/tag/v0.1.1

## [0.1.0] — 2026-04-21

First tagged release. The 1.0 release prep sprint (S8) wraps seven prior
implementation sprints (M0–M7) into a shippable self-hosted intelligent
agent platform.

### Added

- **Core gateway** (`rust/crates/corlinman-gateway`): OpenAI-compatible
  `/v1/chat/completions` (stream + non-stream), `/v1/embeddings`,
  `/v1/models`, WebSocket admin endpoints, and the full admin REST surface
  (`/admin/plugins`, `/admin/rag/*`, `/admin/approvals`, `/admin/scheduler/*`,
  `/admin/config`, `/admin/logs/stream`, `/admin/health/metrics`). Session
  history persisted to `~/.corlinman/sessions.sqlite` with a configurable
  trim cap.
- **Python agent plane** (`python/packages/corlinman-server`,
  `corlinman-agent`, `corlinman-providers`): gRPC `Agent.Chat` reasoning
  loop with streaming token deltas, tool-call loop, and providers for
  Anthropic, OpenAI, Google, DeepSeek, Qwen, and GLM.
- **Plugin runtime** (`rust/crates/corlinman-plugins`): three plugin
  types (sync / async / service) over JSON-RPC 2.0 stdio or gRPC.
  Includes manifest parser, `plugin-manifest.toml` validation, async
  task callback registry (`/plugin-callback/:task_id`), approval gate
  for human-in-the-loop tool execution, hot reload of the plugin
  registry, and a Docker sandbox runner for untrusted plugins.
- **RAG** (`rust/crates/corlinman-vector`): SQLite + FTS5 BM25,
  usearch HNSW dense recall, reciprocal-rank fusion, optional
  gRPC-backed cross-encoder rerank, tag-filter pushdown, LRU unload,
  and multi-step schema migrations (v1 → v4).
- **Channels** (`rust/crates/corlinman-channels`): QQ (go-cqhttp /
  OneBot v11) and Telegram adapters with rate limiting, multimodal
  uploads, user-to-session binding.
- **Observability** (M7): W3C `traceparent` propagation, OpenTelemetry
  OTLP exporter, three-tier Prometheus metrics (gateway / plugin /
  provider), `/health` probes driven by real component state, `corlinman
  doctor` with 20+ diagnostic checks (config / agent gRPC ping / SQLite
  / usearch / plugin registry / docker / disk / memory / log rotation /
  provider HTTPS smoke / manifest duplicates / broken symlinks /
  pending-approvals overflow / python subprocess health / …).
- **Admin UI** (`ui/`): Next.js 15 + React 19 dashboard for plugins,
  RAG, approvals, scheduler, config, logs, and health metrics.
  Playwright e2e coverage.
- **CLI** (`rust/crates/corlinman-cli`): `corlinman onboard`,
  `corlinman doctor`, `corlinman plugins`, `corlinman config`,
  `corlinman dev`, `corlinman vector`, and — new in this release —
  `corlinman qa run` + `corlinman qa bench`.

### Docs

- `docs/roadmap.md` — canonical sprint plan (through M8 and beyond).
- `docs/architecture.md`, `docs/plugin-authoring.md`, `docs/runbook.md`.
- `docs/perf-baseline-1.0.md` — p50 / p99 numbers for chat, RAG, and
  plugin exec roundtrips. Used by CI to detect ≥20 % regressions.
- `qa/scenarios/*.yaml` — 8 executable scenarios covering chat
  stream + non-stream, tool-call loop, plugin sync + async, RAG hybrid
  retrieval, OneBot echo, and a marked-live fresh-install walkthrough.

### Known gaps (deferred to 0.1.1)

- **No prebuilt docker image yet.** Build from source with `cargo build
  --release -p corlinman-gateway -p corlinman-cli`; the `ghcr.io/ymylive/corlinman:0.1.0`
  image is pending a v0.1.1 follow-up once a build host with docker is
  available.
- **Screenshot placeholder**: `README.md` references
  `docs/assets/dashboard.png`; the actual PNG will be added with the
  installation walkthrough screencast.
- **`fresh-install` QA scenario** is marked `requires_live: true` — it's
  exercised by the S8 T4 screencast rather than the offline CI runner.
- **1.0 release comms** (blog / Zhihu / Hacker News / r/selfhosted /
  r/LocalLLaMA) are a separate content-production task, not part of
  this release artefact.

### Reference

Commit history on the `main` branch:

- `sprint-1` through `sprint-3`: M1 / M2 / M3 / M4 scope
- `sprint-4` (M5 channels), `sprint-5` (M6 auth + logs + approvals),
  `sprint-6` (M6 admin UI + Playwright)
- `sprint-7` (M7 observability)
- `sprint-8` (this release — M8 1.0 prep)

[0.1.0]: https://github.com/ymylive/corlinman/releases/tag/v0.1.0
