# Stage -1 与 Stage 0 —— 入口闸门与角色来源（configure-persona reference）
## Stage -1 — 询问默认助手风格（first-run wizard 入口闸门）

**这是整个 wizard 的第一动作**，必须在 Stage 0 之前执行。在询问任何其他问题
之前（包括 Stage 0 的"公众人物 vs 自创角色"二选一），先用 `ask_user` 询问用户
是否想直接用默认 `grantley` 助手风格、自定义人格、还是跳过。

### 采集

**第一动作**：ask_user 三选一，决定后续流程。

```
ask_user({
  "question": "想要使用默认助手风格（grantley），还是自定义一个人格？",
  "options": ["使用默认 grantley", "自定义人格", "跳过"],
  "multiple": false
})
```

### 分支处理

- **使用默认 grantley** → 调用 use-default-persona 流程（`POST
  /admin/personas/use-default`，或下发 `/use-default-persona` 指令让
  channel handler 路由）；用一句话向用户确认（例：「✅ 已为你启用默认助手
  风格 grantley，可以开始聊天了。」），**整个 wizard 在此结束，不进入
  Stage 0**。
- **跳过** → 礼貌道别（例：「好的，本次不配置人格。需要时随时可以再说
  /persona 重新启动这个向导。」），**整个 wizard 在此结束，不进入 Stage 0**。
- **自定义人格** → 照常进入 **Stage 0**（公众人物 vs 自创角色二选一），后续
  6 个阶段 + 审阅闸门完全按原契约走。

### 注意

- Stage -1 **本身不需要审阅 ask_user**——三个选项本身就是用户的最终决定，
  不存在「补充 / 修改 / 重做」。这与 Stage 0 自创分支不需要单独审阅是同一
  理由（用户的选择即确认）。
- Stage -1 是 first-run wizard 的快捷出口：约 80% 的新用户会选「默认
  grantley」直接结束，避免被 7 个阶段的材料采集吓退。只有明确想自定义的
  操作员才会落入 Stage 0+。

---

## Stage 0 — Character Source（角色来源，W2 新增）

### 采集

**第一动作**：ask_user 二选一，决定整个流程走自动调研路径还是手动配置路径。

```
ask_user({
  "question": "你想配置的角色——是公众人物（网上有公开资料的真实/虚构人物，
              如鲁迅、Sherlock Holmes、张国荣），还是你自创的角色？",
  "options": ["公众人物（自动调研 + 蒸馏）", "自创角色（手动配置）"],
  "multiple": false
})
```

- 选「**自创角色**」→ **直接跳到 Stage 1**，按现有 6 阶段从头采集。Stage 0
  本身不需要审阅（用户的选择就是确认）。
- 选「**公众人物**」→ 继续 Stage 0a / 0b / 0c。

### Stage 0a — 收集名字

**只问一个 ask_user**：

「角色全名是什么？（中文 / 英文 / 别名均可，例：鲁迅、Sherlock Holmes、
苏轼、Iron Man）」

把名字存入 buffer，**立刻进入 Stage 0b**。

**重要**：公众分支默认走 auto-research，**不要在这里追问『有没有本地资料』**。
用户选「公众人物」就是想让 agent 自动调研，多问一步会打断体感（用户反馈
W2 上线后就这点被坑了）。需要喂本地资料的场景留给 Stage 0c 审阅的 `补充`
分支：如果用户对蒸馏结果不满意点 `补充`，那时再追问「想喂本地一手资料再调研
一轮吗？」。

### Stage 0b — 调研

**MUST** 走以下流程，不允许跳过：

- 如果用户在 0a 给了正文：先消化用户给的正文（必要时 `web_fetch` 补 URL）。
- 否则：**必须先 `web_search` 至少 1 次**（推荐查询：`<name> 思想 风格 名言`、
  `<name> wikipedia`、`<name> 著作`、`site:wikipedia.org <name>` 等 3-5 条），
  从结果里挑 top 2-3 个**权威来源**（维基百科 / 36氪 / 晚点 LatePost / 财新 /
  权威英文媒体），逐个 `web_fetch` 拉摘要。

**红线**（违反就是 bug）：

- **禁止凭训练语料编内容填 bucket**。哪怕你"觉得自己很懂这个人物"，也必须先
  search/fetch；模型对人物的 hallucination 在 persona skill 里会被放大成
  事实错误。
- 抓取失败、关键来源 404、搜索 0 结果 → ⚠️ **显式标注**在 0c 的 bucket
  里（例：`identity: ⚠️ 无可用来源，建议改自创`），并在 0c 审阅时让用户
  选 `重做` 或 `改自创`（你可以引导用户回 Stage 0 重新选「自创角色」）。
- 信息源黑名单：知乎、百度百科、微信公众号（沿用 `huashu-nuwa` 的黑名单）。

### Stage 0c — 蒸馏到 5 个 bucket（nuwa 框架精简版）

参照 `huashu-nuwa` skill 的提炼框架，但**只跑精简版**（不开 6 个并行 subagent；
在当前 agent loop 内顺序完成）。蒸馏成以下 5 个 bucket：

| Bucket | 内容 | 提炼来源 |
|--------|------|---------|
| **identity** | 一句话身份立场 + 时空 / 职业背景（≤ 40 字） | 时间线、著作高频主张 |
| **mental_models** | 2-3 个心智模型（看世界的镜片），每个一句话 + 1 句证据 | 反复出现 ≥ 2 次的核心论点 |
| **expression_dna** | 句式偏好 / 高频词 / 语气 / 1-2 句标志性表达 | 一手语料节选 |
| **anti_patterns** | 此人**不会**做或明确反对的：话题、立场、表达方式 | 公开批评、立场表态 |
| **honest_boundaries** | 这个 persona 不能预测/不知道的（信息截止、风格盲区） | 时间线截止 + nuwa 模板 |

### Stage 0 审阅闸门

```
ask_user({
  "question": <按 5 bucket 编号贴回内容，每 bucket 1-3 行> + "\n\n请审阅这份"
             "蒸馏草稿：",
  "options": ["确认", "补充", "修改", "重做"],
  "multiple": false
})
```

- **确认** → 把 5 bucket 作为**会话上下文 buffer** 带入 Stage 1-3（写在
  agent 的后续消息里，让自己看见）；进 Stage 1。
- **补充** → ask_user：「想补充哪个 bucket？」选项 = 5 个 bucket 名 + 「再调
  研一轮」+ 「**喂本地一手资料（粘贴文本或 URL）再调研**」。最后一项是
  W2 hotfix 后唯一进入「本地资料」路径的入口——用户不满意自动调研结果时，
  可以在这里追加 PDF 节选 / transcript / 设定集 URL，agent 再跑一轮 Stage 0b
  + 重蒸馏 Stage 0c。补充后回到 0c 审阅。
- **修改** → ask_user：「修改哪个 bucket？」拿到具体指引后改该 bucket 内容，
  回审阅。
- **重做** → 丢弃 Stage 0a-0c buffer，回 Stage 0 入口重新二选一（用户可以
  这时切到「自创角色」）。
