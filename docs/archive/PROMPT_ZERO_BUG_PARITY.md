# 提示词 — 全量修 bug 至零缺陷 + hermes-agent / claude-code 源码对标吸收

> 用法:将本文件全文作为提示词交给 Claude Code(在 corlinman 仓库根目录执行)。
> 建议命令:`claude "$(cat docs/PROMPT_ZERO_BUG_PARITY.md)"` 或在会话中 `@docs/PROMPT_ZERO_BUG_PARITY.md 按此执行`。

---

你是 corlinman 仓库的首席修复与架构工程师。本次任务分两个阶段,**阶段一不完成不得进入阶段二**。全程遵循本仓库既有方法论:**逐项实测代码,不信任"文件存在即完成"**(见 `docs/GAP_ANALYSIS_HERMES_OPENCLAW.md` 的审计方法)。

## 全局约束

- 遵守 `CONTRIBUTING.md`、`.importlinter` 分层约束、现有代码风格。
- 每一批修复必须通过完整本地 CI:`make ci`(等价于 uv sync → ruff → mypy → pytest(非 live)→ pnpm typecheck/lint/vitest → gen-proto diff 校验 → lint-imports)。
- 禁止:为通过测试而删除/跳过测试;用 Mock 冒充真实实现来"消灭"bug;修改 CI 配置放宽门槛;把 `NotImplementedError` 换成静默空实现。
- 每完成一批,更新 `CHANGELOG.md`(沿用现有双语条目格式)并递增版本号。
- 提交粒度:一个根因一个 commit,commit message 说明复现方式与根因。
- 可以使用 subagent 并行审计,但**合并与判定只在主线做**。

---

## 阶段一:全量 bug 清零,项目完全可用

### 1.1 基线盘点(先测,再改)

依次执行并完整记录输出(不要截断、不要只看退出码):

1. `make ci` — 记录每一个失败/警告。
2. `uv run corlinman doctor` — 记录每一项非绿检查。
3. 全仓静态扫描,建立"可疑点台账":
   - `grep -rn "NotImplementedError\|TODO\|FIXME\|XXX\|HACK" python/packages/ --include="*.py"`
   - `grep -rn "Mock\|placeholder\|stub" python/packages/ --include="*.py"`(区分测试文件与生产代码,只登记生产代码)
   - ui 侧:`grep -rn "TODO\|FIXME\|@ts-ignore\|@ts-expect-error\|any as" ui/app ui/components ui/lib`
4. 运行时冒烟(真实启动,不是单测):
   - 启动 server,走一轮完整对话(OpenAI 兼容端点),触发至少一次真实工具调用;
   - 打开 admin UI 核心页面(插件、RAG、审批队列、日志流、模型路由),确认无 500/白屏/控制台报错;
   - 渠道 dry-run(QQ/Telegram 适配器至少走到出站前的最后一步);
   - scheduler 触发一个 cron 任务;
   - 配置热重载路径(`ConfigWatcher`)是否真实生效。
5. 复核既有已知问题清单,逐项确认当前真实状态(可能已修/半修/未修):
   - `docs/TODO_FOLLOWUPS.md` 全部未勾选项;
   - `docs/GAP_ANALYSIS_HERMES_OPENCLAW.md` 中标注的实测缺陷:MCP 未接入 agent 工具面、`ConfigWatcher` 启动期未接线、voice 仅 `MockVoiceProvider`、`EvolutionApplier` 仅记账不生效、`metrics_baseline={}` 导致 auto-rollback fail-safe 跳过、`service`/`mcp` 插件类型未支持、Bedrock/Azure `NotImplementedError` 占位;
   - `ui/test-results/` 与 `ui/tests/` 中的既有失败用例。

### 1.2 建立 bug 台账并分级

产出 `audit/BUG_LEDGER_<日期>.md`(沿用仓库根目录 `audit/` 惯例),每条记录:ID、复现步骤、根因、影响面、级别、修复 commit、回归测试。分级标准:

- **P0** 崩溃、数据丢失、启动失败、安全漏洞(含凭据泄露、无鉴权路由误暴露);
- **P1** 核心功能不可用或结果错误(agent 循环、工具执行、记忆读写、渠道收发、审批门);
- **P2** 功能降级、误报误导(如历史上 Codex "HTTP 400" 误报一类)、文档与行为不符;
- **P3** 体验问题、日志噪音、样式缺陷。

### 1.3 修复循环(逐条执行,直至台账清零)

对台账中每一条,严格走:**复现 → 定位根因 → 最小修复 → 新增回归测试(必须先红后绿)→ `make ci` 全绿 → 台账销项**。规则:

- 修不动的先写失败测试并标记 `xfail` + 台账保留,不许静默放弃;
- 同根因的多个表象合并成一条,修根因;
- 涉及 25 个 python 包(`python/packages/corlinman-*`)之间的改动,先跑 `uv run lint-imports` 确认不破坏分层;
- ui 改动必须过 `pnpm -C ui typecheck && pnpm -C ui lint`,涉及页面行为的补 Playwright 用例。

### 1.4 阶段一验收标准(全部满足才算完成)

1. `make ci` 全绿,零警告级失败;
2. `uv run corlinman doctor` 全部检查通过;
3. 1.1-4 的运行时冒烟全流程无错误;
4. bug 台账中 P0/P1 为零,P2 为零或每条有明确豁免理由并经台账记录,P3 允许遗留但需登记;
5. 生产代码中 `NotImplementedError` 只允许出现在**文档中明确声明为未来功能**的位置,并有对应的用户可见错误提示(不是崩溃);
6. `docs/TODO_FOLLOWUPS.md` 状态全部刷新为当前真实状态。

---

## 阶段二:对标 hermes-agent 与 claude-code,吸收优点并落地

### 2.1 取源与复核

1. 拉取 `NousResearch/hermes-agent` 最新源码到本地临时目录(只读参考,不引入其代码依赖);
2. claude-code 以 `docs/parity-matrix-2026-06-11.json`(claude-code 2.1.88 restored source 的 19 个 cluster 扫描)为基线,若本地有更新版本的 restored source 则重扫;
3. **先复核存量结论再增量**:`docs/GAP_ANALYSIS_HERMES_OPENCLAW.md` 与 `docs/PLAN_CLAUDECODE_PARITY.md` 的 wave 表逐项实测当前状态,标注"已落地/半成/未动",不重复造已完成的轮子。

### 2.2 对比维度(逐项读两边源码,写实证结论,不许凭印象)

对每一项产出:两边的实现机制摘要 → corlinman 现状 → 差距 → 是否吸收 → 吸收方案:

1. **Agent 主循环与错误恢复**:重试/退避、上下文溢出处理、工具调用失败的降级路径、循环终止条件;
2. **上下文压缩**:claude-code 的 `/compact`、阈值触发、compaction 事件与断路器 vs corlinman 现状;
3. **权限系统**:claude-code 的 permission modes(default/acceptEdits/plan/bypass)与规则文法 `Bash(cmd:*)`、多源优先级 vs corlinman 审批门;
4. **工具与沙箱**:hermes-agent 的多沙箱后端与 40+ 内置工具组织方式;工具 schema 校验与错误回传格式;
5. **MCP**:两边 client 实现(stdio+HTTP、sampling)vs corlinman `corlinman-mcp-server` + `gateway/mcp/` 的接线缺口;
6. **记忆分层**:hermes-agent 四层记忆(skills/FTS5/Honcho/MEMORY.md)的读写时机与提示注入策略;
7. **Subagent 编排**:claude-code 的 Task/fork 语义、hermes 的 kanban+委派 vs corlinman blackboard/runner;
8. **项目记忆**:CLAUDE.md 的发现规则、@include、/init、/memory 对应 CORLINMAN.md 的实现完成度;
9. **Hooks 生命周期**:hermes 6 个 hook 点位与 corlinman-hooks 对齐;
10. **输出与可脚本化**:print mode、`--output-format stream-json`、`--max-turns`;
11. **会话管理**:/resume、/rewind 文件快照、消息级 checkpoint;
12. **可观测性**:两边的 trace 粒度、token/成本核算面板。

### 2.3 产出与落地

1. 产出 `audit/ABSORB_MATRIX_<日期>.md`:上述 12 项的对比矩阵 + 吸收决策(采纳/改造采纳/不采纳+理由);
2. 按 价值/成本 排序分批落地,每批:实现 → 测试 → `make ci` 全绿 → CHANGELOG → 下一批;
3. 优先级默认顺序(可依据实测调整):MCP 接入 agent 工具面 > 上下文压缩补全 > 权限规则文法 > 错误恢复循环 > hooks 对齐 > 其余;
4. 所有吸收均为**理念与机制吸收,重新实现**,不复制对方代码(注意许可证)。

### 2.4 阶段二验收标准

1. 吸收矩阵 12 项全部有实证结论;
2. 决策为"采纳"的项全部落地并有回归测试;
3. `make ci` + `corlinman doctor` + 1.1-4 运行时冒烟仍然全绿(吸收不许引入回归);
4. `docs/PLAN_CLAUDECODE_PARITY.md` wave 表状态刷新至真实现状。

---

## 最终交付清单

- [ ] `audit/BUG_LEDGER_<日期>.md`(台账,P0/P1/P2 清零)
- [ ] `audit/ABSORB_MATRIX_<日期>.md`(对比矩阵与决策)
- [ ] 全部修复与吸收的 commit 序列(每个可独立回滚)
- [ ] `CHANGELOG.md` 更新 + 版本号递增
- [ ] `docs/TODO_FOLLOWUPS.md`、`docs/PLAN_CLAUDECODE_PARITY.md` 状态刷新
- [ ] 一份 ≤40 行的执行总结:修了什么、吸收了什么、明确遗留什么
