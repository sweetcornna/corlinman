# PLAN — 硬件联动:路由器/软路由接入、家电控制与直连诊断

> 状态:📋 规划(2026-07-02)。
> 愿景:corlinman 从"自托管 agent 平台"延伸为**家庭/边缘基础设施的 agent 大脑**——
> 通过路由器感知与控制局域网内的硬件,读取软路由等设备的硬件数据做直连诊断,
> 并在人审批门之下执行受控操作(重启服务、断网限速、场景联动家电)。
> 原则:**只读默认、写操作过审批门、全程审计**;所有硬件能力以现有插件/工具体系落地,不另起炉灶。

---

## 0. 与现有架构的映射(不新造机制,全部复用)

| 需求 | 复用的现有能力 |
| --- | --- |
| 硬件操作作为 agent 工具 | 插件系统(JSON-RPC 2.0 stdio `sync` 工具;长连接采集用 `service` 型插件 — 依赖阶段性补齐 service 插件支持,见 PROMPT_ZERO_BUG_PARITY 阶段一) |
| 危险操作拦截 | 人审批门(approval gate)+ admin UI 审批队列 |
| 定时巡检 | scheduler(cron 驱动任务) |
| 异常告警推送 | QQ / Telegram 渠道适配器 |
| 诊断入口 | `corlinman doctor` 模式复用 → 新增 `corlinman doctor --hardware` |
| 面板展示 | Tidepool admin UI 新增"硬件"页;状态卡(status card)复用公开只读链路 |
| 设备知识沉淀 | 记忆/RAG(设备档案、历史故障、修复方案入知识库) |
| 对外暴露 | `corlinman-mcp-server` 把硬件工具同时暴露为 MCP server,供其他客户端复用 |

新增一个包:`python/packages/corlinman-hardware` —— 硬件抽象层(HAL),
含设备注册表、能力模型、驱动接口;各具体驱动做成独立插件,不进核心。

## 1. 能力模型(HAL 核心抽象)

```
Device
 ├─ identity: id / vendor / model / firmware / 网络位置(ip, mac, 接入路由器)
 ├─ transport: ssh | http(s) api | snmp | mqtt | ubus | modbus …
 ├─ capabilities: [Capability]
 └─ policy: read_only | approval_required | auto_allowed(白名单动作)

Capability(三类)
 ├─ Sensor      只读采集:cpu/mem/温度/风扇/流量/在线设备/信号强度/功耗…
 ├─ Actuator    写操作:开关/重启/限速/固件升级/场景执行(默认全部走审批门)
 └─ Diagnostic  诊断动作:ping/traceroute/DNS 解析/带宽测试/端口探测/日志抓取
```

所有采集数据统一为带时间戳的指标点,落 SQLite 时序表(复用现有存储,先不引入 TSDB;
量大后再评估),并接入 Prometheus 导出。

## 2. 分阶段路线图

### Phase H1 — 软路由/路由器只读接入 + 直连诊断(第一优先)

目标:corlinman 能"看见"网络,回答"家里网为什么卡"并给出实证诊断。

驱动(按覆盖面排序,每个 = 一个插件):

| 目标系统 | 接入方式 | 采集项 |
| --- | --- | --- |
| OpenWrt(含各类软路由) | ubus over HTTP / LuCI RPC / SSH | CPU、内存、负载、温度、WAN 状态、DHCP 租约、无线客户端、conntrack 连接数、QoS 队列 |
| iKuai 爱快 | Web API | 带宽、分流、终端列表、协议流量 |
| MikroTik RouterOS | REST API / API 协议 | 接口流量、资源、防火墙计数 |
| 通用兜底 | SNMP v2c/v3 | 标准 IF-MIB / HOST-RESOURCES-MIB |
| 任意 Linux 边缘盒子 | SSH + 只读命令白名单 | sensors、smartctl、ethtool、ip -s |

诊断工具集(`Diagnostic`,agent 可直接调用):
`net.ping` `net.traceroute` `net.dns_check` `net.bandwidth_probe`(iperf3/内置)
`net.port_scan`(仅局域网、需审批)`router.log_tail` `router.client_list`。

诊断剧本(bundled skill,如 `network-doctor`):分层排查 WAN→路由→无线→终端,
输出结论 + 证据链;结果自动写入知识库形成设备病历。

验收:接入 ≥2 种真实路由系统;`corlinman doctor --hardware` 全绿;
在 QQ/Telegram 问"网络怎么样"能收到实测数据回答。

### Phase H2 — 局域网设备发现 + 家电控制

目标:corlinman 能列出家里有什么设备,并受控地开关/调节它们。

1. **发现**:经路由器数据(DHCP 租约/ARP)+ 主动 mDNS/SSDP 扫描,合并成设备注册表,
   自动指纹识别厂商(MAC OUI)。
2. **控制通道**(按杠杆率排序):
   - **Home Assistant REST/WebSocket API 优先** —— 一次接入即获得其全部生态(米家、
     Zigbee、HomeKit 桥接等),corlinman 做大脑,HA 做手脚;
   - MQTT(Zigbee2MQTT / Tasmota / ESPHome)直连,覆盖无 HA 的部署;
   - Matter controller 作为长期标准路线;
   - 米家/巴法云等云端 API 作为可选插件,默认不启用(隐私原则:局域网优先)。
3. **策略**:每个 Actuator 动作声明风险级;`auto_allowed` 白名单(如开灯)可免审批,
   其余(如断电、解锁类)强制走审批门 + 渠道内一键批准。

验收:通过 HA 或 MQTT 真实控制 ≥3 类家电;审批流在 QQ/Telegram 内闭环。

### Phase H3 — 主动运维与告警

1. scheduler 定时巡检(默认 5min 采集 / 1h 体检),阈值与趋势异常(温度飙升、
   丢包率、内存泄漏斜率)触发告警推 QQ/Telegram;
2. 告警附带 agent 自动初诊结论(调用 H1 诊断剧本),而非裸指标;
3. 受控自愈:预案化操作(重启 PPPoE、重启某容器)绑定审批门,批准后执行并回报;
4. admin UI"硬件"页:设备列表、实时指标、告警历史、审批入口。

### Phase H4 — 场景编排与多模态入口

1. 场景/规则 DSL(时间、传感器条件、人在检测 → 动作序列),由 agent 起草、人确认后启用;
2. 语音入口:接通真实 voice provider 后,"太热了"→ agent 推理 → 空调调温(过策略);
3. 状态卡公开链路复用:分享一个只读家庭网络健康页;
4. 设备病历 + 记忆:agent 记住"这台软路由每逢雨天掉线"类长程模式。

## 3. 安全模型(硬性要求,先于功能)

1. 凭据全部进现有密钥管理,不落配置明文;SSH 用专用低权账号 + 命令白名单;
2. 默认只读;所有 Actuator 默认 `approval_required`,白名单需显式配置;
3. 网络隔离建议:corlinman 所在主机与 IoT VLAN 的访问关系写入部署文档;
4. 全部硬件操作(含读)写审计日志,OTel span 标注 device_id;
5. 固件升级、防火墙修改、端口扫描永远不允许进 `auto_allowed`;
6. 对外暴露的状态卡/MCP 面默认脱敏(不含 MAC、内网拓扑细节)。

## 4. 里程碑与工作量估算

| 里程碑 | 内容 | 规模 |
| --- | --- | --- |
| M1 | `corlinman-hardware` 包:HAL 抽象 + 设备注册表 + SQLite 指标表 | M |
| M2 | OpenWrt + SNMP 两个驱动插件 + 诊断工具集 + `doctor --hardware` | M |
| M3 | network-doctor skill + 巡检 scheduler + 渠道告警 | S |
| M4 | admin UI 硬件页 + 审批接线 | M |
| M5 | Home Assistant 桥 + MQTT 直连 + 白名单策略 | M |
| M6 | 场景 DSL + 自愈预案 | L |

前置依赖:`service` 型插件支持与 MCP 工具面接线(见 `docs/PROMPT_ZERO_BUG_PARITY.md`
阶段一/二)必须先行,否则长连接采集与工具暴露无处落地。

## 5. 风险与对策

- **设备碎片化**:驱动只进插件市场不进核心;HAL 接口冻结后社区可自增驱动;
- **误操作实体世界**:审批门 + 白名单 + 动作风险分级,宁可打扰不可自动;
- **采集拖垮小内存软路由**:采集端限频限并发,SSH 会话复用,可配置降级为 SNMP;
- **云 API(米家等)风控/变更**:云通道全部可选插件化,核心路径只依赖局域网协议;
- **LLM 幻觉指挥硬件**:Actuator 参数经 HAL schema 严格校验,拒绝越界值。

---

## 附:Phase H1 启动提示词(届时直接交给 Claude Code)

> 按 `docs/PLAN_HARDWARE_INTEGRATION.md` 实施 Phase H1(M1+M2):
> 1) 新建 `python/packages/corlinman-hardware`,实现 Device/Capability HAL、设备注册表与
> SQLite 指标表,遵守 `.importlinter` 分层;2) 实现 OpenWrt(ubus/SSH)与 SNMP 两个驱动
> 插件及 `net.ping`/`net.traceroute`/`net.dns_check`/`router.client_list` 诊断工具;
> 3) 扩展 `corlinman doctor --hardware`;4) 全部写操作接审批门,凭据走密钥管理;
> 5) 每步过 `make ci`,补齐单测与文档,更新 CHANGELOG。验收以本计划 Phase H1 验收标准为准。
