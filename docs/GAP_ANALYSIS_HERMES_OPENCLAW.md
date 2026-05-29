# corlinman 功能广度差距分析 —— 对标 hermes-agent / openclaw

> **状态**: ✅ 审计 + Tier A + Tier B 已执行落地(2026-05-22,ap1.0.0 之后)。
> Tier C(RAG 深化)推迟 —— 审计确认 embedding/RAG 已接线,深化属低优先。
> **方法**: 2 个调研 agent 独立抓取 `NousResearch/hermes-agent`、`openclaw/openclaw`
> 的真实特性面;corlinman 现状由主线**逐项实测代码**(grep 501 / NotImplementedError /
> Mock / 实际调用链),不信任"文件存在即完成"。
> **结论**: ap1.0.0 让运行时**从降级变可用**;功能**广度**对标基本未动 ——
> 下表是修正后的 backlog(已含计划 §6 漏掉的项)。

---

## 1. 功能矩阵

| 维度 | corlinman ap1.0.0(实测) | hermes-agent | openclaw |
| --- | --- | --- | --- |
| LLM provider 适配 | 4 个:openai / anthropic / google + Bedrock·Azure 占位(`NotImplementedError`)。`openai_compatible` kind 覆盖多家国产云。OAuth 订阅在 provider-auth 计划中 | 30+ | 30+，含 4 个本地运行时(Ollama/LM Studio/vLLM/SGLang) |
| 消息频道 | **2**:QQ、Telegram | ~23 | 23 |
| 工具 / function-calling | P5 真实 ToolExecutor,跑 `sync` 插件(JSON-RPC stdio);`service`/`mcp` 插件类型未支持。内置工具集薄 | 40–70+ 内置工具,7 个沙箱后端 | ~10 类工具,策略门控,内置浏览器 |
| MCP | 有代码(`corlinman-mcp-server` 包 + `gateway/mcp/`)**但未接入 agent 工具面** | 原生 MCP client + sampling | MCP client(stdio+HTTP),并暴露 memory MCP server |
| 记忆 / RAG | `corlinman-embedding` 已接(`/v1/embeddings`、admin rag/memory 路由);`tagmemo`/`episodes` 包存在;`about_tag` resolver —— 部分接通 | 4 层(skills / FTS5 / Honcho / MEMORY.md),8 个 provider | 3 层 markdown + BM25+向量混合检索,4 个后端,dreaming 整理 |
| Agent 编排 | 推理循环 + subagent(blackboard / runner) | kanban + cron + 委派 | coordinator-worker、Lobster 工作流、Agent Teams |
| 自进化 | evolution proposer + curator 在;**apply/rollback 现已接 `EvolutionApplier`,但仅记账(写 history,从不改 skill/prompt/kb;`metrics_baseline={}` 使 auto-rollback fail-safe 跳过;monitor 无 driver)** | 技能自创建 + 离线 GEPA 优化器 | 技能自扩展 |
| 语音 / 多模态 | `/v1/voice` 路由已挂载,但**只有 MockVoiceProvider,无真实适配器** | 推到说话、10 TTS、本地 Whisper | 14 TTS、Whisper、Talk Mode、唤醒词 |
| Hooks | `corlinman-hooks` 包 | 6 个生命周期 hook | 事件总线,18ms hook |
| Skills | 16 个内置 + 首启自动播种 | 118 内置 + agentskills.io | 5700+ ClawHub |
| 管理 / UI | Next.js admin UI(较完整) | TUI + web 仪表盘 | Control UI + macOS/iOS app |
| 可观测性 | structlog 结构化日志 + `doctor` 检查(P12) | Langfuse + OTel 插件 | 原生 OpenTelemetry(v2026.2) |
| 配置热重载 | `ConfigWatcher` 有代码,**启动期未接线** | config.yaml | 热重载 |
| 部署 | docker + native systemd | 7 后端 / 4 模式 | npm/docker/fly/render/nix |

---

## 2. 修正后的 backlog

计划 §3 原列 P6–P11。本次审计**新增** Tier B(计划漏项),并修正若干认知。

### Tier A —— parity 关键(计划已列)

| 项 | 缺口实测证据 | 验收 |
| --- | --- | --- |
| **P6** evolution apply/rollback | `routes_admin_b/evolution.py:210 status_code=501` "apply/rollback are read-only stubs" | 提案可 apply / rollback,落库 ⚠️ **仅落库(记账)**:`apply_proposal`(`routes_admin_b/evolution.py:769`)接 `corlinman_auto_rollback.EvolutionApplier`,写 history 行 + intent-log,但**不改任何 skill/prompt 文件或 kb.sqlite**;`metrics_baseline={}` 使 auto-rollback 失效(ARCH_DEBT NEW-fhB-1 / NEW-fhfunc-3) |
| **P7** 语音真实 provider | `routes_voice/provider.py:1` "trait surface + a mock";无 OpenAI Realtime 适配器 | `/v1/voice` 接真实 realtime 语音 |
| **P8** Bedrock / Azure provider | `market_providers.py:122/142` `raise NotImplementedError` | 两家 provider 真实可用(SigV4 / deployment 路由) |
| **P10** 频道广度 | `corlinman-channels` 仅 `run_qq_channel` / `run_telegram_channel` | 新增高优先频道(Discord / Slack / 飞书…)收发通 |
| **P11** 配置热重载接线 | `config_watcher.py` 存在;`lifecycle/*.py` 零引用 → 未启动 | 改 config.toml 热生效 |

### Tier B —— 计划漏项(本次审计发现)

| 项 | 为什么是缺口 | 验收 |
| --- | --- | --- |
| **P14** MCP 接入 | corlinman 有 MCP 代码但未接 agent 工具面;hermes/openclaw 都把 MCP 当**首选扩展路径** | agent 能加载并调用外部 MCP server 的工具 |
| **P15** 内置工具广度 | P5 执行器能跑插件,但内置工具集薄;对标 web 搜索 / 浏览器 / 视觉 / 代码执行 | 补齐一批一级内置工具 |
| **P16** 工具执行器补全 | P5 显式留下 `service` / `mcp` 插件类型 `unsupported` | service 插件经 supervisor、mcp 插件经 P14 桥接执行 |
| **P17** 可观测性深化 | 仅 structlog + doctor;两个对标都有 OTel 分布式追踪 | OTel trace 导出(token/成本/延迟) |

### 认知修正

- **记忆/RAG(原 P9)**:`corlinman-embedding` 实际**已接线**(`/v1/embeddings` + admin rag/memory 路由)。原 P9"从零整合"判断过重 → 降为"深化"(session 检索、整理),并入 Tier C。
- **provider 广度(原 P8)**:`openai_compatible` kind 已让一个适配器覆盖 DeepSeek/Qwen/GLM/Groq 等多家;真正硬缺口只是 Bedrock/Azure(非 OpenAI 协议)。P8 范围据此收窄。

---

## 3. 执行波次(多 agent 派发)

> 受 harness 限制:并行**写代码** agent 上限 2–3,文件域必须不重叠。

| 波 | parcel | 状态 |
| --- | --- | --- |
| A1 | P6(evolution apply/rollback)+ P8(Bedrock/Azure provider) | ⚠️ P8 ✅;P6 **仅记账**(见下方遗留收尾 + ARCH_DEBT NEW-fhB-1 / NEW-fhfunc-3) |
| A2 | P11(配置热重载)+ P7(OpenAI Realtime 语音) | ✅ |
| B1 | P14+P16(MCP 接入 + 执行器补全)+ P10(Discord/Slack/Feishu)+ MCP 启动装配 | ✅ |
| B2 | P15(web_fetch/web_search/calculator 内置工具)+ P17(OTel span 埋点) | ✅ |
| C | RAG 深化(session 检索 / dreaming 整理) | ⏳ 推迟 |

实际执行:全部 Tier A + Tier B 用多 agent 并行派发(每波 2 个 agent,文件域不
重叠,主线集成 + 逐波跑全套件)。全套件 **2810 passed**。

遗留收尾(已知,非阻断):P6 applier 是"状态机 + 审计",真实内容变更(引擎
提示 / 技能文件)需把 kb/fs 句柄穿过 `AdminState`;P15 新内置工具需在 agent
card 的 `tools_allowed` 里登记后 persona 才会发起调用。

> **修正(诚实对齐)**:P6 的 `POST /admin/evolution/{id}/apply` 走的是
> `corlinman_auto_rollback.EvolutionApplier`,它**只写 intent-log + history 行,
> 从不改动任何 skill/prompt 文件或 `kb.sqlite`**;并且 history 行的
> `metrics_baseline` 恒为 `{}`(`applier.py:279`)。该空基线会让
> `AutoRollbackMonitor`(NEW-fhB-1)的 `MetricSnapshot.from_dict({})` 抛
> `ValueError` → fail-safe 跳过,因此 apply→monitor→rollback 整条链是**死的**。
> 此外 `AutoRollbackMonitor` 在 gateway 内**没有任何运行时 driver**
> (`cli/main.py:191` 的 `rollback run-once` 仍是 STUB,scheduler/lifespan 均未注册),
> 见 ARCH_DEBT NEW-fhfunc-3。注意:让 agent 自我变更 skill/prompt 文件属**高风险**
> 能力,落地前需产品决策。
