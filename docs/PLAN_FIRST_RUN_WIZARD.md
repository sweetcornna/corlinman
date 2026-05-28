# First-Run Wizard + 主聊天窗口 + 图片 Provider — Implementation Contract

This document is the shared contract for the 6 parallel implementation agents.
Each agent owns a non-overlapping slice of files but consumes the API shapes
defined here.

## Goal (user-stated)

1. Single-command install of latest version (already exists via `deploy/install.sh`; only docs need polish).
2. Front-end first-run wizard with steps (in this exact order):
   - API config (key + endpoint) — **skippable**
   - Change default username — **required** (must be done **before** password)
   - Change default password — **required** (gated by step 2)
   - Persona customization — three choices: create custom / use default `grantley` / skip
   - Image generation API — three choices: reuse current chat provider (probe) / configure separate / skip
3. On a channel's first chat with an agent, system injects a one-time tip
   pointing users to `/sethome` to designate that channel as their "home
   channel". After set, important system notifications (server restart, etc.)
   are delivered only to that home channel.
4. UI: rename sidebar entry "系统/System" → "更新/Updates" and tighten the
   page's feature description.

## File ownership (avoid collisions)

| Agent | Owned files |
|------|-------------|
| **A** (sidebar/i18n) | `ui/components/layout/sidebar.tsx`, `ui/lib/locales/zh-CN.ts`, `ui/lib/locales/en.ts`, `ui/app/(admin)/system/page.tsx` (text only) |
| **B** (onboard backend) | `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_b/onboard.py`, `…/routes_admin_b/personas.py` (add use-default endpoint) |
| **C** (image provider) | `python/packages/corlinman-providers/src/corlinman_providers/specs.py`, **new** `…/corlinman_providers/capabilities.py`, `python/packages/corlinman-agent/src/corlinman_agent/image/generate.py`, **new** `…/routes_admin_b/image_provider.py` route module |
| **D** (channels) | `python/packages/corlinman-channels/src/corlinman_channels/commands.py` (handlers only — register /sethome and /use-default-persona), **new** `…/corlinman_server/home_channel_store.py`, `…/corlinman_server/gateway/services/chat_bootstrap.py` (first-chat tip injection), `…/corlinman_server/gateway/lifecycle/entrypoint.py` (restart broadcast hook) |
| **E** (onboard UI) | `ui/app/onboard/page.tsx`, `ui/lib/api.ts` (new client functions), `ui/lib/auth.ts` (no new exports beyond what already exists) |
| **F** (skill + docs) | `python/packages/corlinman-server/src/corlinman_server/bundled_skills/configure-persona/SKILL.md`, `README.md`, `docs/quickstart.md` |

Agent E may import any **new** API client function it needs from `ui/lib/api.ts`
or `ui/lib/auth.ts` — define them as thin wrappers around the endpoints below.

## Backend endpoints — contract

All under `/admin` prefix, all require admin auth (already-logged-in session).

### B1 — `POST /admin/onboard/finalize-account`
- Body: `{ new_username: string }`
- Calls existing `POST /admin/username` logic internally (or just reuses the
  service function in `auth.py:change_username`); accepts the **current**
  password from session, not from body — i.e. on first-run flow we trust the
  authed session.
- Returns `{ status: "ok", username: string }`
- **Error**: `409 username_unchanged` if new_username == current.

### B2 — `POST /admin/onboard/finalize-password`
- Body: `{ old_password: string, new_password: string }`
- Wraps `auth.py:change_password`.
- Returns `{ status: "ok", must_change_password: false }`.

### B3 — `POST /admin/onboard/finalize-persona`
- Body: `{ choice: "skip" | "default" | "custom" }`
  - `skip`: do nothing
  - `default`: ensure default `grantley` persona exists & mark active
  - `custom`: returns `{ status: "ok", redirect: "/persona" }` — UI navigates
    to persona page or kicks off `/persona` wizard
- Returns `{ status: "ok", choice: ..., redirect?: string }`

### B4 — `POST /admin/onboard/finalize-image-provider`
- Body: one of:
  - `{ choice: "skip" }`
  - `{ choice: "reuse", provider_name: string }` — probe current provider
    (delegates to C); on miss returns `409 image_not_supported` with body
    `{ supported: false, hint: string }` so the UI can offer fallback.
  - `{ choice: "separate", spec: ProviderSpec }` — create a new provider with
    `image_capable=true`.
- Returns `{ status: "ok", image_provider: string }` on success.

### B5 — `POST /admin/personas/use-default`
- Body: `{}`
- Idempotent: ensure built-in `grantley` persona row exists; mark as the active
  persona for the requesting admin/operator.
- Returns `{ status: "ok", persona_id: "grantley" }`.

### C1 — `POST /admin/providers/{name}/probe-image`
- Returns `{ supported: bool, evidence: string, models?: string[] }`
- Implementation: try `GET {base_url}/v1/models` and look for known image
  models (e.g. `gpt-image-1`, `dall-e-*`, `flux-*`, `imagen-*`); if not found,
  optionally try `OPTIONS`/`HEAD` on `/v1/images/generations`. Cheap,
  non-destructive only.

### D1 — `/sethome` slash command (handler-mode, sync, no LLM)
- Trigger: `/sethome` / `/主页` (Chinese alias)
- Saves current `ChannelBinding` (channel, account, thread, sender) as the
  user's home channel via `home_channel_store.set_home(user_id, binding)`.
- Reply: `"✅ 主聊天窗口已设置：<channel>/<thread>. 重启与重要系统提醒将发送到此处。"`

### D2 — `/use-default-persona` slash command (handler-mode)
- Trigger: `/use-default-persona` / `/默认人格`
- Calls B5 internally; replies with confirmation.

### D3 — first-chat reminder injection
- In `chat_bootstrap`, before dispatching the LLM, if
  `SessionSummary.turn_count <= 1` and **no** `/sethome` has been issued in
  this session before, **prepend** a one-time system message:
  > "💡 提示：使用 `/sethome` 命令可将当前窗口设为主聊天窗口，重启等系统提醒只发到主窗口。"
- Mark a flag in `home_channel_store` to avoid re-injecting.

### D4 — restart broadcast
- On server startup (in `entrypoint.py` post-boot hook), iterate all
  registered home channels and enqueue a system message:
  > "🔄 服务器刚刚重启完成（vX.Y.Z）"

## Step ordering (front-end gating)

The wizard MUST enforce this order; the back end is forgiving but the UI never
shows steps out of order:

```
0: detect must_change_password / first-run state
1: API config        (skip → 2)
2: change username   (must succeed → 3)
3: change password   (must succeed → 4)
4: persona choice    (skip/default/custom)
5: image provider    (skip/reuse/separate)
6: done → redirect to /admin
```

The "先改账号 再改密码" rule prevents the (perceived) cookie-mismatch issue
even though backend currently keeps the session alive across rename.

## i18n keys to add (Agent A & E)

```
sidebar.updatesLabel              // "更新" / "Updates"
system.pageTitle                  // "更新管理" / "Update management"
system.pageSubtitle               // "查看版本、检查并应用更新" / "Check version, run upgrades"
onboard.step.api.title            // "配置 API"
onboard.step.username.title       // "修改默认账号"
onboard.step.password.title       // "修改默认密码"
onboard.step.persona.title        // "助手个性化"
onboard.step.image.title          // "图片生成 API"
onboard.persona.choice.default    // "使用默认助手"
onboard.persona.choice.custom     // "创建自定义人格"
onboard.persona.choice.skip       // "暂时跳过"
onboard.image.choice.reuse        // "复用当前 API"
onboard.image.choice.separate     // "单独配置"
onboard.image.choice.skip         // "跳过"
onboard.image.notSupported        // "当前 API 不支持图片生成"
```

## Data model additions

### Agent C — `ProviderSpec`
Add **optional** field on `ProviderSpec`:
- `image_capable: bool = False` — operator-asserted or probe-confirmed
- `image_model: str | None = None` — preferred model id (e.g. `gpt-image-1`)

### Agent D — Home channel store
A small SQLite-backed table (extend identity store or new module
`home_channel_store.py`):
```
home_channels (
  user_id TEXT PRIMARY KEY,
  channel TEXT NOT NULL,
  account TEXT NOT NULL,
  thread TEXT NOT NULL,
  sender TEXT NOT NULL,
  set_at_ms INTEGER NOT NULL
)

first_chat_tips_shown (
  user_id TEXT,
  channel TEXT,
  thread TEXT,
  PRIMARY KEY(user_id, channel, thread)
)
```

Helpers:
- `set_home(user_id, binding) -> None`
- `get_home(user_id) -> ChannelBinding | None`
- `list_all_homes() -> list[(user_id, ChannelBinding)]`
- `mark_tip_shown(user_id, channel, thread) -> None`
- `was_tip_shown(user_id, channel, thread) -> bool`

## Out-of-scope (do NOT touch)

- Existing `POST /admin/username` and `POST /admin/password` — keep working as-is.
- Persona wizard `/persona` Stage 0–6 internal behaviour — Agent F only
  prepends a "Stage -1" branch to the SKILL.md.
- `deploy/install.sh` — already correct; Agent F only updates README/quickstart
  to make the recommended command more prominent.
