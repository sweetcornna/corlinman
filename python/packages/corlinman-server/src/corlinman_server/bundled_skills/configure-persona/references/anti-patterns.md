# Anti-patterns（configure-persona reference）
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
