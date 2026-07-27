# 语音合成（TTS）

corlinman 的语音能力由三部分组成：一个**数据驱动的后端注册表**、一个
`text_to_speech` 工具、以及**七个渠道的语音投递**。三者共用同一条
合成路径，所以在管理界面 `/voice` 里试听到的音色，和渠道里最终发出去的
语音是同一份产物。

- 后端目录：`python/packages/corlinman-agent/src/corlinman_agent/voice/`
- 管理路由：`corlinman_server/gateway/routes_admin_b/voice.py`
- 渠道投递：`corlinman_channels/voice_out.py`
- 前端：`ui/app/(admin)/voice/page.tsx`

## 1. 后端是数据，不是代码

一个 TTS 后端就是一行 `BackendDef` 数据：模型列表、音色表、可用容器、
凭据来源、以及 HTTP 请求形状。因此**新增一个厂商 = 加一行数据**，
**用户自己接一个服务 = 在配置里写一段 `[voice.backends.<id>]`**，两者在
UI 里完全平级，都有试听、都能被渠道使用。

内置后端：

| id | 传输 | 说明 |
| --- | --- | --- |
| `gpt_live` | WebRTC | OpenAI Realtime（`gpt-realtime-2.1` / `gpt-realtime-2.1-mini`），10 个音色 |
| `openai` | HTTP | `/v1/audio/speech`，13 个音色，兼容任何 OpenAI 形状的中转 |
| `fish` | HTTP | Fish Audio，音色 = `reference_id`（声音克隆句柄） |
| `elevenlabs` | HTTP | 音色 = `voice_id`，走 path 占位符 |
| `gemini` | HTTP | 音频以 base64 内联在 JSON 响应中 |
| `minimax` | HTTP | T2A v2，中文表现好 |

两种传输覆盖了所有厂商：

- `kind = "http"` —— 一次请求拿到音频（响应体是音频，或音频是 JSON 里的
  base64 字段）。
- `kind = "webrtc_live"` —— OpenAI Realtime 的 WebRTC 会话，见 §4。

## 2. 接入自定义 TTS

在 `config.toml` 里写一段声明即可，无需改代码：

```toml
[voice.backends.my_tts]
label = "自建 TTS"
base_url = "https://tts.example.com"
api_key = { env = "MY_TTS_KEY" }
models = ["v2"]
formats = ["mp3", "wav"]
default_voice = "xiaoyu"
voices = [
  { id = "xiaoyu", label = "小雨", tone = "轻快", description = "适合日常提醒" },
]

[voice.backends.my_tts.http]
path = "/api/tts"
method = "POST"
auth = "header"          # bearer | header | query | none
auth_header = "X-Token"
response = "binary"      # binary | json_b64
body = { content = "{text}", speaker = "{voice}", encoding = "{format}" }
```

**占位符替换规则**（`http_backend.py`）：

- 可用占位符：`{text}` `{voice}` `{format}` `{model}` `{speed}` `{instructions}`。
- 整个字符串**恰好**是一个占位符时按原始类型替换 —— `"{speed}"` 得到数字
  `1.25` 而不是字符串。
- 字符串**包含**占位符时做文本插值，`path` 也支持（ElevenLabs 把音色放在
  URL 里：`/text-to-speech/{voice}`）。
- 值解析为空的键会被**整键丢弃**，所以没填的 `instructions` 不会以空串
  发给会拒绝空值的厂商。
- 其余字段原样发送，可以直接写死常量。

音频在 JSON 里时：

```toml
response = "json_b64"
audio_path = "candidates.0.content.parts.0.inlineData.data"   # 支持数字下标
```

同名块也可以**扩展内置后端**（只钉一个中转地址，其余继承）：

```toml
[voice.backends.openai]
base_url = "https://your-relay.example.com/v1"
```

配置改动会在下次请求时生效（管理路由每次都重新同步注册表），删除某个块
它也会真的消失，不需要重启。

## 3. 凭据解析顺序

1. `[voice.backends.<id>].api_key`（可写 `{ env = "X" }`）
2. 后端声明的环境变量（`OPENAI_API_KEY` / `FISH_AUDIO_API_KEY` / …）
3. 当前 provider adapter 的 key —— persona 把 `voice` 能力绑到某个 provider
   时就是这条路径
4. 后端默认 `base_url`

**一条硬性保护**：如果 adapter 的 key 和 `OPENAI_API_KEY` 完全相同，而目标
后端不是 OpenAI 系，则**不借用** —— 避免把 OpenAI 凭据泄漏给第三方主机。

## 4. OpenAI Realtime（WebRTC）

Realtime 模型没有一次请求返回音频文件的 REST 端点，而是通过 WebRTC 会话
返回音轨。官方 API 使用 multipart SDP offer/answer 交换：

```
POST {base_url}/v1/realtime/calls
Authorization: Bearer <standard OpenAI API key>
multipart/form-data:
  sdp      (application/sdp)
  session  (application/json)
  -> 201 + 裸 answer SDP
```

corlinman 仍兼容旧 Sub2API：官方路径返回 404/405 时，再尝试 JSON
`POST /v1/live` 和 `POST /backend-api/codex/realtime/calls`。除此以外的错误
不会被回退掩盖。

合成流程：开会话 → 推一条用户消息 → 请求一次纯音频回复 → 录制入站音轨 →
拆会话。本地永远不挂麦克风（transceiver 是 `recvonly`，且
`turn_detection = null`，否则模型会一直等你说话）。

### 依赖与前置条件

- **可选依赖 `aiortc`**：`uv sync --extra voice`（仓库根目录即可）。没装时返回
  `gpt_live_dependency_missing`，不会在启动时炸。
- **官方 OpenAI API 不需要 macOS 伪装或 ChatGPT.app attestation**。标准 API
  key 可在任意服务端平台调用 `/v1/realtime/calls`。默认
  `base_url = "https://api.openai.com/v1"`，也可指向实现同一协议的中转。
- **旧 Sub2API `/v1/live` 仍受其 DeviceCheck 限制**：如果该中转返回
  `live_attestation_unavailable`，伪造 User-Agent、`runtime.GOOS` 或系统字段
  都无效，因为凭据由 Apple DeviceCheck 签发。应升级中转以支持官方 Realtime
  API，或直接为 `gpt_live` 配置标准 OpenAI API key；corlinman 不绕过该门禁。

管理界面的轻量探测会先试官方 multipart 路径。官方端点对占位 SDP 返回
400/422，说明路由和鉴权已通过，因此探测视为可用；401/403、网络错误以及旧
Sub2API 的 attestation 503 仍会原样显示。

corlinman 的 WebRTC 链路有回环测试覆盖（`test_gpt_live_webrtc_loopback.py`：
真实 aiortc 对端接收官方 multipart 请求、应答 SDP、推音轨并录出可播放文件）。

错误码：`live_attestation_unavailable` / `live_endpoint_missing` /
`live_http_status` / `live_timeout` / `gpt_live_dependency_missing`。

## 5. 管理接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/admin/voice/backends` | 后端目录（含自定义），带 `credential_set` |
| GET | `/admin/voice/settings` | 当前默认值，密钥以 `***REDACTED***` 返回 |
| PUT | `/admin/voice/settings` | 写回默认值；密钥留空或回传哨兵 = 保持不变 |
| POST | `/admin/voice/preview` | 试听：合成一段样本，返回 `/v1/files/{id}` |

试听走的是**同一个** `synthesize()`，因此 UI 里听到的就是渠道会发的。
预览音频通过 admin session cookie 桥接读取（`/v1/files` 不接受 Basic
Auth），前端 `<audio src>` 带 cookie 即可播放。

## 6. 渠道语音投递

`corlinman_channels/voice_out.py` 统一了两件以前分散在各渠道的事：判断
「这是不是音频」，以及「这个渠道的原生语音条要什么容器」。

| 渠道 | 原生语音条 | 处理 |
| --- | --- | --- |
| QQ / OneBot | ✅ `RecordSegment` | NapCat 自己转 SILK，直接 base64 内联 |
| Telegram | ✅ `sendVoice` | **mp3 自动转码为 OGG/Opus 48k 单声道**，否则只能当文档发 |
| 微信公众号 | ✅ `send_voice_customer` | 转码为 mp3 32k/16k 单声道、≤60s、≤2MB |
| QQ 官方 Bot | ⚠️ 仅 SILK | 腾讯只收 SILK，无通用编码器 → 非 SILK 时明确告知并跳过 |
| Discord / Slack / 飞书 | ❌ | 作为音频文件上传（客户端自带播放器），状态文案标注为音频 |
| Web 聊天 | ✅ 播放器 | `attachment-gallery` 直接渲染 `<audio controls>` |

**所有路径都有降级**：没有 ffmpeg、编码失败、超出体积上限，都会退回成普通
文件发送 —— 一条能播放的文件远好过一条没送到的语音。微信没有通用文件消息，
所以投递不了时会把原因追加到回复正文里，避免模型说"已发送语音"而用户什么
也没收到。

转码产物按「绝对路径 + mtime + 大小」哈希缓存在临时目录，同名重生成的音频
不会命中旧结果。

## 7. 音色

音色**按所选后端校验**，不再全局硬编码。未知 id 会回退到该后端默认音色；
克隆型后端（Fish / ElevenLabs / MiniMax）没有固定音色表，任何非空值原样透传。

- OpenAI Realtime（10 个）：Marin、Cedar、Alloy、Ash、Ballad、Coral、Echo、
  Sage、Shimmer、Verse（官方推荐 Marin / Cedar）
- OpenAI（13 个）：Marin、Cedar（新一代，推荐）+ Alloy、Ash、Ballad、Coral、
  Echo、Fable、Nova、Onyx、Sage、Shimmer、Verse

> OpenAI 后端默认音色是 `alloy` 而不是推荐的 `marin` —— `marin`/`cedar` 只
> 存在于 `gpt-4o-mini-tts`，仍钉在 `tts-1` 的部署用它们会 400。

## 8. 生效优先级与环境变量

音色/后端/模型的解析顺序，从高到低：

1. 工具调用参数（模型显式点名某个音色）；
2. persona / provider params（某个 persona 绑定了自己的音色）；
3. **`[voice]` 配置**（管理界面里选的默认值）；
4. `CORLINMAN_TTS_BACKEND` / `_MODEL` / `_VOICE` 环境变量；
5. 后端内置默认值。

配置**高于**环境变量：环境变量早于设置页存在，界面上改了值不应被宿主机上
一条陈旧的 export 悄悄盖掉。

`text_to_speech` 跑在 **agent 进程**里，它看不到网关的配置快照 —— `[voice]`
是通过 `py-config.json` sidecar 送过去的（`_apply_voice_config_from_sidecar`），
sidecar 每次重写都会重新应用，因此界面里保存即生效，无需重启 agent。
`CORLINMAN_TTS_TIMEOUT_SECS` 仍只走环境变量。

## 9. 与 /models 能力页的关系

`/models` 的**能力**标签页把「对话 / 图片生成 / 语音」三个绑定并排显示：
对话链接到路由页，图片生成可直接编辑（写 `[models].image_provider` /
`image_model`），语音显示当前 `[voice]` 摘要并链接回本页做逐音色试听。
语音的写入口只有这一处，避免两条写路径改同一份配置。
