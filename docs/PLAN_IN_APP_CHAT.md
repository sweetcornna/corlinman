# 计划：In-App Chat（hermes 驱动的全功能聊天前端）

> 综合 4 路深度调研（hermes 后端 / channels 系统 / UI 架构 / 参考产品基线）后产出。
> 目标：在 admin UI 内交付一个对标 Claude.ai 体验、且与 telegram/qq 等渠道功能对齐的聊天窗口，最终也成为 channels 抽象下的一个 "web" 通道。

---

## 0. 目标与非目标

**目标**
- 在 `/chat` 路径上线一个生产级聊天前端，端到端驱动 hermes-style agent（流式 token、工具卡、子 agent、审批）。
- 一期就能替代 playground 当主用，二期补齐 artifact / 分支 / 编辑 / 语音 / 斜杠命令。
- 三期把它作为 channels 抽象下的 **WebChannel**，与 telegram / qq / discord 共享路由层；附带把 telegram 缺失功能（编辑/删除/置顶/搜索/已读回执）一并补齐。

**非目标（明确推迟）**
- 多用户群聊 / 多人协作编辑（无产品需求信号）
- Sticker / GIF 选择器 / 投票 / 阅后即焚（Telegram 噱头特性，价值低）
- 完整 RBAC / 审计日志（待企业化阶段再做）

---

## 1. 核心架构决策

### 1.1 后端通信路径：分层复用，不做 gRPC-web 桥

| 选项 | 评估 | 决策 |
| --- | --- | --- |
| A. 浏览器直连 gRPC bidi (`Agent.Chat`) | 需要 gRPC-web 网关 + proto 客户端打包，前端复杂度高 | ❌ |
| B. 仅靠现有 `/v1/chat/completions` (OpenAI 兼容) | playground 已用此路径流式；快但拿不到 tool_call / subagent / approval / journal 事件 | ❌ 单用 |
| **C. 双通道：`/v1/chat/completions` 收发 token + `/admin/sessions/{key}/events/live` SSE 收 hermes 富事件** | 复用所有现成基础设施；前端把两路合流为统一事件流 | ✅ **采用** |

**契约要点**
- 发消息：`POST /v1/chat/completions` `{ model, messages, stream: true, metadata: { session_key } }` → SSE token deltas
- 富事件订阅：`GET /admin/sessions/{session_key}/events/live` （SSE，`EventEnvelope` JSON 流：TurnStart / BlockStart / TextDelta / ReasoningDelta / ToolStateRunning / ToolStateCompleted / SubagentSpawned / SubagentEvent / SubagentCompleted / AwaitingApproval / TurnComplete）
- 历史回放：`GET /admin/sessions/{key}/turns` + `GET /admin/sessions/{key}/turns/{turn_id}/events`
- 会话管理：`GET /admin/sessions`、`POST /admin/sessions/{key}/delete`、`PATCH /admin/sessions/{key}` （新增 rename/pin/archive 字段）
- 中止：调用方读 SSE 时主动 `AbortController.abort()` + 调用新接口 `POST /admin/sessions/{key}/cancel` 触发后端 `ReasoningLoop.cancel()`
- 工具审批：前端收到 `AwaitingApproval` 后渲染卡片，用户决定时 `POST /admin/sessions/{key}/approvals/{call_id} { approved, scope }`（后端目前 `approval_gate.py` 是 stub，需补实现）

**后端少量缺口（Wave 1 即补）**
- `/admin/sessions/{key}/events/live` SSE 路由（W1.3 已规划但未实现）
- `/admin/sessions/{key}/cancel` 中止接口
- `/admin/sessions/{key}/approvals/{call_id}` 审批接口
- session 元数据扩展字段（title / pinned / archived）

### 1.2 与 channels 抽象的关系

- **Wave 1–2**：聊天前端**不**走 channels 层，直接走 hermes 服务（更简单）。
- **Wave 3**：在 `python/packages/corlinman-channels` 新增 `WebChannel`（实现 `Channel` Protocol），把浏览器消息映射为 `InboundEvent` + `ChannelBinding("web", account, thread, sender)`。这样路由 / keyword filter / 多账号开关一视同仁，且 telegram 已有的 admin 观测面板可直接为 web 通道复用。

### 1.3 复用 vs 新建（来自 UI 调研）

| 项目 | 来源 | 用法 |
| --- | --- | --- |
| 布局 / 侧栏 / 鉴权 | `ui/app/(admin)/layout.tsx` | 新页面直接套用 |
| 设计令牌 / 玻璃面板 | `ui/app/globals.css` + `ui/components/ui/glass-panel.tsx` | 0 新 CSS |
| 消息气泡基础形态 | `ui/components/sessions/transcript-view.tsx` | 抽离为通用 `MessageBubble` |
| Markdown 渲染 | `ui/components/system/release-notes.tsx`（react-markdown + rehype-sanitize） | 抽出 `<MarkdownMessage />` 共享 |
| 代码块高亮 | 新增（用 `shiki` 或 `react-syntax-highlighter`） | Wave 1 引入 shiki |
| SSE 客户端 | `ui/lib/sse.ts` + `ui/lib/sessions/event-stream.ts` | 直接复用 |
| 数据获取 | tanstack React Query v5（已装） | 列表 / 历史用 useQuery |
| API wrapper | `ui/lib/api.ts` (`apiFetch`) | 新增 `ui/lib/api/chat.ts` |
| 命令面板 | `ui/components/cmdk-palette.tsx` | 斜杠命令直接复用 |
| 模型选择 | `ui/components/models/model-picker-dialog.tsx` | 嵌入 composer |
| persona 选择 | `ui/components/playground/agent-picker.tsx` | 嵌入 composer |
| sidebar 入口 | `ui/components/layout/sidebar.tsx` `OPERATOR_ITEMS` | 新增 `nav.chat` |
| i18n | `ui/lib/locales/{en,zh-CN}.ts` | 同步双语 |
| 单测 / E2E | Vitest + Playwright | 新组件均补测试 |

---

## 2. 路由与文件结构（Wave 1 落地）

```
ui/app/(admin)/chat/
  layout.tsx                # 两栏：左 ChatSidebar，右 ChatArea
  page.tsx                  # 默认页（空态 / 最近会话）
  [sessionKey]/page.tsx     # 具体会话视图

ui/components/chat/
  chat-sidebar.tsx          # 会话列表 + new chat + 搜索 + 折叠分组
  chat-area.tsx             # 头部（标题 + 模型/persona/设置）+ 消息流 + composer
  message-list.tsx          # 虚拟化滚动 + 自动跟随
  message-bubble.tsx        # 用户/助手/系统三态 + hover toolbar
  message-actions.tsx       # copy / regenerate / edit / branch / delete
  markdown-message.tsx      # react-markdown + rehype-sanitize + shiki
  tool-call-card.tsx        # 折叠：工具名 / args(JsonView) / result / 状态
  subagent-card.tsx         # 嵌套子 agent 事件流
  reasoning-block.tsx       # Claude thinking 折叠块
  approval-prompt.tsx       # 内联审批 [Approve][Deny][Always]
  composer.tsx              # 多行输入 + 附件 + 模型/persona/slash + send/stop
  composer-attachments.tsx  # 已附加文件 chip + 拖拽区
  composer-slash-menu.tsx   # / 触发的命令面板
  empty-state.tsx           # 首次进入提示
  __tests__/                # 每个组件配 Vitest

ui/lib/api/chat.ts          # streamChat / listSessions / getTurns /
                            # cancelSession / postApproval / uploadAttachment
ui/lib/chat/event-merger.ts # 把两路 SSE 合流为统一 ChatEvent 序列
ui/lib/chat/types.ts        # ChatEvent / ToolCallState / Conversation
ui/lib/chat/use-chat-stream.ts  # React hook：发消息 + 订阅富事件
```

---

## 3. 分波次交付清单

> 每波次结束都要：`npm run lint && npm run typecheck && npm run test && npm run test:e2e`，
> 通过后才能进入下一波。Wave 1 验收需在浏览器实跑黄金路径（发消息 / 流式渲染 / 工具卡 / 中止 / 切换会话）。

### Wave 1 — MVP（P0，2–3 个工作单元）

后端：
- [ ] `/admin/sessions/{key}/events/live` SSE 实现
- [ ] `/admin/sessions/{key}/cancel` 中止接口
- [ ] `/admin/sessions/{key}/approvals/{call_id}` 审批接口 + `approval_gate.py` 最小实现
- [ ] session 元数据：`title / pinned / archived` 字段 + 对应 PATCH

前端（按上表文件结构）：
- [ ] 路由 + 两栏布局
- [ ] 会话侧栏：新建 / 列表（按时间分组：今天 / 昨天 / 7 天内 / 30 天内 / 更早）/ 重命名 / 置顶 / 归档 / 删除（带 undo toast）/ 搜索（前端 fuzzy 即可）
- [ ] 消息线程：用户右、助手左、系统居中分隔；markdown + 表格 + 任务列表；代码块（shiki 高亮 + 复制 + 行号）；图片 lightbox；时间戳 hover；hover toolbar；自动滚到底 + 上滑取消跟随 + "回到底部"浮钮
- [ ] 流式：token 增量 + smooth cursor；思考链折叠；工具卡（运行中 / 完成 / 失败）；子 agent 卡（嵌套显示）；审批卡内联交互；stop / retry
- [ ] composer：自动增高文本框 / Enter 发送 Shift+Enter 换行；粘贴图片；拖放附件；附件 chip；模型选择；persona 选择；`/` 斜杠命令面板（先内置 `/clear /model /persona /reset`）
- [ ] 附件上传：图片 / PDF / 通用文件；后端走 `Attachment` 走流；预览
- [ ] sidebar 新增 `nav.chat`；i18n 双语 key
- [ ] 跨标签同步：BroadcastChannel + 来自 SSE 的 turn 更新
- [ ] 单测全覆盖；新增 Playwright e2e（黄金路径 + 中止）

### Wave 2 — 高级功能（P1）

- [ ] 右侧 artifact 面板：代码 / Markdown / SVG / Mermaid / HTML 即时预览，可关闭、调宽、detach
- [ ] artifact 版本化 + diff + revert
- [ ] 用户消息原地编辑 → 自该点 re-run（删除其后历史）
- [ ] 助手消息 regenerate → N 个版本左右切换
- [ ] 任意消息 branch / fork 到新会话
- [ ] 语音输入（MediaRecorder + 浏览器内 Whisper 或后端 STT 端点）
- [ ] 推理 trace + token / 成本计数（每条 + 全局）
- [ ] @-提及 agent / skill 选择器
- [ ] 槽位变长提示模板库（`/template`）
- [ ] 折叠超长消息（>N 行）
- [ ] PWA 安装清单 + service worker offline shell

### Wave 3 — WebChannel 后端集成（P1）

- [ ] `python/packages/corlinman-channels/src/corlinman_channels/web.py`：实现 `Channel` Protocol，把浏览器 inbound 转 `InboundEvent`，把 hermes outbound 走回 SSE 推到浏览器
- [ ] `/api/channels/web/*`：`POST /send`、`GET /events` (SSE)、`POST /typing`、`POST /edit/{msg_id}`、`POST /delete/{msg_id}`、`POST /react/{msg_id}`
- [ ] `ChannelBinding("web", account=tenant_id, thread=session_key, sender=user_id)` 桥接
- [ ] 前端切到 web-channel 路径（通过 feature flag 灰度），保留旧路径回滚
- [ ] admin 观测：复用 `ui/app/(admin)/channels/telegram/page.tsx` 模式，做 `channels/web/page.tsx` 后台观测页

### Wave 4 — Telegram 对齐 + 同步打磨（P1+ / P2 混合）

- [ ] 消息编辑（用户 48h 内）/ 删除（仅自己 / 全员）/ 置顶
- [ ] 回复引用 → composer 顶部 quote chip
- [ ] 服务端推 inline keyboard 按钮（映射审批 / 快速回复）
- [ ] 快速回复 chip（模型可在 `done` 事件附带建议）
- [ ] 会话内 Cmd+F 搜索 + 深链 msg id
- [ ] 已读回执（assistant ack 状态对异步长任务有用）
- [ ] 浏览器桌面通知 + Web Push（VAPID）
- [ ] 移动端打磨：单栏 / drawer / safe-area / 触摸目标 / 长按菜单
- [ ] a11y：完整键盘 nav / `aria-live` 流 / WCAG AA 对比度 / 减弱动画

---

## 4. 风险与缓解

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 后端 SSE live 端点缺失 | Wave 1 阻塞 | 优先补，且先实现"只回放 + 轮询"降级方案，端点上线后无缝切换 |
| approval_gate.py 是 stub | 审批 UX 无法走通 | Wave 1 同步补最小实现（accept/deny/always-in-session） |
| `/v1/chat/completions` 与 hermes 事件流时序不对齐 | UI 双流合并可能错位 | `EventEnvelope.sequence` 单调；`event-merger.ts` 用 turn_id + seq 做主键合流；token 流仅作 fast-path，权威以富事件为准 |
| 大量历史会话渲染卡顿 | 体验崩塌 | 引入 `react-virtuoso` 虚拟化（200+ 消息时启用） |
| 文件上传体积 / 安全 | 后端漏洞 | 复用现有 `Attachment` 通道，前端限制 50MB + MIME 白名单；服务端单独评审 |
| Wave 3 web-channel 与现有路由层冲突 | 旧 telegram/qq 受影响 | feature flag 隔离；新通道默认 off；e2e 覆盖回归 |
| 工作量大，单 PR 难评审 | review 拖延 | 每 wave 独立 PR；wave 内若超 80 文件再拆子 PR |

---

## 5. 验收标准（Wave 1）

1. `/chat` 打开 < 1s（冷启）
2. 发送消息后首 token < 800ms（取决于 provider）
3. 流式渲染无可感知 flicker；停止按钮 200ms 内中断
4. 工具卡 / 子 agent 嵌套 / 审批 / 错误均有视觉态
5. 刷新页面或重连后通过 SSE Last-Event-ID 续流，不重复不缺漏
6. 切换会话 / 切换模型 / 切换 persona / 上传附件 / 拖放 / 粘贴 全部覆盖 e2e
7. 暗黑 / 浅色主题、中英文 i18n 切换无样式破损
8. lint / typecheck / vitest / playwright e2e 全绿
9. `_design/current-ui/` 截图与 telegram admin 风格一致（玻璃面板 + Tidepool token）

---

## 6. 执行约定

- 所有代码工作在 worktree（`EnterWorktree`）中进行，避免污染主分支
- 每个 wave 一个 PR，commit 走 conventional commits 风格（与 CHANGELOG.md 一致）
- 不破坏现有 telegram/qq channel 行为
- 不引入新的全局 CSS（一切走 Tidepool token + tailwind utility）
- 不引入未在 ui/package.json 中的新框架（如必须，新增依赖单独说明）
- 涉及秘密 / token 一律放 env / credentials store，绝不 inline 到任何源码或提交信息

---

## 7. 待用户决策的开放问题

1. **范围切片**：是按上方"4 波 → Wave 1 先上线"逐波交付（推荐），还是一次性把 Wave 1+2 打包？
2. **是否做 Wave 3 WebChannel**：如果定位仅为"内部 admin 用聊天"，可以永远不做（前端直连 hermes 已足）；如果要"作为渠道与 telegram 同档"，则 Wave 3 必做。
3. **代码高亮库选型**：`shiki`（精细、体积大）vs `react-syntax-highlighter + prism`（轻、够用）。倾向 shiki。
4. **语音输入实现**：浏览器内 Whisper（@huggingface/transformers，离线大）vs 调后端 STT 端点（依赖外部 provider）。倾向后端，Wave 2 决策。
5. **跨标签同步用 BroadcastChannel** 还是后端推送即足够？倾向 BroadcastChannel 配合 SSE。
