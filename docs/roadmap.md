# corlinman 路线图

> 前瞻性路线图。**已发布的历史**见 [`CHANGELOG.md`](../CHANGELOG.md)（1.0 于 2026-05-24 发布，当前 **v1.27.0**）；里程碑历史见 [`milestones.md`](milestones.md)。本文只描述**尚未落地**的方向。

最后更新：2026-07-04 · 当前版本 v1.27.0（纯 Python 栈）。

---

## 现状（v1.27.0）

1.0 之后按"波（wave）"持续演进，最新完成的是 **claude-code parity waves 1–4**：跨渠道指令 + 控制台、会话管理（`--continue` / `/resume` / `/rewind`）、声明式 hooks、MCP 客户端（sampling + `tools/list_changed` + 动态广告）、摘要压缩断路器、后台 shell（`run_shell(run_in_background)` + `shell_task_output` / `shell_task_kill`）。逐版本明细见 [`CHANGELOG.md`](../CHANGELOG.md)。

---

## 近期（P1）

- **稠密向量 RAG** —— 当前检索是 SQLite FTS5（BM25）关键词检索；补齐稠密向量（HNSW）+ RRF 融合 + cross-encoder rerank（`bge-reranker-v2-m3`）的混合检索。设计见 [`PLAN_PORT_COMPLETION.md`](PLAN_PORT_COMPLETION.md)。
- **claude-code parity 收尾** —— Dim 4 sandbox-backend 抽象（XL）；Dim 5 剩余（`/mcp` 控制台命令、`.mcp.json` scopes、client resources、sampling completer 生产接线）；[#108](https://github.com/sweetcornna/corlinman/issues/108) items 1（进程级 live-registry feed）+ 2（in-progress user bubble）；Dim 9 residuals（prompt/agent evaluators、`pre_compact` / `session_*` emitters、Notification/Setup/FileChanged、exit-2 rewake、stream-json hook lines）。
- **渠道状态流对齐** —— 目前 Telegram 有可变 spinner 状态条；补齐 Discord / Slack / Feishu 的同等流式状态。
- **OIDC 登录** —— 替代 basic auth。
- **Redis journal 后端** —— 多网关 HA 现走 Postgres journal，补 Redis 选项。
- **24h soak + fuzz 语料** 进 CI。

## 长期（P2）

- **外部向量库后端** —— Qdrant / Milvus（取代/旁路内置 SQLite FTS5 检索）。
- **多租户** —— 每租户 data_dir + RBAC。
- **客户端生态** —— Canvas 渲染器、语音 I/O（Whisper STT / edge-tts TTS）、VS Code / JetBrains / Tauri 桌面与移动客户端。
- **插件 SDK 分发** —— 精品插件 gallery + npm / PyPI 包。
- **文档站点** —— `docs.corlinman.dev`。

---

## 验收门禁（合 main 前本地必过）

```bash
uv run ruff check .
uv run mypy python/packages/
uv run pytest -m "not live_llm and not live_transport"
pnpm -C ui typecheck && pnpm -C ui lint && pnpm -C ui build
bash scripts/gen-proto.sh && git diff --exit-code python/packages/corlinman-grpc/src/corlinman_grpc/_generated/
```

约定：每完成一波在 [`CHANGELOG.md`](../CHANGELOG.md) 记一段；本 roadmap 只描述计划，已发布内容不在此追加。
