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
| `gpt_live` | WebRTC | GPT-Live 实时模型（`gpt-live-1` / `gpt-live-1-mini`），9 个音色 |
| `openai` | HTTP | `/v1/audio/speech`，13 个音色，兼容任何 OpenAI 形状的中转 |
| `fish` | HTTP | Fish Audio，音色 = `reference_id`（声音克隆句柄） |
| `elevenlabs` | HTTP | 音色 = `voice_id`，走 path 占位符 |
| `gemini` | HTTP | 音频以 base64 内联在 JSON 响应中 |
| `minimax` | HTTP | T2A v2，中文表现好 |

两种传输覆盖了所有厂商：

- `kind = "http"` —— 一次请求拿到音频（响应体是音频，或音频是 JSON 里的
  base64 字段）。
- `kind = "webrtc_live"` —— GPT-Live 的实时会话，见 §4。

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

## 4. GPT-Live（WebRTC）

GPT-Live 没有 REST 语音端点，唯一通道是 WebRTC 会话。网关把它暴露成一次
SDP offer/answer 交换：

```
POST {base_url}/v1/live
{"sdp": "<offer sdp>", "session": { ...realtime session json... }}
  -> "<answer sdp>"   （裸 SDP）或 {"sdp": "<answer sdp>"}（JSON）
```

Codex 风格的别名 `POST /backend-api/codex/realtime/calls` 请求体完全相同，
代码会依次尝试两个路径（前者 404 才继续）。

合成流程：开会话 → 推一条用户消息 → 请求一次纯音频回复 → 录制入站音轨 →
拆会话。本地永远不挂麦克风（transceiver 是 `recvonly`，且
`turn_detection = null`，否则模型会一直等你说话）。

### 依赖与前置条件

- **可选依赖 `aiortc`**：`uv sync --extra voice`（仓库根目录即可）。没装时返回
  `gpt_live_dependency_missing`，不会在启动时炸。
- **网关必须能做 Live attestation**。这是 Sub2API 侧的硬性条件，源码里是
  编译期分支（`liveattestation/attestation_darwin.go` vs
  `attestation_unsupported.go`），三条同时满足才行：

  1. Sub2API **跑在 macOS 上**（非 darwin 一律编译进 unsupported 分支）；
  2. **Apple Silicon**——`runtime.GOARCH != "arm64"` 直接报
     *"live attestation currently requires Apple Silicon"*；
  3. 该机器上装有**官方 ChatGPT.app**（`/Applications/ChatGPT.app` 或
     `~/Applications/ChatGPT.app`）——attestation 取自该 app 的 Apple
     DeviceCheck 凭证。

  任一不满足时返回：

  ```
  503 {"error":{"message":"Live attestation is unavailable: live attestation
       is only supported when Sub2API runs on macOS; ..."}}
  ```

  这个门禁在校验 SDP 和模型 id **之前**触发，所以管理界面的试听会先做一次
  轻量探测（`probe_live_endpoint`），直接告诉你"网关无法 attest"，而不是让
  你先去装 `aiortc` 再发现一样跑不通。

  换言之：**部署在 Linux VPS 上的 Sub2API 永远无法提供 GPT-Live**。要用它，
  需要在一台装了 ChatGPT.app 的 Apple Silicon Mac 上跑一个 Sub2API 实例，
  并把 `[voice.backends.gpt_live].base_url` 指向它。corlinman 这一侧的 WebRTC
  链路已有回环测试覆盖（`test_gpt_live_webrtc_loopback.py`：真实 aiortc 对端
  应答 SDP、推音轨、录出可播放文件），所以剩下的唯一变量就是网关。

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

- GPT-Live（9 个）：Arbor、Breeze、Cove、Ember、Juniper、Maple、Sol、Spruce、Vale
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
