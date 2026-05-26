---
name: configure-persona
description: Guided wizard for creating or editing a persona — call when the user invokes /persona or asks to configure / create / edit a persona / 角色 / 人格.
metadata:
  openclaw:
    emoji: "🎭"
    requires:
      bins: []
      anyBins: []
      config: []
      env: []
    install: |
      No installation needed. The skill drives the in-process
      `persona.*` tool family + `ask_user`; no external services.
allowed-tools:
  - persona_list
  - persona_get
  - persona_create
  - persona_update
  - persona_delete
  - persona_list_assets
  - persona_attach_asset_from_url
  - ask_user
---
# Configure Persona

Step-by-step wizard for creating a new persona or editing an existing one.
Use this skill whenever:

- The user invokes `/persona` (or any localised alias: `/角色`, `/人格`,
  `配置人格`, `配置角色`).
- The user asks to "create / edit / configure a persona / 角色 / 人格"
  in natural language.
- The system inserts a `[SYSTEM-INSERTED] The user invoked the /persona
  command. …` prelude — that's the channel router rewriting the literal
  command into this skill's invocation contract.

## Tools you will use

- `persona_list` — read the registry (id, display_name, summary).
- `persona_get` — fetch a full persona body (system_prompt + metadata).
- `persona_create` — persist a new persona.
- `persona_update` — patch an existing persona's fields.
- `persona_list_assets` — enumerate emoji + reference images attached to
  a persona.
- `persona_attach_asset_from_url` — pull an HTTP(S) image into the
  persona's asset bag.
- `ask_user` — the canned-question UX for any branch point that needs
  user input (interview questions, confirmations, mode selection).

## Flow

### Step 1 — Greet + branch

Call `ask_user` with a short greeting and a two-option question:

> 想创建新的 persona 还是编辑已有的？(create / edit)

If the user picks `edit`, call `persona_list` first and ask which `id`
they want; then jump to **Step 6 (edit)**.

If the user picks `create`, continue to **Step 2**.

### Step 2 — Identity

Ask two `ask_user` questions in sequence:

1. `id`（小写，1-64 字符，仅 `[a-z0-9_-]`，例如 `grantley` 或
   `cyber_oracle`）
2. `display_name`（中文 / 英文均可，会在 UI 与对外消息中展示）

Validate the slug client-side: if it contains an invalid character,
push back with a corrected suggestion before continuing.

### Step 3 — Voice / style interview

Run a **3-5 turn** interview, one `ask_user` per turn. Suggested
prompts (adapt to the persona type):

- 一句话定义这个角色的身份与立场？
- 他/她的语气是怎样的？(温柔 / 毒舌 / 严肃 / 俏皮 / 学术…)
- 他/她有哪些常用口头禅或标志性表达？
- 在什么话题上特别有主见？应该规避什么？
- 回应长度偏好？(简短 / 中等 / 长篇)

Buffer the answers; do not call any persona tool yet.

### Step 4 — Compose + confirm

Draft a `system_prompt` from the interview answers (keep it tight —
under 600 字 unless the user asked for a richer brief). Present the
draft via `ask_user` along with the `id` + `display_name`:

> 我准备这样建：
> - id: <slug>
> - display_name: <name>
> - system_prompt:
> <draft>
>
> 确认创建？(yes / 修改)

If the user wants changes, loop back to Step 3 for the specific field
they called out (do NOT re-ask everything).

### Step 5 — Persist

After confirmation, call `persona_create` with `{id, display_name,
system_prompt, short_summary}` where `short_summary` is a ≤120 字
sentence derived from the system_prompt.

Surface any error (slug collision, validation) back to the user via
plain text and offer a one-shot retry path.

### Step 6 — Edit (alternate branch)

After `persona_get` returns the current row, ask via `ask_user` which
field to patch (`display_name`, `system_prompt`, `short_summary`,
`is_active`). Then collect the new value, present a diff-style preview
(`old → new`), and on confirmation call `persona_update`.

### Step 7 — Assets

After create/update, ask:

> 要现在添加 emoji 或参考图吗？
> 1. 用 /admin/persona 拖拽上传 (推荐)
> 2. 粘贴图片 URL 让我帮你拉取
> 3. 跳过

For option 2, loop on `ask_user` collecting `{label, url}` pairs and
call `persona_attach_asset_from_url` per entry. Always echo the
resulting asset path back to the user.

### Step 8 — Wrap-up

Summarise what was created / changed in 2-3 lines and link to
`/admin/persona` for further visual tweaks (avatar, voice profile,
channel bindings).

## Anti-patterns

- Do NOT call `persona_create` before the user confirms the
  `system_prompt` draft in Step 4. The persona row is the persistent
  unit; rolling back means a `persona_delete` round-trip.
- Do NOT skip `ask_user` and infer answers from prior conversation
  context — the wizard's contract is explicit confirmation.
- Do NOT auto-upload images without an explicit URL from the user;
  scraping arbitrary web pages for "matching" art violates the asset
  contract.
- Do NOT silently truncate the interview if the user gives short
  answers; ask one clarifying follow-up per terse answer before moving
  on.
