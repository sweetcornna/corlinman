# Architecture

本文给准备贡献代码的开发者用。读完你应能回答：消息从 HTTP 进来到 SSE 出去经过哪几个进程；一个新 Python 模块该放哪个 package；一个 proto 字段该加到哪个 service。

**前置知识**：你看过顶层 [README.md](../README.md)，熟悉 Python asyncio 的基本概念。

> corlinman 是**纯 Python 栈**（Rust→Python 迁移已完成，`rust/` 已删除）。本文只描述"现在系统长什么样"；前瞻计划见 [`roadmap.md`](roadmap.md)，逐版本历史见 [`CHANGELOG.md`](../CHANGELOG.md)。

## 1. 高层架构

```
                 +-----------------------------+
   Client -----> |     corlinman-gateway       | <-----  Next.js UI (REST + SSE)
 (HTTP/SSE)      | (Python, FastAPI/uvicorn,   |         /admin/*
                 |  listens :6005)             |
                 +---+--------+--------+-------+
                     |        |        |
          gRPC bidi  |  gRPC  |  in-proc |  JSON-RPC stdio / gRPC
   +-----------------+  +-----+  +-----+   +---------+
   |                 |  |     |  |     |   |         |
   v                 |  v     |  v     |   v         v
+-------------------+|+---+   |+---+   | +----------------+
| corlinman_agent   |||Emb|   ||Vec|   | | plugins        |
| (Python, reasoning|||(py|   ||(py|   | | (node/py/      |
|  loop + provider) |||)  |   ||)  |   | |  bash/…,       |
+-------------------+|+---+   |+---+   | |  optional      |
                     |        |        | |  Docker sandbox)|
                     v        v        v +----------------+
              +-----------------------------+
              | upstream LLM providers      |
              | (Anthropic / OpenAI /       |
              |  Google / DeepSeek / …)     |
              +-----------------------------+

 旁路：
   - corlinman-channels (QQ/OneBot v11 · Telegram · …)  -> gateway internal ChatRequest
   - scheduler (croniter, corlinman_server.scheduler)   -> gateway state
   - embedding/vector (SQLite FTS5 BM25)                -> /admin/rag
```

客户端只需要知道 `:6005`，其他都是内部的。gateway 本身是 Python（FastAPI/uvicorn）进程；agent reasoning 走 `grpc.aio` server，监听 `/tmp/corlinman-py.sock`（UDS，`CORLINMAN_PY_SOCKET` 覆盖，TCP 回退 `127.0.0.1:50051`）。单容器单进程树。

## 2. 为什么用 gRPC（而不是 HTTP）连接 gateway ↔ agent

1. **流式天然双向**：Agent 的 Chat 是 `stream ClientFrame ↔ stream ServerFrame`，REST/SSE 做不到真正的双向，需要 client 额外发 POST 回传 tool result，时序复杂。
2. **schema 强类型**：proto 定义一次，两端代码生成，不会漂移；HTTP/JSON 靠 docs 同步等于没同步。
3. **取消语义**：gRPC 有内建 cancellation，客户端 drop stream 服务端立刻收到；裸 HTTP 要自己约定。
4. **metadata 通道**：`traceparent` / `request_id` 走 metadata，不污染 payload。

Provider SDK 生态（anthropic / openai / google-genai 官方 SDK）、embedding、agent reasoning loop 全在 Python 侧迭代，所以整个栈统一用 Python + `grpc.aio`。

## 3. Python package 图

所有包在 `/Users/cornna/project/corlinman/python/packages/`（现有 25+ 个包）。workspace 清单在 `/Users/cornna/project/corlinman/pyproject.toml`。下图是核心依赖骨架：

```
              +---------------------+
              |   corlinman_grpc    |  (grpcio-tools 产物 + py.typed)
              +----------+----------+
                         ^
       +-----------------+-----------------+
       |                 |                 |
+------+---------+ +-----+-------+ +-------+---------+
| corlinman_     | | corlinman_  | | corlinman_      |
| providers      | | embedding   | | agent           |
| (anthropic/    | | (local pool | | (reasoning_loop |
|  openai/google/| |  + remote   | |  + context_     |
|  deepseek/qwen/| |  client)    | |  assembler +    |
|  glm)          | +------+------+ |  session +      |
| failover.py    |        |        |  approval_gate) |
+-------+--------+        |        +-------+--------+
        ^                 |                ^
        |                 |                |
        +-----------------+----------------+
                          |
                 +--------+---------+
                 | corlinman_server |  (FastAPI gateway + grpc.aio server 入口)
                 +------------------+
```

核心包职责：

| package | 一句话 |
| --- | --- |
| `corlinman_grpc` | grpcio-tools 生成 stub + `py.typed`，其他包从这里 import |
| `corlinman_providers` | `CorlinmanProvider` Protocol 和 per-vendor 实现 + registry + failover |
| `corlinman_agent` | `reasoning_loop.py`（自建，不依赖 LangChain）+ context_assembler + session + coding tools |
| `corlinman_embedding` | 本地 `ProcessPoolExecutor` 绕 GIL，或走 remote embedding 服务 |
| `corlinman_server` | FastAPI gateway + `grpc.aio` server 入口、traceparent middleware、SIGTERM 143、scheduler、rag_store |

其余包（channels / mcp / hooks / canvas / cli / plugins / …）在此骨架之上分层。统一规约：配置用 pydantic v2 strict，日志用 structlog + JSON，异常继承 `CorlinmanError`。

## 4. proto 服务速览

所有 `.proto` 在 `/Users/cornna/project/corlinman/proto/corlinman/v1/`。`grpcio-tools` 在构建步骤消费这份 IDL。

| service | 方向 | 作用 |
| --- | --- | --- |
| `common.proto` | 无 service，只有类型 | `Message` / `Role` / `Usage` / `TraceContext` / `ErrorInfo` / `ChannelBinding` / `FailoverReason` enum |
| `Agent` (agent.proto) | gateway → agent server（Python↔Python） | **核心**。`rpc Chat(stream ClientFrame) returns (stream ServerFrame)`，承载 chat 流水线 |
| `LLMProvider` (llm.proto) | Python 内部 | `Chat` / `Complete`，provider 抽象 |
| `Embedding` (embedding.proto) | gateway → agent server | `Embed(text)` / `EmbedBatch(stream)` |
| `Vector` (vector.proto) | 反向 service | `Query(RagQuery) → RagResult` / `Upsert(stream)`；context_assembler 调 |
| `PluginBridge` (plugin.proto) | 反向 service | `Execute(ToolCall) returns (stream ToolEvent)`；收到 LLM 返回的 tool call 后调这里执行 |

**反向 gRPC**：gateway 与 agent server 之间既有正向（调 Agent/Embedding）也有反向 service（注册 Vector/PluginBridge），全部由 Python `grpc.aio` server 实例监听 UDS。

**字段规范**：`args_json` / `result_json` / `payload_json` 一律用 `bytes` 零拷贝 JSON，不用 `google.protobuf.Any` 也不用 `Struct`——避免 proto runtime 反复解析 / 序列化。`traceparent` 和 `request_id` 走 gRPC metadata 而非 payload。

## 5. 关键跨进程流：`/v1/chat/completions` streaming

这是整个系统最热的路径，值得逐跳看一遍。

```
 Client         gateway              (grpc client)      Python agent     provider         plugin
   |                |                      |                  |              |              |
   | POST /v1/chat  |                      |                  |              |              |
   |--------------->|                      |                  |              |              |
   |                | auth + trace span    |                  |              |              |
   |                | model 路由解析       |                  |              |              |
   |                | cancel scope 建立     |                  |              |              |
   |                |--Chat(bidi open)---->|                  |              |              |
   |                |                      |--ClientFrame::-->|              |              |
   |                |                      |  Start(msgs,tools, session_key, |              |
   |                |                      |        placeholders, trace)     |              |
   |                |                      |                  | context_     |              |
   |                |                      |                  |  assembler   |              |
   |                |                      |                  |  (RAG 注入 / 占位符一次替换) |
   |                |                      |                  |-provider.chat_stream------->|
   |                |                      |<-ServerFrame::TokenDelta--------|              |
   |<------SSE------|                      |                  |              |              |
   |  data: {delta} |                      |                  |              |              |
   |                |                      |<-ServerFrame::ToolCall----------|              |
   |                |                      |  (OpenAI 标准 tool_calls JSON)  |              |
   |                | approval middleware  |                  |              |              |
   |                |  (config.toml)       |                  |              |              |
   |                |  若 prompt 模式:     |                  |              |              |
   |                |   SSE awaiting_approval                 |              |              |
   |                |   阻塞 future        |                  |              |              |
   |                |--registry.execute--->|                  |              |              |
   |                |                      |              JSON-RPC stdio / gRPC              |
   |                |                      |                      |------------------------>|
   |                |                      |                      |<------------------------|
   |                |<-ToolResult(call_id, payload_json)-----------|                         |
   |                |--ClientFrame::ToolResult--------------->|                              |
   |                |                      |                  | provider 续 loop            |
   |                |                      |<-TokenDelta / ToolCall / Done------------------|
   |<------SSE------|                      |                  |                             |
   | data: [DONE]   |                      |                  |                             |
```

**背压**：用 `asyncio.Queue(maxsize=16)` 控制流。

**Cancellation 全链路**：客户端断开 TCP → FastAPI/uvicorn 检测到 disconnect → 取消 asyncio task → gRPC stream 关闭 → agent 抛 `CancelledError` → `asyncio.timeout` 上下文退出 → provider client 的 aiohttp session `close()`。测试矩阵里有随机断链的 soak job 保证无僵尸。

**失败路径**：provider 报 429/5xx → `corlinman_providers.failover` 按 `FailoverReason`（`rate_limit` / `billing` / `auth` / `auth_permanent` / `timeout` / `model_not_found` / `format` / `context_overflow` / `overloaded`）分类 → 回 `ServerFrame.ErrorInfo{retryable=true}` → gateway 侧退避重试助手按 `DEFAULT_SCHEDULE = [5,10,30,60]s` 指数退避。超最后一档还不行直接 500 给客户端。

## 6. 数据与配置组织

corlinman 数据默认放 `~/.corlinman/`：

```
~/.corlinman/
├── config.toml                    # 主配置
├── agents/                        # Agent markdown + frontmatter
│   └── <name>.md
├── plugins/                       # 插件目录
│   └── <plugin-name>/
│       ├── manifest.toml
│       └── ...
├── knowledge/                     # RAG 知识库原文
│   └── <collection>/
│       └── *.md
├── kb.sqlite                      # 知识块 + FTS5 (BM25) 索引
├── sessions.sqlite                # 会话历史
└── logs/                          # rolling daily
    └── corlinman.log.YYYY-MM-DD
```

可用 `--data-dir` 或 `CORLINMAN_DATA_DIR` 覆盖。Docker 默认挂到 `/data`。检索是 SQLite FTS5（BM25）；稠密向量索引在路线图上（[`roadmap.md`](roadmap.md)）。

`config.toml` 分段示例：

```toml
[server]
port = 6005
bind = "0.0.0.0"

[admin]
username = "admin"
password_hash = "$argon2id$..."

# Providers 是一个自由命名的 map — 表键由运维选定，`kind` 字段选择线协议。
# 六个 legacy 槽名（anthropic / openai / google / deepseek / qwen / glm）会
# 推断 kind 以向后兼容；其他名字必须显式写 `kind = "..."`。
# 完整参考：docs/providers.md。
[providers.openai]
kind = "openai"
api_key = { env = "OPENAI_API_KEY" }
base_url = "https://api.openai.com/v1"
enabled = true

[providers.openrouter]
kind = "openai_compatible"
api_key = { env = "OPENROUTER_API_KEY" }
base_url = "https://openrouter.ai/api/v1"
enabled = true

[models]
default = "claude-sonnet-4-5"

[[approvals.rules]]
plugin = "file-ops"
tool = "file-ops.write"
mode = "prompt"

[channels.qq]
enabled = true
ws_url = "ws://127.0.0.1:3001"
self_ids = [123456789]
```

## 7. 可观测性与运行时

**日志**：gateway 和 agent 两侧都用 `structlog` + JSON 输出到 stdout，汇聚到容器日志。字段名对齐：`request_id` / `trace_id` / `subsystem` / `level` / `ts` / `msg`。

**追踪**：W3C `traceparent` 走 gRPC metadata 跨进程透传，接入 OpenTelemetry SDK + OTLP exporter，trace 可在 Jaeger/Tempo 上可视化。跨进程日志用 `request_id` + `trace_id` 关联（见 [runbook §3](runbook.md)）。

**metrics**：`/metrics` 暴露 Prometheus 指标（7 个 metric family：QPS、延迟、tool-call rate、backoff、stream inflight、RAG 阶段耗时、插件执行时长）。metric 句柄集中定义在 `corlinman_server.gateway.core.metrics`。bundled Grafana dashboard 见 `ops/dashboards/corlinman.json`。

**Docker ENTRYPOINT**：`tini` 负责转发信号；SIGTERM 传到 Python gateway 后由其 shutdown handler 优雅收尾（关 grpc.aio server、channel 任务、provider session）。

## Protocols reserved for device clients

The gateway ships one wire contract for future device-class clients
(iOS / Android / macOS / Linux / Electron):

- **NodeBridge v1** — WebSocket + JSON at `config.nodebridge.listen`
  (default `127.0.0.1:18788`). Registration + heartbeat + dispatch +
  telemetry. No native client is built from this repo. See
  [`protocols/nodebridge.md`](protocols/nodebridge.md).

## 延伸阅读

- Provider 配置 reference + 14 种 `kind` 表 + 常见 recipe（Ollama/OpenRouter/SiliconFlow）：[providers.md](providers.md)
- 跨进程通道更多细节：`proto/corlinman/v1/agent.proto` 的注释
- 插件运行时的类型层次：[plugin-authoring.md](plugin-authoring.md)
- 每个 package 的内部模块：该 package 目录下的 `README.md`
- 当前里程碑历史：[milestones.md](milestones.md)；前瞻路线图：[roadmap.md](roadmap.md)

## See also

- [Quickstart](quickstart.md) — 60 秒首启动 + 默认密码轮换 + skip-to-mock 路径
- [Profiles](profiles.md) — 多 agent 隔离实例（persona + memory + skills + state）
- [Credentials](credentials.md) — provider key 管理页（EnvPage 风格）
- [Evolution & Curator](evolution-curator.md) — hermes-agent 自我进化机制移植
