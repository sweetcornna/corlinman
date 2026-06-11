# PLAN: 企业级聊天界面（对标 ChatGPT / Claude.ai）

> 状态：草案 v1（2026-06-11）。探索阶段：workflow `chat-ui-problem-explore`（6 agents，已完成）
> + `chat-ui-unknown-bug-hunt`（8 视角多轮，进行中——结果将合并至 §3）。
> 分支：`feat/chat-enterprise-parity`（自 `main` v1.19.1 切出）。

## 1. 背景与目标

用户报告 web 聊天界面 5 个问题：①无法展示文件 ②助手无法发送照片 ③生成图片报
network error/超时 ④无法打断发送 ⑤历史会话消息加载问题。目标：按企业级
ChatGPT/Claude 标准修复并补齐表桩（table-stakes）能力。

## 2. 诊断结论（已逐条核实代码）

### P1 文件展示 — 链路断在 5 处
- `use-chat-stream.ts:353-358`：构建请求时只发 `{role, content}`，**attachments 被完全丢弃**（UI 的选择器/拖拽/AttachmentGallery 其实已存在）。
- 后端 `routes/chat.py:82-90` `ChatMessage.content: str`——不接受 OpenAI content-parts 数组；`InternalChatRequest.attachments` 字段已存在但没人填。
- **没有文件上传/下载 HTTP 端点**；只有 <1MB 图片走 data URL（composer.tsx:175）。
- 历史不持久化附件元数据 → 刷新后附件消失。
- `ChatAttachment.kind` 与 OpenAI part 类型无转换函数。

### P2 助手发图 — 整条事件链缺媒体类型
- `InternalChatEvent` 联合（gateway_api/types.py:310-397）只有 Token/ToolCall/ToolResult/Done/Error，无媒体事件；`_sse_iter`（chat.py:537-575）同样不发。
- 前端 `ChatEvent` 联合（types.ts:139-237）无媒体变体；event-merger 不处理。
- `image_generate` 工具（corlinman-agent/image/plain.py:196-214）只返回**本地文件系统路径**，浏览器无法获取。
- 其余 7 个频道靠 `send_attachment` 工具 + 频道 runner 分发媒体；web 频道（CorlinmanChannel）不在该分发循环内，`send()` 签名也无附件参数。

### P3 生图 network error / 超时 — 根因唯一且明确
- `_sse_iter` 纯事件驱动**无心跳**；`image_generate` 默认超时 120s（CORLINMAN_IMAGE_TIMEOUT_SECS），工具执行期间 SSE 零字节 → 代理 30-60s 空闲超时杀连接 → 浏览器抛 "network error"。
- 成熟解法就在本仓库：`sessions_events.py:292`（admin 事件流）用 `asyncio.wait_for(..., 10s)` + 注释行心跳。前端 SSE 解析器（chat.ts:149）只认 `data:` 行，注释心跳天然兼容，**纯后端改动**。
- 前端无超时区分/重试 UX，裸抛 socket 错误文案。

### P4 打断发送 — 机制完整，反馈缺失（感知性 bug）
- 停止按钮、AbortController、`POST /admin/sessions/{key}/cancel`、ReasoningLoop 协作式取消、断连检测**全部已实现且有测试**。
- 但后端发出的 `Cancelling` 事件被前端**显式丢弃**（event-merger.ts:307），点停止后无任何视觉反馈，消息一直 pending 直到 TurnErrored——用户感知为"无法打断"。
- `error='cancelled'` 与真实错误渲染无区分。

### P5 历史会话加载
- `chat-area.tsx:75-87`：`initialHistory` 还是 `undefined`（query 加载中）就先 `hydrate([])`；流式进行中切换/再 hydrate 会 wipe 进行中的消息。
- `page.tsx:89`：消息 ID 用数组下标拼（`hist_${i}_${created}`）——不稳定，React key 重建 DOM、丢滚动位置；时间戳缺失时还会撞 ID。
- `_sessions_lib.py:567`：replay 硬编码 `limit=500` 且不暴露 `before_turn_id` 游标（journal 层本身支持）→ 长会话静默截断、无分页。

### 对标差距（benchmark agent，已按企业级标准筛选）
表桩级缺失：消息虚拟化（100+ 消息卡顿）、发送失败重试 UX、KaTeX 数学、续写（continue）、
移动端键盘遮挡、a11y（hover-only 按钮无键盘可达、无 aria-live）、搜索词高亮、会话导出、
附件下载、上传进度、Cmd+/ 聚焦。已具备：流式 markdown/代码高亮/复制、编辑重跑、
regenerate、分支、回复引用、Cmd+F 搜索、推理块、工具卡片、图片 lightbox。
P2 backlog（不入本期）：语音输入、分享链接、内联 mermaid、follow-up 建议、消息 pin、参数化重生成。

## 3. 未知问题挖掘结果（51 条：2 critical / 35 major / 14 minor）

> 全量清单：`audit/chat-perfect-2026-06-11/hunt-51-findings.json`（untracked 工件）。
> 8 视角 × 3 轮挖至干涸；critical 经对抗核实。按主题归并：

### 3.1 流协议契约（本期最高优先，与 P3 叠加）
- **[CRITICAL] SSE 错误块不符协议**：流式报错时后端发 `{"error":{…}}`（无 `choices`/`finish_reason`，chat.py:565-574），前端 `chunkToChatEvents` 只遍历 `chunk.choices` → 零事件 → **消息永远卡 loading**，HTTP 已锁 200 无从分辨。
- 跨轮事件污染：`reduceEvent` 不校验 `ev.turnId` vs pending.turnId；每轮重建 dedup set + 旧 EventSource 500ms 残留 → 上一轮事件混入新轮草稿。
- token 流无停滞看门狗（后端崩溃则永久 loading）；`[DONE]` 无 finish_reason 校验；流尾 buffer 不 flush（丢最后一帧）；`delta` 无空值守卫；`finish_reason: ""` 因 truthiness 被忽略；JSON parse 失败双路径静默吞。
- 后端：tool args_json `decode('utf-8')` 无容错可炸死生成器；消息内容无长度/编码校验。

### 3.2 错误处理与可靠性
- 401/会话过期全程不浮出：fetch 401 仅 set error，EventSource 静默无限重连，无重登引导。
- `approve`/`sendMessage`/`editAndRerun`/`stop` 均 fire-and-forget 无 catch；错误消息无重试按钮（hook 已有 `retryLast` 但 UI 未挂）；rename/消息编辑都在 API 完成前提前退出编辑态，失败无感知。
- 流式中会话 TTL 不复查（30min 会话可挂 2h 流）。

### 3.3 移动端与无障碍（13 条，最大簇）
- **<640px 无响应式抽屉**：侧栏恒占 256px，375px 手机几乎不可用；iOS 软键盘遮挡 composer。
- 焦点管理系统性缺失：lightbox/model-picker/搜索浮层无 focus trap、无初始焦点、关闭不还焦；emoji picker roving tabindex 破损；多处 `focus:outline-none` 无替代指示。
- 触控目标不足：气泡操作条 ~20px、附件删除钮 20px（WCAG 2.2 最低 24px）。
- 流式消息无 `role='log'`/`aria-atomic`；对比度：侧栏 ink-3、链接 accent light 模式临界。

### 3.4 渲染与会话管理
- lightbox 点图片即关闭（缺 stopPropagation）；剪贴板失败无反馈；无列表虚拟化（repo 内 logs/page.tsx 已有 useVirtualizer 先例）；空状态不区分"无会话/无搜索结果"；删除-撤销竞态；`t` 依赖缺失。

### 3.5 企业级差距（独立工作流，不入本 PR）
- **[CRITICAL] 多租户隔离缺失**：session list/delete/replay/patch 全不按 tenant 过滤，turns 表无 tenant_id 列；admin cookie 鉴权恒取 default_tenant。→ **独立 PR（W8）**，波及 journal schema + 全频道，需迁移与回归，不与 UI 改动混合。
- 数据保留策略/TTL、share 链接、消息 feedback、模型切换上下文告警、usage 展示（hook 已算 totals 未展示）→ 部分入 W6，其余 P2。

## 4. 架构决策

1. **文件服务（新基础设施，解锁 P1/P2/附件下载）**
   - `POST /v1/files`（multipart，admin-session 鉴权，复用现有 cookie bridge——**不得破坏 in-app chat auth bridge**）→ `{file_id, url, name, mime, size}`；
   - `GET /v1/files/{file_id}`（鉴权 + 正确 Content-Type/Disposition）；存储于 state 目录下 `files/`，元数据 sqlite/sidecar。
2. **用户附件**：composer 上传得 `remoteUrl` → 前端 `attachmentToContentPart()` 构建 OpenAI parts → 后端 `ChatMessage.content: str | list` + `_build_internal_request` 抽取到 `req.attachments` → journal 持久化附件元数据 → replay 返回 → AttachmentGallery 渲染历史附件。
3. **助手媒体**：不动 gRPC proto（跨进程改动大、风险高）。路径：`image_generate`/`send_attachment` 产物注册进文件服务 → 工具结果携带可服务 URL → journal 发附件事件 → event-merger 新增 `media` ChatEvent → 气泡用现有 AttachmentGallery 渲染；同时 markdown 内 `![](/v1/files/…)` 自然可显示。web 频道 `send_attachment` 进入与其他 7 频道一致的分发语义。
4. **SSE 心跳**：`_sse_iter` 照搬 sessions_events 的 10s `asyncio.wait_for` + `: ping` 注释行。
5. **Hydration 合约**：query 未 resolve 不 hydrate；流式中不 hydrate；历史 ID 改用 `turn_id+seq` 稳定生成。
6. **历史分页**：replay 端点暴露 `before_turn_id`，前端"加载更早消息"增量取。

## 5. 实施波次（文件归属互斥，避免并行写冲突）

| Wave | 内容 | 主要文件 | 执行 |
|---|---|---|---|
| W1a 后端流契约 | SSE 心跳；错误块改为合法 chunk（finish_reason='error'）+ 前端兜底识别 `chunk.error`；utf-8 decode 容错；消息长度校验 | routes/chat.py | 主循环亲自 |
| W1b 前端流状态机 | turnId 守卫防跨轮污染；stall 看门狗；buffer flush；delta/finish_reason 守卫；parse 错误浮出；Cancelling 渲染+停止反馈；cancelled/错误区分+重试按钮；401 重登引导；fire-and-forget 加 catch；hydration 修复；稳定历史 ID | use-chat-stream.ts、event-merger.ts、chat.ts、event-stream.ts、chat-area.tsx、page.tsx、message-bubble.tsx | 主循环亲自 |
| W2 文件服务 | upload/serve 端点 + 存储层 + 测试 | gateway/routes/files.py（新）、services/ | opus agent（后端独立，无冲突，与 W1 并行） |
| W3 用户附件端到端 | parts 转换、后端 Union content、journal 持久化、历史附件渲染、下载/进度 | use-chat-stream.ts、chat.ts、routes/chat.py、_sessions_lib.py、attachment-gallery.tsx | W1+W2 后串行（共享文件） |
| W4 助手发图 | 工具产物→文件服务、journal 媒体事件、media ChatEvent、气泡渲染；web 频道 send_attachment 语义 | image/plain.py、chat_service.py、event-merger.ts、message-bubble.tsx | W3 后串行（共享 event-merger） |
| W5 历史分页 | before_turn_id 透传 + 加载更早 UI；500 上限处理 | _sessions_lib.py、sessions.py、page.tsx | W1 后 |
| W6 对标增强（按文件簇分包并行派 opus） | ①markdown 簇：KaTeX、lightbox 修复（trap/stopPropagation/i18n）、链接对比度、剪贴板容错 ②列表簇：虚拟化（参照 logs/page.tsx useVirtualizer）、aria-live/log、搜索高亮 ③composer 簇：移动键盘避让、触控目标、emoji picker a11y、上传进度、Cmd+/ ④侧栏簇：响应式抽屉、rename 修复、空状态区分、对比度、撤销竞态、usage 展示 ⑤导出：markdown 导出 + model-picker focus trap | 每簇文件互斥 | 4-5 个 opus agents 并行 |
| W7 验证收尾 | vitest + pytest + playwright e2e + build + ruff/mypy；汇总 PR | — | 主循环 |
| W8 租户隔离（独立 PR） | turns 表 tenant_id 迁移 + journal 查询全链路过滤 + cookie 鉴权 tenant 解析 | agent_journal_backend.py、sessions.py、_sessions_lib.py、admin_auth.py | 本 PR 合并后单独立项 |

> 约束：内容卡禁 `backdrop-filter`（vitest 强制）；新样式只用 `sg-*`/`lg-*`；channels 与 web 共用 chat_service 时注意 duck-typed SimpleNamespace 契约（勿加必填字段）；不破坏 admin-session cookie bridge。

## 6. 验证

- 单测：vitest（event-merger、use-chat-stream、composer、hydration 回归）、pytest（files 路由、心跳、附件抽取、分页）。
- e2e：playwright——发送→流式→停止→重试；附件上传→渲染→刷新后仍在；历史切换无白屏。
- 手动：长工具（生图）期间连接保活 ≥120s；移动视口。
