# PLAN — Persona Studio: user-defined characters with emoji + reference-image packs

**Initiative**: generalize hermes-agent's "格兰 (Grantley)" daily-QZone stack into a multi-persona studio any user can configure.
**Target release**: v1.6.0.
**Status**: APPROVED 2026-05-26 — W1 starting.
**Author**: claude-opus-4-7. **Date**: 2026-05-26.

---

## Context

`hermes-agent` runs a fully-automated daily QZone (说说) pipeline for the tiger persona **Grantley Bell**. Concrete artifacts there:

| hermes-agent component | what it does |
|---|---|
| `tools/qzone_tool.py` (`qzone_publish` tool) | posts text + images to QQ 空间 via OneBot-borrowed cookies |
| `tools/image_with_refs.py` (`image_with_refs` tool) | OpenAI Responses API + `gpt-image-2` with character 立绘 as refs |
| `tools/onebot_client.py` | borrows a live QQ login from NapCat to obtain QZone cookies |
| `~/.hermes/characters/{grantley,algo,oscar,…}.png` | 11 fixed character立绘 PNGs (refs for image generation) |
| `cron/{scheduler,jobs}.py` | timed daily-说说 dispatch |

`corlinman` is the active Python successor. Persona-infra survey (`adaa5b64ccdfb9b6f`) found **the foundation is already half-built**:

| corlinman surface | state | notes |
|---|---|---|
| `persona/store.py` PersonaStore (sqlite) | ✅ done | `id`, `display_name`, `short_summary`, `system_prompt`, `is_builtin` |
| `gateway/routes_admin_a/personas.py` CRUD | ✅ done | 5 endpoints + QQ humanlike toggle |
| `/admin/persona` page (table + editor modal) | ✅ done | shadcn form, slug validator, toast |
| `channels_runtime/_live_humanlike` live resolver | ✅ done | per-message lookup, no channel restart |
| `default_grantley` builtin persona body | ✅ done | already seeded at boot |
| scheduler framework (`scheduler/{runner,cron,…}`) | ✅ done | Python port, more complete than hermes |

What is **missing** for the user's expanded ask ("any user can define their own persona via chat or UI, with emoji pack + reference images"):

| Gap | Layer |
|---|---|
| Asset model (emoji + reference images) — table + filesystem | data |
| Asset upload route (multipart) + asset serving route | server |
| Asset upload UI (drag-drop in persona editor) | ui |
| Agent-side `persona.*` tools so the agent can read / mutate personas mid-conversation | agent |
| `image_with_refs` tool — pulls refs from the active persona | agent |
| `qzone_publish` tool + OneBot client | agent |
| `qzone.daily_publish` scheduler builtin action | server |
| Emoji-aware reply rendering (model knows which emoji labels exist) | agent + channels |

## Goal

A user can:
1. **Define a persona** via `/admin/persona` (existing UI, extended) or via conversation with the agent.
2. **Upload assets** to a persona — emoji/sticker images keyed by label (e.g. `happy.png`, `angry.png`) and reference images keyed by view label (e.g. `front`, `casual`, `swim`).
3. **Bind a persona to a channel** (the existing QQ humanlike toggle — extended to Telegram/Discord/Slack/Feishu).
4. **Receive in-character replies that use uploaded emoji** when the model judges it appropriate (via `send_attachment` → emoji file path).
5. **Schedule a daily QZone (or other) post** that calls `image_with_refs` (using the persona's ref pack) and `qzone_publish` (using the persona's voice).

The Grantley daily-说说 setup ships as **one preconfigured instance** of this system — not a hardcoded special case.

## Non-goals

- Per-user persona ownership (`owner_user_id`). The current single-tenant model stays; we add a nullable `owner_user_id` column for future-proofing but no auth enforcement now.
- Animated stickers / video emoji.
- Cross-channel emoji translation (Slack custom emoji `:foo:` syntax stays out of scope; we send images via `send_attachment`).
- Replacing the existing OpenAI image generator with a Anthropic / local option — the abstraction will allow it later but Phase-1 uses whatever the user's existing provider config exposes.

---

## Architecture

```
┌─────────── Persona Studio data plane ──────────────────────────┐
│                                                                │
│  personas table (existing) ────┬──> persona_assets table (NEW) │
│     id, system_prompt, …       │     id, persona_id, kind,     │
│                                │     label, file_name, mime,   │
│                                │     size_bytes, sha256,       │
│                                │     created_at_ms             │
│                                │                               │
│                                └──> filesystem (NEW)           │
│                                     <DATA_DIR>/personas/<id>/  │
│                                       emoji/<sha256>.<ext>     │
│                                       refs/<sha256>.<ext>      │
└────────────────────────────────────────────────────────────────┘
                                  │
        ┌─────────────────────────┼─────────────────────────────┐
        ▼                         ▼                             ▼
┌─── HTTP (admin) ───┐  ┌── HTTP (serve) ──┐         ┌─── Agent tools ───┐
│ POST /admin/personas│  │ GET /admin/      │         │ persona.list      │
│   /{id}/assets     │  │   personas/{id}/ │         │ persona.create    │
│ DELETE …           │  │   assets/{aid}   │         │ persona.update    │
└────────────────────┘  └──────────────────┘         │ persona.delete    │
        │                        │                   │ persona.attach_   │
        ▼                        │                   │   asset_from_url  │
┌── UI / admin ───────┐          │                   │ persona.list_     │
│ persona editor      │          │                   │   assets          │
│ drag-drop zones     │ ─────────┘                   │ image_with_refs   │
│ asset preview grid  │                              │ qzone_publish     │
│ humanlike picker    │                              │ send_emoji        │
└─────────────────────┘                              └───────────────────┘
                                                              │
                                                ┌─── scheduler ──────┐
                                                │ qzone.daily_publish│
                                                │  (builtin action)  │
                                                └────────────────────┘
```

### Storage strategy

Filesystem under `<CORLINMAN_DATA_DIR>/personas/<persona_id>/{emoji,refs}/<sha256>.<ext>`, metadata in sqlite. Rationale:

- Mirrors the existing `workspace/` convention (same data dir, same backup discipline).
- Cheaper to serve large images (no SQLite row → JSON pipeline; just `FileResponse`).
- Dedup via sha256 filename — same emoji uploaded twice costs one inode.
- `sha256` doubles as ETag for HTTP caching.
- Easy to wipe one persona's pack: `rm -rf <DATA_DIR>/personas/<id>/`.

### Conversational upload UX

The agent **cannot** receive image bytes from the web playground today (HTTP `ChatRequest` has no `attachments` field). The conversational path therefore uses one of:

1. **QQ / Telegram channel** — those adapters already parse multimodal segments into `Attachment` objects on `InternalChatRequest`. The agent can call `persona.attach_emoji_from_attachment(persona_id, label)` to pin the most-recent inbound image.
2. **URL pasted into chat** — `persona.attach_asset_from_url(persona_id, kind, label, url)`. The server downloads + validates + stores.
3. **Hand-off to /admin/persona** — for web users, the agent says "open the persona editor and drop the file on the emoji zone." This is the lowest-effort path and the recommended default in Phase 1.

We ship (2) + (3) in Phase 1. (1) lands in Phase 2 (needs a small agent_servicer extension to expose the last-turn attachments to the dispatch handler).

---

## Waves

### W1 — Asset storage layer (data + server)

**W1.1 — Schema migration**
- Add `personas` migration: nullable `owner_user_id TEXT` (future multi-tenant).
- New table `persona_assets`:
  ```sql
  CREATE TABLE persona_assets (
      id TEXT PRIMARY KEY,                  -- ulid
      persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
      kind TEXT NOT NULL,                   -- 'emoji' | 'reference'
      label TEXT NOT NULL,                  -- 'happy' | 'front' | …
      file_name TEXT NOT NULL,              -- original upload name
      mime TEXT NOT NULL,
      size_bytes INTEGER NOT NULL,
      sha256 TEXT NOT NULL,
      created_at_ms INTEGER NOT NULL,
      UNIQUE(persona_id, kind, label)
  );
  CREATE INDEX idx_persona_assets_persona ON persona_assets(persona_id);
  ```
- File: `python/packages/corlinman-server/src/corlinman_server/persona/store.py` (extend `PersonaStore.__init__` schema block).

**W1.2 — `PersonaAssetStore`**
- New module `persona/asset_store.py`. API:
  - `put(persona_id, kind, label, bytes_, mime, file_name) -> AssetRecord`
  - `get(persona_id, kind, label) -> AssetRecord | None`
  - `list(persona_id, kind=None) -> list[AssetRecord]`
  - `delete(persona_id, kind, label) -> bool`
  - `delete_all(persona_id) -> int`
  - `read_bytes(asset_id) -> bytes` (for serving)
  - `path_for(asset_id) -> Path` (for `send_attachment` callers)
- Validates MIME (`image/png` | `image/jpeg` | `image/webp` | `image/gif`).
- Size cap 8 MiB per asset (Telegram-friendly + QQ-friendly).
- Per-persona total cap 200 MiB.

**W1.3 — Admin asset routes** (extend `routes_admin_a/personas.py`)
- `POST /admin/personas/{id}/assets` — multipart: `kind`, `label`, `file`. Returns `AssetRecord`.
- `GET /admin/personas/{id}/assets` — list (paginated by kind). Returns `{ assets: [...] }`.
- `GET /admin/personas/{id}/assets/{asset_id}` — serve file with `ETag: sha256`, `Cache-Control: max-age=86400, immutable`.
- `DELETE /admin/personas/{id}/assets/{asset_id}` — 204.
- Existing `DELETE /admin/personas/{id}` is extended to call `asset_store.delete_all` before persona row removal.

**W1.4 — Tests**
- `tests/persona/test_asset_store.py` — round-trip put/get/list/delete, MIME reject, size cap.
- `tests/gateway/routes_admin_a/test_persona_assets.py` — multipart upload, ETag, 404, delete cascade.

### W2 — UI extensions to `/admin/persona`

**W2.1 — Asset upload zones in editor modal**
- File: `ui/app/(admin)/persona/page.tsx`. Add two collapsible sections to the editor modal:
  - **Emoji pack** — drag-drop grid. Each cell shows a label input + preview + delete. Click "+" to upload a new file (label defaults to filename stem).
  - **Reference images** — same shape, separate section. Limited to 8 active refs (image-with-refs LLM call has a practical limit).
- File: `ui/lib/api/personas.ts`. Add `listAssets`, `uploadAsset`, `deleteAsset`.
- Optimistic UI on upload; on failure rollback + toast.

**W2.2 — Asset reuse across personas** (deferred to W6 — skip in v1)

**W2.3 — Tests**
- `ui/__tests__/persona-editor.spec.tsx` — upload mock, label edit, delete confirm.

### W3 — Agent-side persona tools

**W3.1 — Tool schemas + dispatchers**
- New module `python/packages/corlinman-agent/src/corlinman_agent/persona/`:
  - `__init__.py` — exports tool names + schemas.
  - `tools.py` — schemas for `persona.list`, `persona.get`, `persona.create`, `persona.update`, `persona.delete`, `persona.list_assets`, `persona.attach_asset_from_url`.
  - `dispatch.py` — handlers; talks to `PersonaStore` + `PersonaAssetStore` directly (in-process).
- Register in `agent_servicer.BUILTIN_TOOLS` + `_builtin_tool_schemas()` + `_dispatch_builtin()` switch.
- The handlers RETURN compact JSON; full asset bytes never round-trip through the model.

**W3.2 — Permission gate**
- All `persona.{create,update,delete,attach_*}` tools route through `PermissionGate` so an admin-curated allowlist can keep them out of low-trust profiles.

**W3.3 — Tests**
- `tests/persona/test_dispatch.py` — happy path + idempotency on duplicate slug.

### W4 — `image_with_refs` tool

**W4.1 — Provider abstraction**
- New module `corlinman_agent/image/generate.py`. Single function:
  ```python
  async def generate_with_refs(
      provider: CorlinmanProvider,
      prompt: str,
      ref_paths: list[Path],
      aspect_ratio: Literal["square", "portrait", "landscape"] = "square",
  ) -> bytes: ...
  ```
- Initial impl: OpenAI Responses API + `gpt-image-1` (or `gpt-image-2` when the provider exposes it). Fallback: emit `ErrorEvent` with clear "image generation not configured" reason.
- Hermes uses `gpt-image-2-medium` quality; we mirror but make it configurable via the persona's metadata field.

**W4.2 — `image_with_refs` tool**
- Schema: `{ prompt, characters: list[label], aspect_ratio? }`. `characters` are persona reference labels, resolved against the **bound** persona (from the channel binding) or an explicit `persona_id`.
- Output: path of the generated image, MIME, dimensions. Saved under `<DATA_DIR>/workspace/generated/<ulid>.png` so the existing `send_attachment` workspace-resolution path picks it up cleanly.

**W4.3 — Tests**
- HTTP mock via `respx` — replay a recorded `responses.create` payload.

### W5 — QZone publishing pipeline

**W5.1 — OneBot HTTP/WS credential bridge**
- New module `corlinman_agent/onebot/client.py`. Async client:
  - `fetch_login_info() -> {qq, nickname, ...}` via OneBot `/get_login_info`
  - `fetch_cookies(domain="user.qzone.qq.com") -> str` via OneBot `/get_cookies`
  - `fetch_csrf_token() -> str` via OneBot `/get_csrf_token`
- Talks to the SAME NapCat the QQ channel already uses (`channels.qq.ws_url`-derived HTTP endpoint).

**W5.2 — `qzone_publish` tool**
- Port `hermes-agent/tools/qzone_tool.py` → `corlinman_agent/qzone/publish.py`.
- Tool schema: `{ text, images: list[path], generate?: image_with_refs_args }`. When `generate` is present, calls `image_with_refs` first and prepends the result to `images`.
- Reads cookies via the new OneBot client; never stores them.
- Returns `{ tid: <feed_id>, qzone_url: ... }`.

**W5.3 — Tests**
- Reuse hermes-agent's `tests/tools/test_qzone_tool.py` fixtures; adapt imports.

### W6 — Scheduler builtin: `qzone.daily_publish`

**W6.1 — Builtin action**
- File: `scheduler/builtins/qzone_daily.py`.
- Args (in scheduler job metadata): `persona_id`, `prompt_template`, `time_of_day` ("09:00"), `qq_account`.
- Runs as a single agent turn under the bound persona's system prompt + a job-specific instruction ("compose today's 说说 and publish via `qzone_publish`"). Captures output to scheduler history.

**W6.2 — Admin UI for daily-说说 jobs**
- File: `ui/app/(admin)/scheduler/qzone/page.tsx` (new). Form: persona dropdown, prompt template, cron expression. Wraps the generic scheduler create-job route with `action_type=qzone.daily_publish`.

**W6.3 — Seed Grantley's daily job**
- `bundled_personas/grantley/daily_job.json` — opt-in via admin button "Enable Grantley daily 说说". Not auto-seeded so a fresh deploy doesn't immediately try to post to QZone.

### W7 — Emoji-aware replies (channel-side)

**W7.1 — System-prompt augmentation**
- When a channel turn fires with a bound persona, the humanlike injector appends:
  ```
  ## Available emoji
  You can send these by calling `send_attachment` with the listed path.
  - happy: <DATA_DIR>/personas/<id>/emoji/<sha256>.png
  - angry: <DATA_DIR>/personas/<id>/emoji/<sha256>.png
  …
  ```
- File: `channels_runtime` (extend the existing humanlike injection point).

**W7.2 — `send_emoji` convenience tool** (optional sugar)
- Schema: `{ label }`. Resolves `(active_persona_id, label) → path` and dispatches `send_attachment` internally.
- Saves the model from copy-pasting the long path. Worth the ~30 lines.

### W8 — `/persona` command — conversational configuration flow

**Why**: The user wants a single keystroke to start a guided persona-setup conversation, instead of relying on the model to spontaneously pick up the persona.* tools when asked. This makes Persona Studio discoverable for non-technical users (just type the command anywhere — channel or web playground).

**W8.1 — Command registration**
- New module `corlinman_channels/commands.py` (channels-side) + `corlinman_server/gateway/commands.py` (web/admin-side). Both register against a small shared registry:
  ```python
  CommandSpec(name="persona", aliases=("/persona", "/角色", "/人格", "配置人格"), summary="启动 persona 配置向导")
  ```
- The matcher fires on **whole-message exact match** (`/persona`) OR a prefix followed by args (`/persona edit grantley`). Partial matches inside longer prose do NOT trigger — keeps the agent free to discuss personas without invoking the wizard accidentally.
- File: `corlinman_channels/router.py` (extend `ChannelRouter.dispatch` to consult the registry before the keyword/@mention gate); `corlinman_server/gateway/routes/chat.py` (apply same matcher to the trailing user message).

**W8.2 — Wizard prelude**
- When a command matches, the inbound user message is replaced (for the agent's view) with a structured prelude:
  ```
  [SYSTEM-INSERTED] The user invoked /persona. Walk them through
  configuring a persona using the persona.* tools and ask_user.
  Required fields: id (slug), display_name, system_prompt voice/style.
  Optional: upload emoji + reference images (instruct them to use the
  /admin/persona UI to drag-drop assets, OR paste image URLs and you
  call persona.attach_asset_from_url).
  When done, summarise what was created and link to /admin/persona.
  ```
- The original literal `/persona` text is preserved on the inbox row for audit.
- File: `corlinman_channels/commands.py:dispatch_command()` + `corlinman_server/gateway/services/chat_bootstrap.py` (web-side substitution).

**W8.3 — Bundled `configure-persona` skill**
- New skill body under `python/packages/corlinman-server/src/corlinman_server/bundled_skills/configure-persona/SKILL.md`. The body contains the step-by-step playbook:
  1. Greet the user, ask whether they want to create a new persona or edit one.
  2. For create: ask for `id` (slug, lowercase), `display_name`, then a 3-5 turn voice/style interview.
  3. Compose a `system_prompt` from the interview answers and confirm with the user before persisting.
  4. Call `persona.create`.
  5. Offer asset upload paths (web UI or URL-paste).
  6. Optionally bind to a channel via the humanlike toggle.
- Auto-seeded into the default profile on first boot (matches the existing `bundled_skills` machinery — see `entrypoint.py`).

**W8.4 — Command help surface**
- A separate `/help` command lists registered commands with summaries. Cheap to add alongside.
- `/persona-list` shortcut → calls `persona.list` and renders the result, no wizard.

**W8.5 — Tests**
- `tests/channels/test_commands.py` — exact match, alias, prefix-with-args, non-match (leaves message untouched).
- `tests/gateway/test_chat_command_substitution.py` — web chat with `/persona` substitutes the prelude.
- `tests/persona/test_wizard_e2e.py` — scripted agent walks through create flow end-to-end against a fake store.

---

## Decisions (locked in 2026-05-26)

1. **Asset caps**: per-asset **8 MiB**, per-persona **200 MiB**. Enforced in `PersonaAssetStore.put`.
2. **Image-gen provider**: **global default** via a new `[image_generation]` config block (`provider="openai"`, `model="gpt-image-1"`); persona row does NOT carry per-persona provider. Future migration if needed.
3. **Humanlike scope**: extend the existing QQ pattern to **Telegram, Discord, Slack, Feishu** in this initiative — admin route `/admin/channels/{channel}/humanlike` becomes generic; UI gets a humanlike switch on each channel page.
4. **Daily-说说 default**: **strict opt-in**. Grantley's `daily_job.json` ships as a bundled template; a `/admin/scheduler/qzone` button "Enable Grantley daily 说说" is the only way it goes live.
5. **owner_user_id**: add the nullable column **now** (zero enforcement, future-proofs auth migration). PersonaStore writes carry `None` until auth lands.

## Wave dependency graph

```
W1 (data) ──┬──> W2 (UI)
            │
            ├──> W3 (agent tools) ──┬──> W8 (/persona command + wizard)
            │                       │
            ├──> W7 (emoji replies) │
            │                       ▼
            └──> W4 (image_with_refs) ──> W5 (qzone_publish) ──> W6 (scheduler)
```

W1 unblocks everything; W2/W3/W7 are parallelizable; W5 needs W4; W6 needs W5; W8 needs W3.

## Estimated effort

| Wave | Files touched | New LOC | Test LOC | Notes |
|---|---|---|---|---|
| W1 | persona/store.py + asset_store.py + routes_admin_a/personas.py + 2 tests | ~450 | ~250 | schema + multipart routes |
| W2 | ui persona/page.tsx + lib/api/personas.ts + 1 test | ~350 | ~120 | drag-drop + previews |
| W3 | corlinman_agent/persona/ + agent_servicer wiring + 1 test | ~250 | ~180 | 7 tools + dispatch |
| W4 | corlinman_agent/image/ + agent_servicer wiring + 1 test | ~200 | ~120 | provider abstraction |
| W5 | corlinman_agent/{onebot,qzone}/ + agent_servicer wiring + 1 test | ~350 | ~200 | port hermes qzone_tool |
| W6 | scheduler/builtins/qzone_daily.py + ui scheduler/qzone/ + bundled seed | ~250 | ~80 | depends on W5 |
| W7 | channels_runtime injector extension + 1 test | ~80 | ~40 | small touch |
| W8 | commands.py + chat_bootstrap substitution + bundled skill + 3 tests | ~250 | ~200 | wires the `/persona` command + wizard |
| **Total** | | **~2180 LOC** | **~1190 LOC** | 1 senior-week, full-attention |

---

## Out-of-plan follow-ups

- Multi-tenant persona ownership (`owner_user_id` enforcement) — separate initiative once auth lands.
- Persona marketplace (browse + install community personas) — mirrors the existing `/admin/skills` hub.
- Per-persona evolution profiles (the agent learns the user's preferred phrasings and updates `system_prompt` over time) — slots into the existing `evolution.sqlite` machinery.
- Voice clone hookup — persona row gains `voice_profile_id` referencing an ElevenLabs / RVC pack; out of scope for v1.6.0.
