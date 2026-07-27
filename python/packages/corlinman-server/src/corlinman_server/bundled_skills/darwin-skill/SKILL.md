---
name: darwin-skill
description: "Darwin Skill (达尔文.skill)：自主 skill 优化器，借鉴 Karpathy autoresearch。8 维度评分（结构+实测）、git 版本控制下 hill-climbing、测试 prompt 验证改进、生成视觉成果卡片。"
when_to_use: "用户提到「优化skill」「skill评分」「自动优化」「auto optimize」「skill质量检查」「达尔文」「darwin」「帮我改改skill」「skill怎么样」「提升skill质量」「skill review」「skill打分」时使用。"
---

# Darwin Skill

> 借鉴 Karpathy autoresearch 的自主实验循环，对 skills 进行持续优化。
> 核心理念：**评估 → 改进 → 实测验证 → 人类确认 → 保留或回滚 → 生成成果卡片**
> GitHub: https://github.com/alchaincyf/darwin-skill

---

## 设计哲学

autoresearch 的精髓：
1. **单一可编辑资产** — 每次只改一个 SKILL.md
2. **双重评估** — 结构评分（静态分析）+ 效果验证（跑测试看输出）
3. **棘轮机制** — 只保留改进，自动回滚退步
4. **独立评分** — 评分用子agent，避免「自己改自己评」的偏差
5. **人在回路** — 每个skill优化完后暂停，用户确认再继续

与纯结构审查的区别：不只看 SKILL.md 写得规不规范，更看改完后**实际跑出来的效果是否更好**。

## 如何读取 references/

本 skill 的详细内容按主题拆在 `references/*.md`（与本文件同目录）。在 corlinman 运行时中，用 `Skill` 工具带 `file` 参数读取，例如：`Skill(name="darwin-skill", file="references/optimization-loop.md")`。其他 runtime 中，直接按相对本 skill 根目录的路径读取对应文件即可。**开工前必须先读与任务匹配的 reference，不要只凭本文件行事。**

## 工作内容（概览）

| 主题 | 内容 | 读 |
|------|------|-----|
| 评估 Rubric | 8 维度（结构 60 分 + 效果 40 分）评分标准与「实测表现」打分方式 | `references/rubric.md` |
| Runtime 适配性审查 | gate 项：红灯信号 / 绿灯措辞 / 例外清单 / 扫描命令 | `references/runtime-neutrality.md` |
| 自主优化循环 | Phase 0-3 完整流程（测试 prompt 设计、基线评估、优化循环、汇总报告）+ results.tsv 格式 | `references/optimization-loop.md` |
| 优化策略库与异常 | P0-P3 策略优先级、异常与边界条件 fallback 表 | `references/strategies-and-guards.md` |
| 成果卡片与设计灵感 | Result Card 3 风格模板、截图流程、autoresearch 对应关系 | `references/result-cards.md` |

**执行纪律**：Phase 0.5 测试 prompt 与 Phase 1 基线评分展示后都要暂停等用户确认；每个 skill 优化完必须过人类检查点。

## 约束规则

1. **不改变skill的核心功能和用途** — 只优化"怎么写"和"怎么执行"，不改"做什么"
2. **不引入新依赖** — 不添加skill原本没有的scripts或references文件
3. **每轮只改一个维度** — 避免多个变更导致无法归因
4. **保持文件大小合理** — 优化后SKILL.md不应超过原始大小的150%
5. **尊重花叔风格** — 中文为主、简洁为上
6. **可回滚** — 所有改动在git分支上，用git revert而非reset --hard
7. **评分独立性** — 效果维度必须用子agent或至少干跑验证，不能在同一上下文里「改完直接评」
8. **Runtime 中立性** — skill 必须能在 Claude Code、Codex、Cursor、OpenClaw、Hermes 等任何 skills-compatible runtime 中正常运行。除非 skill 名明确绑定单一 runtime（如 `xxx-codex`、`huashu-slides-codex`），任何「在 Claude Code 里」「Claude Code skill」「单一 badge 钉死」「安装命令只给 `.claude/skills/` 一种路径」都视为 gate 不通过，须在 P0 优先修复（详见「Runtime 适配性审查」章节）

## 使用方式

### 全量优化（推荐首次使用）
```
用户："优化所有skills"
→ Phase 0-3 完整流程
→ 建议：先基线评估，选择分数最低的5-10个重点优化
```

### 单个优化
```
用户："优化 huashu-slides 这个skill"
→ 只对指定skill执行 Phase 0.5-2
```

### 仅评估不改
```
用户："评估所有skills的质量"
→ 只执行 Phase 0.5-1（设计测试prompt + 基线评估），不进入优化循环
```

### 查看历史
```
用户："看看skill优化历史"
→ 读取并展示 results.tsv
```
