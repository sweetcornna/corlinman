# Runbook

给自己部署 corlinman 的用户和运维工程师用的"出了问题看这里"手册。每条都是"一句问题 + 一段
解决"，按遇到频率从高到低排。

前置：你已经运行 `corlinman onboard` 完成了初装。

## 1. `corlinman doctor` 报问题该怎么修

`corlinman doctor` 分模块运行检查。M7 起覆盖 8 个模块（后续还会扩，目标 50+）：

| module        | 说明 |
|---------------|------|
| `config`      | `config.toml` 解析 + 跨字段校验 |
| `manifest`    | `data_dir/plugins/*/plugin-manifest.toml` 扫描 |
| `upstream`    | enabled providers 的 `api_key` 能否解析 |
| `sqlite`      | `data_dir/vector/chunks.sqlite` 打得开，FTS5 可用 |
| `usearch`     | `data_dir/vector/index.usearch` 打得开，维度匹配 embedding 模型 |
| `channels`    | `[channels.qq]` 若启用，ws_url 合法且 2s 握手尝试 |
| `scheduler`   | 每个 `[[scheduler.jobs]].cron` 用 `cron` crate 能 parse |
| `permissions` | `data_dir` 及 `plugins/agents/knowledge/vector/logs` 子目录存在且可读写 |

每个 check 输出 `✓ OK / ! WARN / ✗ FAIL` + 简短 hint。

- **FAIL `config.toml` 解析失败**：看 hint 里的 key 和行号，通常是引号不匹配或缩进错误。
- **FAIL `upstream.anthropic` 429 / 5xx**：provider 不可达或 key 失效。`echo $ANTHROPIC_API_KEY` 确认环境变量真的注入进容器。
- **FAIL `sqlite.FTS5 unavailable`**：容器里的 SQLite 没带 FTS5，重打镜像或换宿主 libsqlite。
- **FAIL `usearch open_checked(dim=N) failed`**：索引维度和 `[rag] embedding_model` 不匹配，`corlinman vector rebuild` 重建。
- **FAIL `scheduler.jobs[i] invalid cron`**：corlinman 用 7 字段 cron（秒 分 时 日 月 周 年）。
- **WARN `manifest.duplicates`**：发现同名 manifest 有多条；`corlinman plugins inspect <name>`
  看所有候选，删掉不要的。
- **WARN `channels.qq ws unreachable` / `ws connect timed out`**：gocq/NapCatQQ 没起或 `ws_url` 配错。
- **WARN `permissions.missing subdir(s)`**：运行 `corlinman onboard` 建好 layout。

每一类 FAIL 都有对应的 run subcommand 做 deep-dive，如 `corlinman doctor --module upstream -v`。
`corlinman doctor --json` 输出结构化结果，适合 CI/监控吃。可用 `--module <name>` 单跑一项。

## 2. `/health` 返回 degraded

`curl http://localhost:6005/health` 返回结构：
```json
{
  "status": "degraded",
  "checks": [
    {"name": "config", "status": "ok"},
    {"name": "agent_grpc", "status": "ok"},
    {"name": "sqlite", "status": "ok"},
    {"name": "usearch", "status": "warn", "detail": "index file mtime > 24h stale"},
    {"name": "plugin_registry", "status": "ok"},
    {"name": "channels.qq", "status": "fail", "detail": "ws disconnected"}
  ]
}
```

整体 status 取 worst：任何 `fail` → `unhealthy`；任何 `warn` 无 fail → `degraded`。排查
顺序：先看 `fail` 条目的 `detail`，基本能直接告诉你去哪查；再看 `warn`，通常可容忍。

外部健康探针建议只认 `unhealthy`（降流量），不认 `degraded`（继续吃流量，但告警）。

## 3. 用 `request_id` + `trace_id` 关联 Rust ↔ Python

每个请求在进 gateway 时生成 `request_id`（UUID v4），`traceparent` header 生成或继承 W3C trace
context 的 `trace_id`。Rust 侧通过 `tracing::info_span!` 注入，Python 侧通过
`structlog.contextvars` 注入，**字段名两端一致**。

排查流程：
```bash
# 1. 客户端报错，拿到 request_id（客户端应当日志它，如果没日志看响应 header X-Request-Id）
export RID=req_abc123

# 2. 在 gateway 日志捞
docker logs corlinman 2>&1 | grep "request_id=$RID"

# 3. 拿到 trace_id 后捞 Python 日志
export TID=0af7651916cd43dd8448eb211c80319c
docker logs corlinman 2>&1 | grep "trace_id=$TID"
# Python 日志也在同一个容器 stdout（gateway 汇聚）
```

`subsystem` 字段告诉你这条日志来自哪：`gateway.routes.chat` / `agent-client` /
`plugins.runtime` / `python.agent.reasoning_loop` / `python.providers.anthropic`。

## 4. 插件被 OOM kill

症状：Agent 调用某工具，gateway 返 "plugin execution failed"。

```bash
# 查指标
curl http://localhost:6005/metrics | grep corlinman_plugin_execute_total
# 看有没有 {plugin="X",status="oom"} 这个 series 且计数在涨
```

修复：
- 临时：在 `~/.corlinman/plugins/<name>/manifest.toml` 的 `sandbox.memory` 从 `"256m"` 调到 `"512m"` 或 `"1g"`，manifest watcher 60s 内自动 reload。
- 长期：看插件代码是不是有内存泄漏（未释放的 buffer）；或数据规模超预期。

## 5. upstream LLM 429，退避是否起效

gateway 的 `corlinman-agent-client::retry` 按 `DEFAULT_SCHEDULE = [5s, 10s, 30s, 60s]` 指数
退避。metric 验证：
```bash
curl http://localhost:6005/metrics | grep corlinman_backoff_retries_total
# corlinman_backoff_retries_total{reason="rate_limited"} 34
# corlinman_backoff_retries_total{reason="upstream_5xx"} 7
```

`reason` 字段取值见 `corlinman-core::error::FailoverReason` enum：`rate_limited` /
`upstream_5xx` / `upstream_timeout` / `upstream_invalid_response` / `network`。

如果 `rate_limited` 计数猛涨但最终请求还是失败（`corlinman_http_requests_total{status="5xx"}`
也涨），说明 4 档退避也扛不住，需要：
- 降低并发（客户端侧或在 gateway 加 rate limit，M7 引入）
- 切换到备用 provider：`ModelRedirect.json` 配好 fallback chain
- 临时提升 provider quota

## 6. RAG 结果不对，usearch 重建

症状：Agent 回答明显漏掉 dailynote 里有的内容、或检索出无关旧笔记。

步骤：
1. 先确认是检索问题还是 LLM 没读上下文：`CORLINMAN_LOG_LEVEL=debug`，搜
   `subsystem=python.agent.context_assembler` 看实际注入了哪些 hit。
2. 如果检索本身差：
   ```bash
   corlinman vector stats                  # 看文档数、索引 size、上次更新时间
   corlinman vector query "你的问题" -k 10  # 直接查索引
   ```
3. 确定索引过时或损坏，重建：
   ```bash
   corlinman vector rebuild --source ~/.corlinman/knowledge --confirm
   ```
   这会：新建 `.usearch.new` → 重跑 embedding → 原子 rename 到 `.usearch`。期间 gateway 仍用老索引。完成后读新索引。如出错原文件未动。
4. 如果是 `config.toml` 的 `[rag]` 段参数调错导致 RRF 融合偏移，`corlinman config diff` 对比 default。

## 7. QQ bot 重连循环

症状：`subsystem=channels.qq` 日志每几秒一条 `reconnecting...`。

排查顺序：
1. `curl /health` 看 `channels.qq` 是 `fail` 还是 `warn`。
2. 确认 gocq/Lagrange/NapCatQQ 端活着：看它自己的日志或 web 管理页。
3. 两边 WS URL 配对：`config.toml` 的 `[channels.qq] ws_url` 是不是正确指向 WS server。
4. 登录态：扫码过期会失败，重扫。
5. 确认没有两个 corlinman 实例在抢同一个 WS 连接。

### QQ 扫码页打不开、二维码不刷新、或者提示 NapCat 连接异常

NapCat 的 `RefreshQRcode` API 只表示“刷新请求已发给 QQ 登录内核”，不保证返回时
`GetQQLoginQrcode` 已经换成新 URL。corlinman 的 gateway 会检测刷新前后 QR 是否相同；
如果 NapCat 继续返回旧码，会请求 `/api/QQLogin/RestartNapCat`，等待 NapCat 被
systemd/docker 拉起后再确认新码。

当前标准路径是 gateway 自己接管 NapCat WebUI：

- `/webui` 和 `/webui/*` 由 gateway 代理到已解析的 NapCat WebUI。
- `/api/QQLogin/*`、`/api/OB11Config/*`、`/api/auth/*` 由 gateway 窄代理到 NapCat。
- `/api/QQLogin/RefreshQRcode` 不直通 NapCat，而是走 gateway 的强刷新路径。

因此新部署不需要再手写一组 nginx NapCat location。旧部署如果仍然在 nginx 里直接反代
NapCat，需要确保 WebUI 自己发出的刷新请求先 exact-match 到 gateway，放在通用 `/api/`
反代之前：

```nginx
location = /api/QQLogin/RefreshQRcode {
    proxy_pass http://localhost:6005/api/QQLogin/RefreshQRcode;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

同时确认 gateway 和 NapCat 使用同一个 WebUI token：

- docker/compose：设置 `NAPCAT_WEBUI_TOKEN`；compose 会传给 NapCat 的 `WEBUI_TOKEN`。
- native/systemd：`WEBUI_TOKEN` 由 NapCat 读取；gateway 同时兼容
  `NAPCAT_WEBUI_TOKEN`、`NAPCAT_WEBUI_SECRET_KEY`、`WEBUI_TOKEN`。
- 如果 `/internal/napcat-credential` 没有返回 `X-Napcat-Credential`，说明 gateway
  仍然不知道 NapCat 的 WebUI token。

从 QQ 管理页打开扫码弹窗时，顶部会显示 NapCat diagnostics。也可以直接查：

```bash
curl -u admin:你的密码 http://localhost:6005/admin/channels/qq/napcat/diagnostics
```

字段含义：

- `mode=managed`：使用 corlinman 默认管理的 NapCat（Docker/native 默认）。
- `mode=external`：使用 `[channels.qq].napcat_url` 指向的用户自带 NapCat。
- `credential=missing_token`：缺 `napcat_access_token` / `NAPCAT_WEBUI_TOKEN` / `WEBUI_TOKEN`。
- `qrcode_api=unreachable`：gateway 到 NapCat URL 不通，先检查 `napcat_url`、端口、容器网络。
- `onebot_config_api=failed`：NapCat 可达，但 OB11 配置 API 不兼容或拒绝请求。

用户自带 NapCat 时，可以在 QQ 通道配置里设置 `napcat_url` 和 `napcat_access_token`；
这两个值会进入同一套 diagnostics 和二维码强刷新路径。

## 8. 定时任务没触发

`corlinman-scheduler` 启动时把 `config.toml` 的 `[[scheduler.jobs]]` 里配的 cron job 注册到 `tokio-cron-scheduler`。排查：

1. Admin UI 的 `/admin/scheduler` 页看任务列表和下次触发时间。列表空的话 config 没读到。
2. 手动触发验证任务本身 OK：
   ```bash
   corlinman scheduler trigger <job-name>
   ```
3. 如果手动 OK 但定时不跑，检查时区：`TZ` 环境变量 + cron 表达式是否匹配。Docker 默认 UTC；
   国内用户通常要 `-e TZ=Asia/Shanghai`。
4. 日志搜 `subsystem=scheduler`，`level=warn` 以上的看有没有异常。

## 9. 优雅关机

corlinman 对 SIGTERM 的约定：**停接新请求 → 抽干 inflight（默认 5s） → 关 gRPC stream → flush
日志 → 退出码 143**。Docker 默认 `stop_grace_period=10s`，够用。

强制关停用：
```bash
docker kill -s SIGKILL corlinman
```

这会打断 inflight 请求（客户端看到连接断），紧急时才用。

**你的 compose**：设 `stop_grace_period: 15s`，留余量给 flush 和 Python 子进程收尾。

**退出码含义**：
- `0` —— 正常 shutdown
- `143` —— SIGTERM 正常处理
- `137` —— SIGKILL 强停
- 其他非 0 —— 异常崩溃，看日志最后几行 panic 信息

## 9.5 SSE 响应被 Nginx / 反代 buffer，客户端看到"憋一阵再一起下来" (added 2026-04-20)

症状：直连 gateway `:6005` 时 SSE 流畅，接入 Nginx / Traefik / 云厂商 LB 后客户端体验变成
"等几秒一次性返回一大段"。

根因：反代默认对 HTTP 响应开启 buffering，SSE 流被 buffer 吃掉了 per-event 边界。

**修法 1（gateway 侧，1.0.x 已内置）**：所有 `text/event-stream` 响应自动加
`X-Accel-Buffering: no` header。Nginx 识别此 header 跳过 buffering。若仍不生效检查 Nginx
是否 `proxy_pass_header X-Accel-Buffering` 开启（默认开）。

**修法 2（反代侧，稳妥）**：在 Nginx location 里显式配：
```nginx
location /v1/chat/completions {
    proxy_pass http://localhost:6005;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600s;
    proxy_http_version 1.1;
    chunked_transfer_encoding off;
}
```

**验证**：客户端看到第一个 `data:` event 的墙钟时间应 ≤ 200ms（upstream provider 已开流后）。
gateway 侧 metric `corlinman_chat_stream_duration_seconds` 的 first-byte 不会受反代 buffer
影响，但客户端肉眼能看到差异——这个差值就是反代 buffer 吃掉的 lag。

计划文件 §14 R9 对此有提及。

## 10. 升级新版本

生产升级一律 pin 明确 tag，不要直接把 `main` 当生产版本。

Docker / compose 场景：
```bash
export VERSION=vX.Y.Z
# 确认 compose 文件或 .env 使用 ghcr.io/sweetcornna/corlinman:${VERSION}
docker compose pull corlinman
docker compose up -d corlinman
curl -fsS http://localhost:6005/health
corlinman doctor
```

如有问题，在 compose 里把镜像 tag 改回上一版并 `docker compose up -d corlinman`。

Hosted demo VPS (`corlinman.cornna.xyz`) 是特殊的 native/systemd 部署：
`corlinman.service` 跑 gateway，`corlinman-agent.service` 跑 Python agent，
静态 UI 由 nginx 从 `/opt/corlinman/ui-static/` 直接服务，QQ/NapCat 仍在 Docker。
这台机器的完整发布和回滚步骤在
[`RUNBOOK_VPS_PROD_UPDATE.md`](RUNBOOK_VPS_PROD_UPDATE.md)。

这台 VPS 目前不要跑通用 `install.sh --upgrade`，因为它会重写 native systemd
unit；当前生产机的 venv 和服务单元仍是 root-owned 布局。迁移到 unprivileged
unit 要单独排期。

**永远不要**跳过非 patch 版本升级（比如 1.0.x 直接跳到 1.2.0）。按 minor 顺序升，每次升完
跑 `corlinman doctor`、`/health` 和一次真实请求。

**数据向后兼容**：1.x 任意版本的数据 1.x 任意版本都能读。2.0 会有一次 `corlinman migrate` 流程，届时补充 migration 文档。

## 11. `/metrics` 指标清单（M7 起）

gateway 暴露 `GET /metrics`（Prometheus text exposition v0.0.4）。完整 metric family：

| 名称                                          | 类型      | labels              | 埋点位置                                    |
|-----------------------------------------------|-----------|---------------------|---------------------------------------------|
| `corlinman_http_requests_total`               | counter   | `route`, `status`   | `corlinman-gateway::middleware::trace`      |
| `corlinman_chat_stream_duration_seconds`      | histogram | `model`, `finish`   | `routes::chat::chat_stream` (TODO wall-timer) |
| `corlinman_plugin_execute_total`              | counter   | `plugin`, `status`  | `corlinman-plugins::runtime` (TODO wiring)  |
| `corlinman_plugin_execute_duration_seconds`   | histogram | `plugin`            | 同上                                        |
| `corlinman_backoff_retries_total`             | counter   | `reason`            | `corlinman-agent-client::retry::with_retry` |
| `corlinman_agent_grpc_inflight`               | gauge     | —                   | `agent-client::stream::ChatStream::open`    |
| `corlinman_vector_query_duration_seconds`     | histogram | `stage` (hnsw/bm25/fuse) | `corlinman-vector::hybrid::search`     |

`status` 用数字字符串（`"200"` / `"503"`）；`finish ∈ {stop, length, tool_calls, error}`；`reason` 取
`FailoverReason::as_str`（`rate_limit` / `upstream_5xx` / `timeout` / `auth` / ...）。

所有 metric 家族在 bootstrap 时就用 `label="startup"` 预先注册一条零值 series（`inc_by(0.0)` / `observe(0.0)`）
以便 Grafana 面板和告警规则能在 zero-traffic 启动阶段也匹配到 series。dashboard 侧可直接过滤掉
`{route="startup"}` / `{plugin="startup"}` / `{stage="startup"}`。

埋点所有权：gateway 定义全部 metric 句柄（`corlinman_gateway::metrics`），plugins / agent-client / vector
三个 crate 通过 import 同一组 `Lazy` 静态拿到同一 `Registry`。TODO：OpenTelemetry OTLP exporter
和 Grafana dashboard JSON（`ops/dashboards/corlinman.json`）留到下一迭代。

## 延伸阅读

- `/metrics` 完整清单：计划文件 §9 可观测性，本文件 §11
- 插件特定故障：[plugin-authoring.md §9 调试](plugin-authoring.md#9-调试)
- 哪层组件出问题去哪查：[architecture.md](architecture.md) §6 时序图
