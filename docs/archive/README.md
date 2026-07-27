# docs/archive — 已完成计划的归档

`PLAN_*.md` / `PROMPT_*.md` 是**时点性工作产物**(某次任务的计划书或 agent
提示词)。计划完成或被取代后移入本目录,原文不再更新——查现状以正文档
(`docs/*.md`)和代码为准。归档不重写内文,文内的旧相对链接按历史快照原样保留。

## 归档清单(2026-07-27,逐一对照现树判定)

| 文档 | 归档依据 |
|---|---|
| PLAN_AGENT_CAPABILITY_FUSION | 自述 done + VPS 验证 |
| PLAN_AGENT_STATUS_CARD | v1.13.0 发布 |
| PLAN_AUTO_UPDATE / PLAN_ONE_CLICK_UPGRADE | 升级器已修复启用,`system/upgrader/` 在树 |
| PLAN_CHANNELS_COMPLETION | 渠道补全已发布(7 渠道全量) |
| PLAN_CLAUDECODE_PARITY | waves 1-4 已合入 v1.27.0,文档表格已过时 |
| PLAN_CLI_CONSOLE | `cli/console.py` 在树 |
| PLAN_decompose_cores | 自述 v1.17.0 EXECUTED |
| PLAN_DYNAMIC_SUBAGENTS | `system/subagent/` 在树 |
| PLAN_FIRST_RUN_WIZARD | `ui/app/onboard` 在树 |
| PLAN_HOOKS_DECLARATIVE | #109 合入 |
| PLAN_IN_APP_CHAT | /chat 已上线 |
| PLAN_MARKETPLACE | v1.16.0,`system/marketplace/` 在树 |
| PLAN_MCP_SAMPLING_LISTCHANGED | corlinman-mcp-server 有 sampling + list_changed 实现与测试 |
| PLAN_MULTI_AGENT | 被 dynamic subagents 取代且已发布 |
| PLAN_PERSONA_LIFE_QZONE_MIGRATION | corlinman-persona + #148–#158 合入 |
| PLAN_PERSONA_STUDIO | `studio/personas.py` 在树 |
| PLAN_PORT_COMPLETION | 自述 shipped ap1.0.0 |
| PLAN_SKILL_HUB | `skill_hub/` 在树且被 marketplace 取代 |
| PLAN_UI_FIXES | 被 Eclipse 重设计(#145)+ UIX 波次取代 |
| PROMPT_ZERO_BUG_PARITY | 2026-07-02 零缺陷清扫完成 |
| PROMPT_CLI_CLAUDECODE_CLASS | CLI console 已发布 |

## 仍留在 docs/ 根目录的(保留理由)

| 文档 | 保留理由 |
|---|---|
| PLAN_EASY_SETUP | 大量代码 docstring 以其为规格引用;后续波(hermes 自演化)完成态未逐条复核 |
| PLAN_PROVIDER_AUTH | 同上——oauth/providers 路由仍按其波次编号引用,作为活契约保留 |
| PLAN_HARDWARE_INTEGRATION | 规划未启动(M1–M6 全在未来) |
| PLAN_AGENT_PARITY_IMPL / PLAN_TASK_CONTINUITY | 自述 not started;是否被 claude-code parity 波次完全覆盖不确定,保守保留 |
| PLAN_CHAT_PERFECT | 多数波已发布;逐波完成态未全部复核,保守保留 |
| PLAN_DEPLOY_UX / PLAN_TASK_OBSERVABILITY | 完成度不确定,保守保留 |

判定新完成的计划时:对照现树验证(代码/发布号),`git mv` 进本目录并在上表补一行。
