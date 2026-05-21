# corlinman Python 运行时补全 —— 多 Agent 并行派发计划

> **状态**: draft — 待 operator 确认 §6 范围
> **作者**: Claude Code(规划 agent),2026-05-21
> **目的**: 把 corlinman 的 Rust→Python 移植「最后一公里」拆成可并行派发的工作包,
> 多个 agent 同时开工,加速补全网关运行时。
> **对标**: `NousResearch/hermes-agent`、`openclaw/openclaw`(功能广度基准)

---

## 0. 背景与目标

### 0.1 真实现状(线上实测,非读代码臆测)

corlinman 的 **Rust 版**(M0–M8,`v0.1.0`)曾完整可用。当前正在做 **Rust→Python 移植**;
`rust/` 树已删除,生产跑的是 Python 网关。Python 网关的**外壳已完成**,但**运行时未接通**。

实测证据(`https://corlinman.cornna.xyz`,2026-05-21):

| 探针 | 结果 | 含义 |
| --- | --- | --- |
| `GET /health` | `{"status":"ok","mode":"degraded"}` | 网关降级启动 |
| `POST /v1/chat/completions` | `501 no ChatService wired` | 聊天通路未接 |
| `GET /v1/models` | `501 no ProviderRegistry wired` | provider 注册表未接 |
| 启动日志 | `gateway.sibling_missing module=...core.config` | 配置加载器模块缺失 |
| QQ 频道 | 无 `:3001` 连接、无频道日志 | `run_qq_channel` 全代码库零调用 |

> ⚠️ 一份自动审计曾报告「corlinman 92% 完成、一切正常」——**该结论错误**:它把
> 「代码文件存在」当成「已接线运行」。本计划以上述线上实测为准。

### 0.2 关键判断:这是「接线」不是「重写」

绝大多数实现代码**已存在**,缺的是**装配**:

- `corlinman-providers`:12+ provider adapter 已实现(`openai_provider.py` 等),`ProviderRegistry` 已实现 —— 但未在网关构建。
- `corlinman-channels`:`run_qq_channel` / `run_telegram_channel` + OneBot 适配器已实现 —— 但无任何调用方,且 `corlinman-server` 未声明该依赖。
- `gateway/services/chat_service.py`:`ChatService` / `ChatBackend` 协议 / `GrpcAgentChatBackend` 已实现 —— 但从未用真实 backend 构建,`ChatState` 永远是 `None`。
- `corlinman-agent`:`reasoning_loop.py` 已实现 —— 但无 server 入口把它跑起来。
- `gateway.core.config`:**缺失** —— 整个降级的根因(`entrypoint.py` 把它当 sibling 懒加载,缺了就降级)。
- `gateway/grpc/`:`serve_placeholder_in_background` + `_NullEngine` —— 占位实现。

### 0.3 目标

完成 Python 网关运行时装配,使其**非降级**启动并:

1. `/v1/chat/completions` + `/v1/models` 真实可用;
2. QQ / Telegram 频道运行时接通(机器人能收发消息);
3. 朝 hermes/openclaw 功能广度补齐二级特性(工具执行、evolution apply、voice 等)。

---

## 1. 设计原则:契约先行 → 真并行

`entrypoint.py` 的 lifespan 已经有**集成缝**:对每个 sibling 模块取 `bootstrap`
属性并调用。**只要把契约固定下来**,Wave 1 之后所有工作包就能各写各的、最后在缝处汇合。

派发原则:

- 一个**工作包(Parcel)= 一个 agent 独立可领的任务**。
- 同一 Wave 内的 parcel **无共享状态依赖** → 完全并行。
- 跨 Wave 严格按依赖排序。
- 每个 parcel 自带验收标准,产出走独立 PR。
- 每个 dispatched agent 在自己 parcel 内**自行做细节勘探**(本计划只定边界+契约,不替它探)。
- Rust 参考实现在 **git 历史**里(`rust/` 已删):需要时 `git log --all -- rust/` / `git show`。

---

## 2. 集成契约(Wave 0 锁定,全员据此编码)

> 这是并行的地基。Wave 0 的 P0 负责实现并把本节固化为 `docs/contracts/runtime-wiring.md`。

### 2.1 `AppState` 运行时字段(`gateway/core/state.py`)

```python
@dataclass
class AppState:
    config: Config | None = None            # P0 — 类型化配置(取代裸 dict)
    provider_registry: Any = None           # P1 — corlinman_providers.ProviderRegistry
    chat: Any = None                         # P2 — gateway.services.ChatService
    # ... 既有字段不动 ...
```

### 2.2 sibling `bootstrap` 约定(`entrypoint.py` lifespan 已有调用点)

每个 sibling 模块**可选**导出:

```python
def bootstrap(state: AppState) -> None | Awaitable[None] | list[asyncio.Task]:
    """启动期装配。返回的 Task 会被登记进 background 列表,
    在 cancel 事件触发时统一取消 + await。"""
```

`entrypoint.py` 既有循环 `(services, "services"), (evolution, "evolution")` 扩展为也覆盖
`providers`、`channels`(或全部并入 `services.bootstrap`)。

### 2.3 配置加载器(`gateway/core/config.py` —— P0 新建)

```python
def load_from_path(path: Path) -> Config: ...
class Config(BaseModel):
    server: ServerCfg; admin: AdminCfg
    providers: dict[str, ProviderCfg]
    models: ModelsCfg
    channels: ChannelsCfg
    evolution: EvolutionCfg | None = None
    # ...
```

### 2.4 `ChatBackend` 协议(已存在于 `chat_service.py`,不改)

`async def start(start: ChatStart) -> tuple[asyncio.Queue, AsyncIterator[ServerFrame]]`
—— P2 的 `DirectProviderBackend` 与 P4 的 `GrpcAgentChatBackend` 都实现它,网关按
`config.models` / 部署模式择一注入 `ChatService`。

---

## 3. 派发计划 —— Waves & Parcels

### Wave 0 — 地基(1 个 agent,串行,阻塞全部)

| Parcel | P0 — 配置加载器 + 契约固化 |
| --- | --- |
| Owner | `core-config` agent |
| 依赖 | 无 |
| 涉及 | `gateway/core/config.py`(新建)、`gateway/core/state.py`、`gateway/lifecycle/entrypoint.py`、`docs/contracts/runtime-wiring.md`(新建) |
| 范围 | 实现缺失的 `gateway.core.config`:类型化 `Config`(pydantic)+ `load_from_path()`;解析 `config.toml` 全字段并解析 `{env="X"}` 引用;把 §2 契约写成 `runtime-wiring.md`;扩展 `entrypoint.py` 的 sibling-bootstrap 循环 |
| 验收 | 启动日志不再出现 `sibling_missing ...core.config`;`AppState.config` 是类型化对象;`runtime-wiring.md` 提交 |
| 工期 | ~1 天 |

### Wave 1 — 运行时核心(4 个 agent,**完全并行**)

> 全部据 §2 契约编码。P2 给「能聊天」的快路径,P4 给「完整 agent」路径;两者并行。

| Parcel | P1 — Provider 注册表接线 |
| --- | --- |
| Owner | `providers` agent · 依赖 P0 |
| 涉及 | `gateway/lifecycle/entrypoint.py`、`gateway/routes/models.py`、`gateway/routes/register.py`、`corlinman-providers/registry.py` |
| 范围 | 由 `Config.providers` 构建 `ProviderRegistry`,挂到 `AppState.provider_registry`;给 `/v1/models` router 注入 `source=` |
| 验收 | `GET /v1/models` 返回已配置模型列表(200) |
| 工期 | ~1 天 |

| Parcel | P2 — ChatService + 直连 backend |
| --- | --- |
| Owner | `chat-service` agent · 依赖 P0(契约)、P1(运行期取 registry) |
| 涉及 | `gateway/services/`(新建 `direct_backend.py`)、`gateway/services/__init__.py`(加 `bootstrap`)、`gateway/routes/chat.py`、`register.py` |
| 范围 | 实现 `DirectProviderBackend`(`ChatBackend`,直接调 `corlinman-providers` 的 `chat_stream`,把 `ProviderChunk` 翻译成 `agent_pb2.ServerFrame`);构建 `ChatService`,挂 `AppState.chat`;给 `chat.router` 注入 `ChatState` |
| 验收 | `POST /v1/chat/completions` 流式+非流式均返回真实补全(200) |
| 工期 | ~2 天 |

| Parcel | P3 — 频道运行时启动器 |
| --- | --- |
| Owner | `channels` agent · 依赖 P0;按 §2.4 契约可与 P2 并行开发,末端集成 |
| 涉及 | `gateway/services/bootstrap.py`(新建)、`corlinman-server/pyproject.toml`(加 `corlinman-channels` 依赖)、`entrypoint.py` |
| 范围 | `bootstrap(state)`:读 `Config.channels`,为启用的频道构建 `QqChannelParams`/Telegram 参数(`chat_service=state.chat`),以后台任务跑 `run_qq_channel`/`run_telegram_channel`,登记进 `background` 受 `cancel` 管控 |
| 验收 | 网关启动即连 NapCat OneBot WS(`:3001` 出现连接);QQ 私聊机器人能收到回复;`/admin/channels/qq/status` 显示在线 |
| 工期 | ~2 天 |

| Parcel | P4 — gRPC Agent backend(完整 agent 路径) |
| --- | --- |
| Owner | `agent-runtime` agent · 依赖 P0;与 P1/P2/P3 并行 |
| 涉及 | `corlinman-server/main.py`、`gateway/grpc/`、`corlinman-agent/`、`corlinman-grpc/` |
| 范围 | 把 `corlinman-agent` 的 `reasoning_loop` 做成可运行的 gRPC `Agent` server(`corlinman-python-server` 入口);替换 `serve_placeholder` / `_NullEngine`;让 `GrpcAgentChatBackend` 真正连上。这条路径带工具/技能/记忆(P2 直连版没有) |
| 验收 | gRPC agent 跑起来后,`ChatService` 经 `GrpcAgentChatBackend` 能产出带工具调用的多轮回复 |
| 工期 | ~3 天(较大) |

### Wave 2 — 功能补全 / 对标 hermes·openclaw(~7 个 agent,并行)

| Parcel | 内容 | 依赖 | 验收要点 |
| --- | --- | --- | --- |
| P5 | 真实 `ToolExecutor`(取代 `PlaceholderExecutor`),推理循环里跑插件/工具 | P4 | 工具调用真实执行并回灌结果 |
| P6 | Evolution apply/rollback(`routes_admin_b/evolution.py` 的 501 → 接 `EvolutionApplier`) | P0 | 提案可 apply/rollback |
| P7 | Voice 真实 provider(OpenAI realtime,替换 `MockProvider`) | P1 | `/v1/voice` 接真实语音 |
| P8 | Bedrock(SigV4)/ Azure(部署路由)provider —— 消除运行期 `NotImplementedError` | P1 | 两家 provider 真实可用 |
| P9 | 记忆/RAG + episodes 整合(tagmemo、`about_tag` resolver) | P0 | 记忆 token 解析齐全 |
| P10 | 频道广度对标(hermes 30+/openclaw 20+:Discord/Slack 等,按优先级取舍) | P3 | 新频道收发通 |
| P11 | `core.config` 热重载接线 + 占位引擎替换 | P0 | 改 config.toml 热生效 |

### Wave 3 — 加固(2 个 agent)

| Parcel | 内容 |
| --- | --- |
| P12 | 每 parcel 的 pytest 覆盖、`corlinman doctor` 检查项、observability 指标 |
| P13 | 文档 + release notes;更新 `milestones.md` / 本计划状态 |

---

## 4. 并行度与派发机制

| Wave | 并行 agent 数 | 关键路径 |
| --- | --- | --- |
| 0 | 1(P0) | **keystone,阻塞全部** |
| 1 | 4(P1–P4 同时派) | P4 最长(~3d)= Wave 1 墙钟 |
| 2 | 7(P5–P11 同时派) | P5 依赖 P4 完成 |
| 3 | 2(P12、P13) | — |

**派发方式**:每个 Wave,按上表给每个 parcel 起一个 agent,prompt = 该 parcel 的
规格行 + `docs/contracts/runtime-wiring.md` + 本节「每 parcel 须做」清单。

**每 parcel 须做**:① 在 parcel 边界内自行细勘探 → ② 据契约实现 → ③ 写 pytest →
④ 跑 `pytest python/packages -q` + 对应冒烟 → ⑤ 开独立 PR(分支 `feat/port-Pn-<slug>`)。

**每 Wave 收口门**:全部 parcel PR 合并后,跑集成冒烟(§5),绿了才开下一 Wave。

**墙钟估算**:Wave0 1d + Wave1 ~3d + Wave2 ~3d + Wave3 ~1d ≈ **8 天**(串行约需 ~20 天)。

---

## 5. 验收门(线上冒烟)

补全完成的判据 —— 对生产网关实测全绿:

1. `GET /health` → `mode` 不再是 `degraded`。
2. `GET /v1/models` → 200,列出已配置模型。
3. `POST /v1/chat/completions` → 200,流式+非流式真实补全。
4. QQ 私聊机器人 → 收到 LLM 回复;`/admin/channels/qq/status` 在线。
5. `pytest python/packages -q` 全绿。
6. 启动日志无 `sibling_missing` / `degraded`。

---

## 6. 范围决策(已确认 2026-05-21)

- [x] **Wave 1 全做** —— P1 + P2 + P3 + P4 四个 parcel 都执行。
- [x] **Wave 2 范围**(规划 agent 拍板):**P5**(真实工具执行)+ **P12**(测试 / `doctor`)
      + **P13**(文档 / 版本)。P6–P11(evolution apply、voice、Bedrock·Azure、记忆、
      频道广度、热重载)推迟到 `ap1.0.0` 之后的迭代。
- [x] **单一 PR** —— 全部工作落在分支 `feat/port-completion`,最终合并为一个 PR。
- [x] **目标版本号**:`ap1.0.0`(runtime-complete)。

> 执行顺序:Wave 0(P0)→ Wave 1(P1–P4)→ Wave 2(P5、P12、P13)。P0 完成后令
> `entrypoint.py` 的集成缝通用化,P1–P4 仅新增各自 sibling 模块、互不改缝 → 可并行派发。

---

## 7. 风险

| 风险 | 缓解 |
| --- | --- |
| 并行 parcel 在 `AppState`/`entrypoint.py` 改冲突 | 契约 §2 锁死字段;`entrypoint.py` 的改动集中在 P0,后续 parcel 只加 sibling 模块不改缝 |
| P2 直连版与 P4 完整版语义分叉 | 都实现同一 `ChatBackend` 协议;网关按 config 择一,互不影响 |
| `corlinman-agent` 实际不可独立运行 | P4 首日先做可运行性勘探,不行则升级为「移植」工作量并通知 operator |
| Rust 参考已删 | 用 `git log --all -- rust/` 取历史;OpenAI 兼容 API 契约本身是稳定参照 |

---

*计划结束。请 operator 在 §6 签字后,从 Wave 0 / P0 起派发。*
