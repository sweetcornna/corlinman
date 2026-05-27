---
name: configure-persona
description: 分阶段材料收集向导（每阶段必须用户确认才推进） — 用于 /persona、/角色、/人格、配置人格、配置角色 等别名触发的 persona 配置流。
metadata:
  openclaw:
    emoji: "🎭"
    requires:
      bins: []
      anyBins: []
      config: []
      env: []
    install: |
      No installation needed. The skill drives the in-process
      `persona.*` tool family + `ask_user` + `web_fetch`; no external
      services.
allowed-tools:
  - persona_list
  - persona_get
  - persona_create
  - persona_update
  - persona_delete
  - persona_list_assets
  - persona_attach_asset_from_url
  - web_search
  - web_fetch
  - ask_user
---
# Configure Persona —— 分阶段材料收集向导

这个 skill 的核心契约：**materials-first（材料中心）+ 每阶段必须用户确认**。
它不是「列出已有 persona 的功能」，也不是「一次性表单」；它是一个 **7 阶段**
（Stage 0-6）材料采集流程，每个阶段都以一个**审阅 ask_user**作为闸门，用户没
点「确认」就不能进入下一阶段。

Stage 0 是 W2 加入的新阶段，决定后续走「公众人物自动调研」还是「自创角色手动
配置」两条路径。自创角色保持现有 Stage 1-6 不变；公众人物会用 `web_search` +
`web_fetch` 调研后按 `huashu-nuwa` skill 的提炼框架蒸馏出 5 个 bucket，预填
Stage 1-3 的 buffer，**每阶段仍走四选项审阅闸门**。

## 何时启用

- 用户输入 `/persona`、`/角色`、`/人格`、`配置人格`、`配置角色`；
- 用户在自然语言里要求「创建 / 编辑 / 配置 persona / 角色 / 人格」；
- 系统注入了 `[SYSTEM-INSERTED] The user invoked the /persona command. ...`
  开头的 prelude；这就是 channel 路由把字面命令重写成本 skill 的调用契约。

## 工具表

- `ask_user` —— 唯一的人机交互通道。每个阶段至少 1 次。
- `persona_list` —— **仅在 Stage 1 的 `edit` 分支调用**。不要把它当开场动作。
- `persona_get` —— edit 分支取当前 persona 全文。
- `persona_create` —— **仅在 Stage 6 用户确认整体草稿后调用**。
- `persona_update` —— edit 分支 patch 字段。
- `persona_list_assets` / `persona_attach_asset_from_url` —— Stage 5 用。
- `web_search` —— **Stage 0b（公众分支）** 检索权威结果。
- `web_fetch` —— Stage 0b（公众分支）+ Stage 4 拉取用户粘贴的 URL 摘要。

## 通用审阅契约（每个 stage 结尾都这样收口）

```
ask_user({
  "question": "<本阶段已收集的材料贴回 + 编号清单>\n\n请审阅：",
  "options": ["确认", "补充", "修改", "重做"],
  "multiple": false
})
```

四个选项的语义（每阶段都一致）：

- **确认** → 把本阶段的 buffer 标记为 final，**进入下一阶段**。
- **补充** → 留在本阶段，继续追加（再问 1 轮 ask_user，把新条目并入 buffer，
  再次进入审阅）。
- **修改** → 用 ask_user 问「要修改哪几条？」，仅对被点名的条目重新 ask_user，
  其余保留；改完回到审阅。
- **重做** → 丢弃本阶段所有 buffer，从本阶段第 1 个采集问题重新开始。

任何阶段都不允许把多条问题合并成单个 ask_user；voice 访谈那一类多轮问询也是
一个问题一个 ask_user。

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

### Stage 0a — 收集名字 + 可选一手资料

按顺序 ask_user 两个问题（不要合并）：

1. 「角色全名是什么？（中文 / 英文 / 别名均可，例：鲁迅、Sherlock Holmes、
   苏轼、Iron Man）」
2. 「如果你手上有这个人/角色的一手资料（书籍正文、演讲 transcript、设定集
   URL、PDF），可以现在粘贴文本或贴 URL；没有就回『无』。一手资料的质量
   通常比网络搜索高得多。」

把名字 + 用户给的本地资料（如果有）存入 Stage 0a buffer。

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
  研一轮」。补充后回到 0c 审阅。
- **修改** → ask_user：「修改哪个 bucket？」拿到具体指引后改该 bucket 内容，
  回审阅。
- **重做** → 丢弃 Stage 0a-0c buffer，回 Stage 0 入口重新二选一（用户可以
  这时切到「自创角色」）。

---

## Stage 1 — Identity（身份）

### 公众分支预填行为（Stage 0 = 公众人物时）

当 Stage 0 走完公众分支并确认了 5 bucket：

- 从 Stage 0a 的角色名自动生成 `id`（slug 规则：小写 `[a-z0-9_-]`，中文转
  pinyin 或常用拉丁拼写，例：`鲁迅` → `lu-xun`、`Sherlock Holmes` →
  `sherlock-holmes`、`苏轼` → `su-shi`）。
- `display_name` 用 0a 的原名（保留中英文）。
- **直接进入 Stage 1 的审阅 ask_user**（不再问"id 是什么"/"display_name 是
  什么"两个采集问题）：
  ```
  ask_user({
    "question": "Stage 1 身份：\n1. id: <slug>\n2. display_name: <名>\n\n"
               "请审阅：",
    "options": ["确认", "补充", "修改", "重做"],
    ...
  })
  ```
- 用户 `修改` → ask_user 收新的 slug 或 display_name。其余流程不变。

### 自创分支采集（Stage 0 = 自创角色时）

第一动作必须是 ask_user，二选一：

> 想创建新的 persona 还是编辑已有的？

- options: `["创建新角色", "编辑已有"]`
- multiple: false

**如果用户选「编辑已有」**：
1. 调用 `persona_list`，把结果作为 ask_user 的下一个问题（选项 = 各 persona 的
   `id`，外加一条「取消」）。
2. 用户选定后 `persona_get(id)`，然后跳到 **Stage 1-Edit 流程**（见底部）。

**如果用户选「创建新角色」**，继续顺序采集：

1. ask_user：`id`（小写 slug，1-64 字符，仅 `[a-z0-9_-]`，例如 `grantley` 或
   `cyber_oracle`）。若校验失败（含非法字符、超长），把规则贴出来并 ask_user
   重新给一个，不要自作主张修正后继续。
2. ask_user：`display_name`（中英文均可，对外消息和 UI 都用这个）。

### 审阅

```
> 当前 Stage 1 材料：
> 1. id: <slug>
> 2. display_name: <name>
>
> 请审阅：
options: ["确认", "补充", "修改", "重做"]
```

注意：本阶段「补充」语义为「再补 1 条 alias 之类的可选字段」其实没有可补的
——所以如果用户点「补充」，礼貌说明 Stage 1 只有这两项，把他/她引到「修改」
或直接「确认」。

---

## Stage 2 — 文字材料（身份/语气/口头禅/禁忌话题/示例对话）

### 公众分支预填行为（Stage 0 = 公众人物时）

把 Stage 0c 的 buffer 映射到本阶段的 5 个轴向：

- 轴向 1（身份立场）← `identity` bucket
- 轴向 2（语气）← `expression_dna` 的「语气 / 句式偏好」部分
- 轴向 3（口头禅）← `expression_dna` 的「标志性表达 / 高频词」
- 轴向 4（禁忌）← `anti_patterns` bucket
- 轴向 5（长度偏好）← 从 `expression_dna` 推断（如未涉及，默认「中等」）

**跳过 5 轮采集 ask_user**，直接进入审阅；用户在审阅时可以用「修改」改任意
一条。`mental_models` bucket 不直接对应轴向，但要在审阅时贴在末尾作为「心智
模型（将注入 system_prompt）」一节，让用户能审阅。

### 自创分支采集（Stage 0 = 自创角色时）

每轮一个 ask_user，建议覆盖以下 5 个轴向（可根据上下文删减，但保持单问单答）：

1. 一句话定义这个角色的身份与立场？
2. 语气是怎样的？(温柔 / 毒舌 / 严肃 / 俏皮 / 学术 / ……)
3. 常用口头禅或标志性表达？
4. 应该规避的话题、表达、立场？
5. 回应长度偏好？(简短 / 中等 / 长篇)

把回答按 `{axis, value}` 收进本阶段 buffer。

### 审阅

```
> 当前 Stage 2 文字材料：
> 1. 身份立场：……
> 2. 语气：……
> 3. 口头禅：……
> 4. 禁忌：……
> 5. 长度偏好：……
>
> 请审阅：
options: ["确认", "补充", "修改", "重做"]
```

- `补充` → ask_user：「想补充哪类材料？」给出剩余轴向作为选项；新答案并入 buffer。
- `修改` → ask_user：「修改第几条？」（multi-select 允许选多条），逐条重问。

---

## Stage 3 — 示例语料（few-shot dialogue samples）

### 公众分支预填行为（Stage 0 = 公众人物时）

用 Stage 0c 的 `expression_dna` bucket（特别是「标志性表达」+「高频词」）+ 调研
拉到的一手语料节选，自动生成 3-5 条 few-shot 样本：

- 格式：`「场景：<X>」角色：「<对应风格的应答>」`
- 至少 1 条要直接复用一手语料里的真实引用（标注来源 URL）；其余可基于
  expression_dna 重写

把生成的样本编号存入 buffer，**跳过采集 ask_user**，直接进入审阅。用户在审阅
时可以用「修改」改某条或用「补充」追加自己的样本。

### 自创分支采集（Stage 0 = 自创角色时）

ask_user：

> 请贴 3-8 条「角色会这样说」的对话样本（每行一条，或直接多条粘贴）。这些会
> 作为 few-shot 示例注入 system_prompt 帮助锁定语气。

把贴回来的文本按行 split，过滤空行，编号存入 buffer。如果用户给得太少（<2 条），
ask_user 一次追问「再贴几条？」；如果空着回，温和警告但允许「确认」跳过本阶段。

### 审阅

```
> 当前 Stage 3 示例语料（共 N 条）：
> 1. ……
> 2. ……
> …
>
> 请审阅：
options: ["确认", "补充", "修改", "重做"]
```

`补充` 直接再 ask_user 一次「请继续贴」。

---

## Stage 4 — 外链 / 文件（参考资料 URL）

### 采集

ask_user：

> 有相关的外部资料吗？例如：
> - 粉丝向资料、人设集 URL
> - 维基/百科条目
> - 公开的设定文档链接
>
> 每行 1 个 URL（http/https），或回「无」跳过。

对每个 URL：
1. 用 `web_fetch(url)` 拉取（失败就把错误信息收进 buffer 备注，**不要静默忽略**）。
2. 截取前 ~800 字的可读摘要，存入 buffer 作为 `{url, summary}`。

如果 web_fetch 失败：在审阅里如实标注「⚠️ 抓取失败：<reason>」，让用户决定要不要在
Stage 2 的语气/禁忌里手动补对应内容。

### 审阅

```
> 当前 Stage 4 外部材料：
> 1. <url> — 摘要：……
> 2. <url> — ⚠️ 抓取失败：……
>
> 请审阅：
options: ["确认", "补充", "修改", "重做"]
```

---

## Stage 5 — 图片（emoji + reference）

### 采集

ask_user 三选一：

> 角色形象图：
> 1. 用 /admin/persona 拖拽上传（推荐，支持 PNG/JPEG/WEBP/GIF，单图 ≤ 8 MiB，
>    总量 ≤ 200 MiB）
> 2. 粘贴图片 URL 让我帮你拉取
> 3. 跳过

options: `["拖拽上传(网页)", "粘贴URL", "跳过"]`

**选「拖拽上传(网页)」**：告诉用户去 `/admin/persona/<persona_id>` 拖拽——
**但 persona 此时还没落库**，所以告知「我会在最后一阶段创建 persona，等创建完
你再去网页上传；现在记下你的选择」。把 buffer 记为 `{mode: "web_upload",
items: []}`。

**选「粘贴URL」**：循环 ask_user：

> 请给一张图：`<label> <url>`（label 是这张图的标签，比如 `happy` / `front` /
> `side`；只能含 [a-z0-9_-]）。完成请回「下一阶段」。

对每个 `{label, url}`：
- **暂时不要调用 `persona_attach_asset_from_url`**（persona 还没创建）。
- 把 `{label, url}` 存入 buffer。

**选「跳过」**：buffer 为空，直接审阅。

### 审阅

```
> 当前 Stage 5 图片材料：
> 模式：<web_upload / paste_url / skip>
> 已登记：
>   1. happy → https://…
>   2. front → https://…
>
> 请审阅：
options: ["确认", "补充", "修改", "重做"]
```

---

## Stage 6 — 合成 + 落库

### 采集（agent 自己起草）

根据 Stage 1-4 的全部 buffer，**起草** `system_prompt` 和 `short_summary`：

- `system_prompt`：把身份、语气、口头禅、禁忌、长度偏好揉成一段角色扮演指令；
  把 Stage 3 的示例语料以 "Examples:" 段落附后；如有 Stage 4 摘要，作为
  "Background context:" 段落附上。
- **公众分支额外**：把 Stage 0c 的 `honest_boundaries` bucket 作为
  "Limitations:" 段附在 system_prompt 末尾（例：「我对 2024 年后的事件不熟悉；
  我不能预测我没说过的话；……」）。这是 nuwa 框架的硬约束，能显著降低 persona
  在不知道领域瞎编的概率。如果 Stage 0c 还提供了 `mental_models`，作为
  "Mental Models:" 段附在 Examples 之前。
- 控制总长 600-1500 字（中文按字符算）。
- `short_summary`：≤ 120 字的一句话总结。

### 审阅

```
> 即将创建 persona：
> - id: <slug>
> - display_name: <name>
> - short_summary: <oneliner>
> - system_prompt:
> <draft full text>
>
> 请审阅：
options: ["确认创建", "修改 prompt", "修改 summary", "重做"]
```

注意：本阶段 options **替换为上面的 4 个**（因为已经到终局，"补充" 没意义，
"修改" 拆成两个具体方向）。

- `确认创建` → 调用 `persona_create({id, display_name, short_summary,
  system_prompt})`。捕获错误：
  - slug collision → 在 plain text 里说明，并 ask_user：「换一个 id？还是覆盖
    已有？」覆盖走 `persona_update`，不覆盖回 Stage 1 的 id 重问。
  - validation error → 把 server 返回的 message 贴回来，回到对应 stage 重做。
- `修改 prompt` → ask_user：「想怎么改？」拿到指引后**重新起草**，再次进入本
  阶段审阅。
- `修改 summary` → 同上，只改 short_summary。
- `重做` → 整个 wizard 从 Stage 1 重新开始（确认前再 ask_user 警告一次）。

### 落库后

`persona_create` 成功后：

1. **如果 Stage 5 有 `paste_url` 模式的图片 buffer**：现在循环调
   `persona_attach_asset_from_url(persona_id, kind="emoji" or "reference",
   label, url)`，每张图把结果（成功 / 失败 + 原因）回给用户。
   - emoji vs reference 的归类：label 是常见情绪词（happy/sad/angry/smile/cry
     等）→ emoji；其他 → reference。如果含糊，ask_user 确认。
2. **如果是 `web_upload` 模式**：明确告诉用户去
   `/admin/persona/<persona_id>` 拖拽。
3. 汇总 2-3 行总结：`id`、display_name、登记图片数、是否需要后续上传，附上
   `/admin/persona/<persona_id>` 链接。

---

## Stage 1-Edit 分支（编辑已有 persona）

`persona_get(id)` 返回当前行后：

1. ask_user：「想修改哪几个字段？」options:
   `["display_name", "short_summary", "system_prompt", "图片(增删)", "完成"]`
   multiple: true。
2. 对每个被选中的字段：
   - 显示当前值（system_prompt 过长就先摘要前 200 字 + 「(略)」）；
   - ask_user 收新值；
   - 进入「字段级审阅」：
     ```
     options: ["确认更新", "再改一次", "保留原值"]
     ```
3. 用户选完所有字段后，一次性 `persona_update(id, **patches)`。
4. 图片编辑：列出现有 assets（`persona_list_assets`），ask_user 选择
   「新增 / 删除 / 完成」，调相应工具。

---

## Anti-patterns（违反这些会导致 wizard 退化为 list）

- ❌ **首动作调 `persona_list`**（除非用户已经主动选 `edit`）。这是当前版本要
  修复的 bug，最常见的失败模式。
- ❌ **把多个阶段合并成一个 ask_user**。例如「一次问 id、display_name、tone
  三个字段」——这破坏了审阅闸门契约。
- ❌ **跳过审阅 ask_user 直接进入下一阶段**。哪怕本阶段只采集到一条材料也要
  审阅。
- ❌ **在 Stage 6 用户确认前调 `persona_create`**。persona 一旦落库回滚就要
  `persona_delete` round-trip，弱化了"确认"语义。
- ❌ **静默忽略 web_fetch 失败**。要明确告诉用户哪条 URL 抓失败、为什么。
- ❌ **审阅选项里随意改名或增删**。`["确认","补充","修改","重做"]` 是 Stage
  1-5 的固定四选；Stage 6 例外（替换为四个面向落库的具体动作）。
- ❌ **自动从对话历史推断答案**。ask_user 的契约是"显式问、显式答"。
- ❌ **scraping 任意网页给角色配图**。Stage 5 的图片来源必须是用户明确给的
  URL 或他/她自己上传。
- ❌ **公众分支：凭训练语料填 Stage 0c 的 5 个 bucket**。即使你"觉得自己很懂
  这个人物"，也必须先 `web_search` + `web_fetch` 拿真实来源。模型对人物的
  hallucination 在 persona skill 里会被放大成事实错误。
- ❌ **公众分支：调研失败时静默回退到训练语料**。`web_search` 0 结果或所有
  `web_fetch` 失败 → 在 Stage 0c bucket 里 ⚠️ 显式标注，让用户选 `重做` 或
  `改自创`（引导用户回 Stage 0 重选）。
- ❌ **让 Stage 0c 的蒸馏结果跳过 Stage 1-3 的审阅 ask_user**。预填只能省去
  采集 ask_user，**审阅闸门一个都不能漏**。
- ❌ **公众分支：为敏感政治人物 / 在世名人 / 负面历史人物自动生成
  `system_prompt`**。触到敏感题材 → ⚠️ 停下来 ask_user：「这是敏感题材，
  你确定要继续吗？建议改自创或换个角色。」让用户拍板。
