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
  - web_fetch
  - ask_user
---
# Configure Persona —— 分阶段材料收集向导

这个 skill 的核心契约：**materials-first（材料中心）+ 每阶段必须用户确认**。
它不是「列出已有 persona 的功能」，也不是「一次性表单」；它是一个 6 阶段的
材料采集流程，每个阶段都以一个**审阅 ask_user**作为闸门，用户没点「确认」就
不能进入下一阶段。

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
- `web_fetch` —— Stage 4 拉取用户粘贴的 URL 摘要。

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

## Stage 1 — Identity（身份）

### 采集

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

### 采集

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

### 采集

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
  "Background context:" 段落附上。控制总长 600-1500 字（中文按字符算）。
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
