# Stage 1-3 —— 身份 / 文字材料 / 示例语料（configure-persona reference）
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
