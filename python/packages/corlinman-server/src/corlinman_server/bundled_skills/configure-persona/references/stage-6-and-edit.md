# Stage 6 与编辑分支（configure-persona reference）
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

1. **如果 Stage 4b 有种子桶 buffer**（用户没跳过生活设定）：现在调
   `persona_life_set_seeds({persona_id, seeds})` 把种子桶写进库（`seeds` 是
   `{类目: [短词,...]}`）。把返回的 `categories`（每类条数）回给用户确认；失败
   就贴回 error message。这一步让 persona 的「生活」真正生效。
2. **如果 Stage 5 有 `paste_url` 模式的图片 buffer**：现在循环调
   `persona_attach_asset_from_url(persona_id, kind="emoji" or "reference",
   label, url)`，每张图把结果（成功 / 失败 + 原因）回给用户。
   - emoji vs reference 的归类：label 是常见情绪词（happy/sad/angry/smile/cry
     等）→ emoji；其他 → reference。如果含糊，ask_user 确认。
3. **如果是 `web_upload` 模式**：明确告诉用户去
   `/admin/persona/<persona_id>` 拖拽。
4. 汇总 2-3 行总结：`id`、display_name、生活设定类目数、登记图片数、是否需要后续
   上传，附上 `/admin/persona/<persona_id>` 链接。

---

## Stage 1-Edit 分支（编辑已有 persona）

`persona_get(id)` 返回当前行后：

1. ask_user：「想修改哪几个字段？」options:
   `["display_name", "short_summary", "system_prompt", "图片(增删)", "生活设定(事件种子)", "完成"]`
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
5. **生活设定编辑**（若用户勾选）：先 `persona_life_get_seeds(persona_id)` 拿
   现有种子库（`has_override=false` 说明还没配过），按类目贴回给用户审阅；走
   Stage 4b 的「自动调研 / 我来提供资料」二选一收新内容，确认后
   `persona_life_set_seeds(persona_id, seeds, merge=true/false)`（整体替换用
   `merge=false`，只追加某些类目用 `merge=true`）。
