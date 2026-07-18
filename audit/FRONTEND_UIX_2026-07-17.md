# 全前端 UI/UX 整改计划 — 2026-07-17

来源:4 个并行审计(状态正确性 / 中英混杂 / 文案密度 / 多余配置项),覆盖 ui/app/(admin) 全部 45 页 + 共享组件 + 双语言包,配置项逐字段对照后端路由验证。

## 系统性根因

1. **语言选择**:`resolvePreferredLang()`(lib/i18n.ts:43)在无 localStorage 偏好时按 `navigator.language` 回退——中文用户用英文系统浏览器会被静默切到 en;而 en 包里又嵌着中文碎片("说说"/"QQ-空间"),zh 包里也有英文残留(登录页 "Sign in"/heroTitle 等)。
2. **时间/状态不跟随服务端与界面语言**:`nextFireTime()` 按浏览器时区推算 cron(后端 croniter 按服务器时区);~25 处 `toLocale*` 不传界面语言;QQ 消息/日志时间戳按 UTC 裸切 ISO。
3. **文案模板性堆叠**:页面导语 + 卡片描述 + 每字段帮助三层散文;13 个导语直接印 `/admin/*` API 路径;正则、config key、Rust 模块名遍布帮助文案。无 Tooltip/Collapsible 原语可收纳长帮助。
4. **表单不做推导**:可机械推导的字段(qzone 任务名 = `persona.daily_qzone`、provider env-var、persona id)全都让用户手填;专家字段(cron、endpoint URL、预算阈值)不折叠。profiles 创建弹窗是正确范式。

## 波次(每波一个 PR)

### W1 — 状态正确性(前端+后端)
- [后端] QQ `StatusOut` 从不设置 `runtime` → 徽章恒 "unknown"(routes_admin_a/channels.py:127-143,对照 telegram :371-387 修);UI 恒显 "1 throttled" 是从连接枚举造出来的(channels/qq/page.tsx:225)。
- qzone "下次触发" 预览时区错位(scheduler.ts nextFireTime + page.tsx:263,446);行内 last_run 用 toLocaleString() 无 locale。
- QQ 消息流(qq-util.ts:78-88)与日志页(log-detail-drawer.tsx:166-176)UTC 裸切 → 本地时间 + 跟随界面语言。
- 开关后失效 key 失配:qq-hero.tsx:121(["qq-status"])、telegram/page.tsx:314 → 实 key 是 ["admin","channels",...]。
- 新建 lib/format.ts:formatDateTime/formatNumber 读 i18next.language;清扫 ~25 处 toLocale* 调用点(清单见状态审计)。

### W2 — 语言机制
- i18n.ts:43 无存储偏好时默认 zh-CN(不再按 navigator 落到 en)。
- Telegram 页 0 个 t() 调用、全硬编码英文,而 channels.telegram.tp.* 翻译早已存在 → 接上。
- 补缺失命名空间(两包同补):evolution.settings.*(27,现渲染裸 key)、skills.drawer.*(19,裸 key)、playground.agentPicker.*(7,裸 key)、identity.*(整页英文默认)、onboard.*、channels.qq.accountOffline*、common.copied/saved、config.sectionModified、models.pickerAddedHint。
- 清 en 包中文碎片(schedulerQzone.lede/enableDaily/dailyEnabled/3407 立绘)与 zh 包英文残留(auth.signIn/heroTitle/sessionHint、plugins.testInvoke、models.paramsTitle、update.newVersion、persona.fieldSystemPrompt);拆双语确认串(sessions/persona 删除弹窗)。
- persona/page.tsx:989 硬编码占位符;components/todo-card.tsx 死组件删除。

### W3 — 文案瘦身与层次
- 原则:导语一句话、无 API 路径/正则/config key/模块名;卡片描述与标签重复的删;字段帮助 ≤1 短句,长契约进 title=/抽屉。
- 新增 components/ui/field-hint.tsx + Tooltip 原语(现无任何收纳原语)。
- 按排名先改:qzone、telegram 系(FullInboxChannelPage 共享)、marketplace/contribute、persona、providers、scheduler、approvals、config、rag、models/tenants/profiles;auth 密码重置提示(en.ts:65 教用户 SSH+argon2)单独重写。
- 对齐已干净页标准:modelHub/skills/system/subagents/sessions。

### W4 — 配置项精简
- qzone:任务名删除(自动 `${persona.id}.daily_qzone`)、提示词预填 DEFAULT_DAILY_PROMPT、cron 折叠为预设+高级、表单与一键按钮合并。
- RAG 标签过滤器:后端验证死字段(请求不带、路由不收)→ 删。
- persona:id 由 display_name 自动 slug、short_summary 放开必填(后端本有默认)、永久 disabled 的测试框删。
- channels 编辑器:6 类 endpoint URL 折叠进"高级"。
- evolution:预算分项 + 回滚阈值(9 个数字)折叠。
- provider 弹窗:base_url 仅 openai_compatible 显示、env-var 按 kind 预填、params 折叠。
- tenants:display_name 折叠(后端默认 = slug)。
- 范式:profiles create-profile-modal 的 "+ advanced" 折叠。

## 审计原始报告要点索引
- 状态:8 项(QQ runtime 高危;SSE 重连、scheduler 主页、subagent 计时已核实干净)。
- 混杂:双包 2962 leaf key 结构一致(satisfies 强制),缺失 = 双包同缺;裸 key 三簇最刺眼。
- 文案:13 个 API 路径导语;最重单页 marketplace/contribute(~280 词)。
- 配置:最明确三例 = RAG 死过滤器、qzone 任务名、provider env-var。
