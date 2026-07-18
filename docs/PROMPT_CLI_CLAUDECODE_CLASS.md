# 提示词 — corlinman CLI 开发:对标 claude-code CLI 源码与最新版特性

> 用法:在 corlinman 仓库根目录交给 Claude Code 执行:
> `claude "$(cat docs/PROMPT_CLI_CLAUDECODE_CLASS.md)"`。
> 前置:建议先完成 `docs/PROMPT_ZERO_BUG_PARITY.md` 阶段一(CI 全绿),否则 CLI 开发会踩在坏地基上。

---

你是 corlinman CLI(`corlinman console`)的主开发工程师。目标:把 console 从当前地基
(`cli/console.py` 仅 ~185 行,PR #88 骨架)开发为 **claude-code 级的交互式 CLI agent 控制台**,
参考 claude-code 源码机制与其最新版特性,但**只吸收机制、重新实现,不复制代码**(注意许可证)。
方法论沿用仓库惯例:逐项实测,不信任"文件/计划存在即完成"。

## 全局约束

- 遵守 `CONTRIBUTING.md`、`.importlinter` 分层;每个 wave 结束 `make ci` 全绿 + CHANGELOG(双语)+ 版本号递增。
- 架构必须遵循 `docs/PLAN_CLI_CONSOLE.md` 既定设计:Mode A embedded(私有 UDS 内嵌 `CorlinmanAgentServicer`,全脑:工具/subagent/记忆)+ Mode B attach(SSE 连运行中 gateway);渲染层 rich + prompt_toolkit 双队列输入。
- **用户既有指令不变**:slash 命令必须跨渠道可用(console / web / QQ / Telegram),命令注册表做成渠道无关的单一来源(见 `docs/PLAN_CLAUDECODE_PARITY.md` "Cross-channel commands design")。
- 禁止:为演示效果 mock agent 事件流;console 专属逻辑下沉进 agent 核心包破坏分层。

## 阶段 0 — 现状复核(先测再写)

1. 实测 `corlinman console` 现有每一条路径:embedded 启动、attach、`-p` print mode、每个已实现 slash 命令,记录"可用/半成/坏"。
2. 复核 `docs/PLAN_CLI_CONSOLE.md` §0 存量表与 `docs/PLAN_CLAUDECODE_PARITY.md` wave 表的真实状态(partial 项逐个验证到代码行)。
3. 产出 `audit/CLI_BASELINE_<日期>.md`:现状 + 与本提示词目标清单的逐项差距。

## 阶段 1 — 取源与特性差量更新

1. **源码基线**:以 claude-code 2.1.88 restored source(`docs/parity-matrix-2026-06-11.json` 的 19 cluster 扫描)为机制参考。
2. **最新特性差量**:抓取 claude-code 官方 changelog(code.claude.com/docs/en/changelog 与 GitHub releases),覆盖 2.1.88 → 最新(2026-07 已至 2.1.198)。已知需纳入评估的新特性(以实抓 changelog 为准,不止于此):
   - 嵌套 subagent(≤3 层任务分解)与 subagent 后台执行、完成后自动 commit/push/开 draft PR 的 background agent 语义;
   - 模型 fallback 链(主模型失败按链降级)与 per-agent 成本归因(/cost 按 agent 拆分);
   - scoped permissions(更细粒度权限作用域);
   - Notification hook(需要输入/任务完成时触发通知);
   - ToolSearch 工具延迟加载(大工具面按需取 schema);
   - Explore 类内置只读搜索 agent 继承主会话模型;
   - 会话工作成果转可分享页面(对应 corlinman 已有 status-card 公开链路,评估融合而非新造)。
3. 更新差距矩阵为 `audit/CLI_PARITY_MATRIX_<日期>.md`:每项 = claude-code 机制摘要(引其源码/文档位置)→ corlinman 现状 → 采纳决策 → 落点(console 层 / 核心包 / 渠道层)。

## 阶段 2 — 分 wave 落地(顺序可按实测调整,单项完成即验收)

**Wave 1 — 可脚本化与会话基本面**
1. Print mode 补全:`-p --output-format text|json|stream-json`、`--max-turns`、退出码语义;stream-json 事件 schema 写进 `docs/contracts/`。
2. 会话:`--continue`、`--resume`(带选择器)、`--fork-session`、历史文件与保留策略;`/rewind` 消息级 checkpoint + 文件快照回滚。
3. 项目记忆:`CORLINMAN.md`(CLAUDE.md 类比)——发现规则(向上遍历+全局)、`@include`、`/init` 生成、`/memory` 编辑。
4. 上下文压缩:`/compact`、阈值自动触发、压缩事件上报、断路器(压缩失败不得死循环)。

**Wave 2 — 权限与计划**
5. 权限模式:default / acceptEdits / plan / bypass + console 内交互式审批 UI(接现有 approval gate,不另做一套);`/permissions` 查看修改。
6. 权限规则引擎:`Bash(cmd:*)` 类文法、allow/deny、多源优先级(cli flag > 项目 settings > 用户 settings)、settings 持久化。
7. Plan mode:EnterPlanMode/ExitPlanMode 工具语义、只读工具门控、计划审批后放行、plan 阶段模型覆盖。

**Wave 3 — 工具面与 MCP**
8. 核心工具语义对齐:Read offset/limit、Bash `run_in_background`、Grep 三种 output_mode、原子 Write、编辑前必读校验。
9. MCP client:`.mcp.json` 三作用域(项目/用户/本地)、工具命名空间合并、`/mcp` 管理、ToolSearch 式延迟加载。
10. Hooks:settings 驱动的生命周期 hook(PreToolUse/PostToolUse/Notification/Stop 等)+ `/hooks` 管理,复用 `corlinman-hooks` 包。

**Wave 4 — 多 agent 与成本**
11. Subagent 后台执行:async 任务、TaskStop、输出溢出落盘、完成通知(接 Notification hook + 渠道推送);嵌套深度≤3(接现有 `Supervisor` caps,配置化)。
12. 模型路由:简单/工具型轮次路由到 small_fast 模型(`router.py`)、fallback 链、`/model` 热切换。
13. `/usage` `/cost`:token/成本核算,按 session 与按 subagent 归因。

**Wave 5 — UX 长尾**
14. Todo 实时清单渲染(activeForm spinner)、主题(日/夜,对齐 Tidepool)、statusLine 可配置、vim 键位、`/doctor` `/config`。

每个 wave 验收:功能演示脚本(录 asciinema 或文字 transcript 存 `audit/cli-demos/`)+ 单测 + `make ci` 全绿 + 跨渠道命令表同步更新。

## 最终验收(场景清单,全部真实跑通)

1. `corlinman console -p "总结这个仓库" --output-format stream-json --max-turns 3` 输出合法事件流并正常退出;
2. 交互会话中:`/init` 生成 CORLINMAN.md → 长对话触发自动 compact → `/rewind` 回滚一次文件编辑;
3. plan mode 下工具被只读门控,批准计划后执行,期间一次 Bash 危险命令被规则 `Bash(rm:*)` 拒绝;
4. `.mcp.json` 挂一个真实 MCP server,其工具在 console 与 QQ/Telegram 渠道均可被 agent 调用;
5. spawn 一个后台 subagent,console 收到完成通知,`/cost` 能看到该 subagent 的独立成本;
6. 同一条 `/model` `/sessions` `/resume` 在 console 与至少一个聊天渠道行为一致;
7. `make ci` 全绿,`corlinman doctor` 全绿。

## 交付清单

- [ ] `audit/CLI_BASELINE_<日期>.md`、`audit/CLI_PARITY_MATRIX_<日期>.md`
- [ ] 分 wave 的 commit 序列(每 wave 可独立回滚)+ CHANGELOG + 版本号
- [ ] `docs/PLAN_CLI_CONSOLE.md` 与 `docs/PLAN_CLAUDECODE_PARITY.md` 状态刷新
- [ ] stream-json 事件契约文档 + 跨渠道命令矩阵文档
- [ ] `audit/cli-demos/` 场景演示记录
- [ ] ≤40 行执行总结:落地项 / 决策不采纳项及理由 / 遗留项
