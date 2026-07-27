---
name: configure-persona
description: 分阶段材料收集向导（每阶段必须用户确认才推进） — 用于 /persona、/角色、/人格、配置人格、配置角色 等别名触发的 persona 配置流。
when_to_use: 用户输入 /persona、/角色、/人格、配置人格、配置角色，或在自然语言里要求「创建 / 编辑 / 配置 persona / 角色 / 人格」时使用。
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
      `persona_*` tool family + `ask_user` + `web_fetch`; no external
      services.
allowed-tools:
  - persona_list
  - persona_get
  - persona_create
  - persona_update
  - persona_delete
  - persona_list_assets
  - persona_attach_asset_from_url
  - persona_life_set_seeds
  - persona_life_get_seeds
  - web_search
  - web_fetch
  - ask_user
---
# Configure Persona —— 分阶段材料收集向导

这个 skill 的核心契约：**materials-first（材料中心）+ 每阶段必须用户确认**。
它不是「列出已有 persona 的功能」，也不是「一次性表单」；它是一个 **8 阶段**
（Stage -1, 0, 1-6）材料采集流程，每个采集阶段都以一个**审阅 ask_user**作为
闸门，用户没点「确认」就不能进入下一阶段。

Stage -1 是 first-run wizard 加入的入口闸门，三选一询问「使用默认 grantley /
自定义人格 / 跳过」；选「默认」或「跳过」就**直接结束**，根本不进入 Stage 0。
只有选「自定义人格」才会落入 Stage 0+ 的完整向导。

Stage 0 是 W2 加入的分支节点，决定后续走「公众人物自动调研」还是「自创角色
手动配置」两条路径。自创角色保持现有 Stage 1-6 不变；公众人物会用
`web_search` + `web_fetch` 调研后按 `huashu-nuwa` skill 的提炼框架蒸馏出 5 个
bucket，预填 Stage 1-3 的 buffer，**每阶段仍走四选项审阅闸门**。

## 何时启用

- 用户输入 `/persona`、`/角色`、`/人格`、`配置人格`、`配置角色`；
- 用户在自然语言里要求「创建 / 编辑 / 配置 persona / 角色 / 人格」；
- 系统注入了 `[SYSTEM-INSERTED] The user invoked the /persona command. ...`
  开头的 prelude；这就是 channel 路由把字面命令重写成本 skill 的调用契约。

## 如何读取 references/

本 skill 的详细内容按主题拆在 `references/*.md`（与本文件同目录）。在 corlinman 运行时中，用 `Skill` 工具带 `file` 参数读取，例如：`Skill(name="configure-persona", file="references/stage-0-entry.md")`。其他 runtime 中，直接按相对本 skill 根目录的路径读取对应文件即可。**开工前必须先读与任务匹配的 reference，不要只凭本文件行事。**

## 工具表

- `ask_user` —— 唯一的人机交互通道。每个阶段至少 1 次。
- `persona_list` —— **仅在 Stage 1 的 `edit` 分支调用**。不要把它当开场动作。
- `persona_get` —— edit 分支取当前 persona 全文。
- `persona_create` —— **仅在 Stage 6 用户确认整体草稿后调用**。
- `persona_update` —— edit 分支 patch 字段。
- `persona_list_assets` / `persona_attach_asset_from_url` —— Stage 5 用。
- `persona_life_set_seeds` —— **Stage 6 落库后**写入 Stage 4b 收集的事件种子库
  （取显式 `persona_id`）。`persona_life_get_seeds` —— edit 分支查现有生活设定。
- `web_search` —— **Stage 0b（公众分支）+ Stage 4b 自动分支** 检索权威结果。
- `web_fetch` —— Stage 0b / Stage 4b 自动分支 + Stage 4 拉取用户粘贴的 URL 摘要。

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

## 阶段总览（每个阶段的完整剧本在对应 reference 里）

| 阶段 | 内容 | 读 |
|------|------|-----|
| Stage -1 | first-run 入口闸门：默认 grantley / 自定义人格 / 跳过（选「默认」或「跳过」直接结束） | `references/stage-0-entry.md` |
| Stage 0 / 0a / 0b / 0c | 公众人物 vs 自创角色分流；公众分支 web_search+web_fetch 调研并蒸馏 5 bucket | `references/stage-0-entry.md` |
| Stage 1-3 | 身份（id/display_name）、文字材料 5 轴向、few-shot 示例语料（含公众分支预填规则） | `references/stages-1-3.md` |
| Stage 4 / 4b / 5 | 外链资料抓取、生活设定事件种子库、形象图片三选一 | `references/stages-4-5.md` |
| Stage 6 + Edit 分支 | 起草 system_prompt/short_summary、落库、落库后写种子/挂图；编辑已有 persona | `references/stage-6-and-edit.md` |
| Anti-patterns | 违反即 bug 的完整清单（首动作禁 persona_list、禁合并提问、禁凭语料编 bucket 等） | `references/anti-patterns.md` |

**执行纪律**：进入某个阶段前，先用上表读取该阶段的 reference 全文再动作；任何阶段不许跳过审阅闸门。
