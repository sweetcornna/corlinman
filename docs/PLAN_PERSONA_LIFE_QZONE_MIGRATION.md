# PLAN — Migrate hermes "Grantley (格兰)" systems → corlinman persona system

Source: `project/hermes-agent` (untracked working-tree additions).
Target: `project/corlinman` (Python monorepo), adapted to corlinman's
persona / 人格系统.

## What "格兰" is in hermes

"格兰" = **Grantley (格兰特利·贝尔)**, the QQ-platform Hermes persona. The
user's new, *uncommitted* systems are two tool files:

| hermes file | what it is |
|---|---|
| `tools/grantly_life_tool.py` | Life-state + diary + random event-seed (4 tools), hardcoded to Grantley. Self-owned JSON state file. |
| `tools/qzone_comment_tool.py` | QQ空间 read + comment (4 tools): list 好友动态 feed, get one post, post/reply comment, list friends. Sync `urllib`. |

Out of scope (not a Grantley system): the `gateway/run.py`
`_normalize_empty_agent_response` silent-drop-after-`/stop` hint (#31884)
and its test — a generic gateway hardening unrelated to Grantley. Noted,
not migrated.

## corlinman target facts (verified)

- **Tool contract**: name-constant + OpenAI `{"type":"function",…}` schema
  fn + async `dispatch_*(*, args_json, …deps) -> str` JSON envelope that
  never raises. Wired in `agent_servicer.py` via `BUILTIN_TOOLS`,
  `_builtin_tool_schemas()`, and the `_dispatch_builtin` switch. Template:
  the existing `qzone_publish` / `persona.*` families.
- **Active persona** at dispatch = `start.extra["persona_id"]` (already
  used by `qzone_publish` + `image_with_refs`). Built-in default persona
  id = `grantley`.
- **Two persona layers**: (1) `corlinman-server` `PersonaStore` =
  system_prompt registry (per `persona_id`); (2) **`corlinman-persona`
  `PersonaStore`** = runtime state `agent_persona_state(tenant_id,
  agent_id)` with `mood`, `fatigue`, `recent_topics`, free-form
  `state_json`. corlinman-agent **already depends on corlinman-persona**.
- **QZone/OneBot**: `corlinman_agent.qzone.publish` already exports
  `_compute_gtk`, `_extract_cookie_value`, `_DESKTOP_UA`, `_QZONE_TIMEOUT`,
  `_QZONE_COOKIE_DOMAIN`. `OneBotClient` (async httpx) has
  `fetch_login_info()`, `fetch_cookies(domain)`, `fetch_csrf_token()`.
  Everything is **async httpx** (hermes was sync urllib → must port).
- **Data dir**: `_resolve_data_dir()` → `$CORLINMAN_DATA_DIR` else
  `~/.corlinman`. Persona-state DB = `<DATA_DIR>/agent_state.sqlite`.
- **Layering** (`.importlinter`): `corlinman_agent` must NOT import
  `corlinman_server`. Both new tool families stay inside corlinman-agent
  (life needs only a `persona_id` string + the corlinman-persona store;
  qzone-comment needs only `OneBotClient` + helpers from `qzone.publish`).

## Design

### 1. Persona-life tools → `corlinman_agent/persona/life.py`

Persona-agnostic rename + native-state integration:

| new tool | was | notes |
|---|---|---|
| `persona_life_get` | `grantly_get_life` | reads life blob + diary tail |
| `persona_life_set_state` | `grantly_set_state` | archives prev → history; mirrors `mood`→native column; pushes activity→`recent_topics` |
| `persona_life_diary_add` | `grantly_diary_add` | appends private diary entry (cap 200) |
| `persona_life_event_seed` | `grantly_event_seed` | random themed inspiration draw |

- **Storage** = `corlinman_persona.PersonaStore` (`agent_state.sqlite`),
  keyed `(tenant_id="default", agent_id=<bound persona_id>)`. Life lives
  in `state_json["life"]` (`current` + `history`), diary in
  `state_json["diary"]`. Read-merge-upsert preserves `fatigue` and other
  fields. When no persona is bound → key `__corlinman_default__`.
- **Seed library** per persona: operator override
  `<DATA_DIR>/persona_life/<persona_id>.events.yaml` → bundled pack
  `persona/life_seeds/<persona_id>.yaml` (ship `grantley.yaml` carrying
  the original 骑士学院 lore) → generic neutral `_GENERIC_SEEDS` fallback.
- Dispatch deps injected: `persona_id`, `state_store` (corlinman-persona
  PersonaStore), `data_dir` (for the events override). All test-injectable.

### 2. QZone comment tools → `corlinman_agent/qzone/comment.py`

Async-httpx port; "Grantly" wording → "the bound account / you".

| tool | purpose |
|---|---|
| `qzone_list_feed` | read 好友动态 timeline (feeds3_html_more), comments inline |
| `qzone_get_post` | one post by tid from the timeline |
| `qzone_post_comment` | top-level comment / @reply (emotion_cgi_re_feeds) |
| `qzone_list_friends` | friend list via OneBot `get_friend_list` |

- Reuse `qzone.publish` auth helpers; uin+cookie via `OneBotClient`.
- Add `OneBotClient.fetch_friend_list()` (`get_friend_list` action).
- Dispatch mirrors `dispatch_qzone_publish`: optional `onebot_client` /
  `http_transport` test seams; constructs `OneBotClient()` from env
  otherwise.

### 3. Wiring + tests

- `onebot/client.py`: add `fetch_friend_list()`.
- `qzone/__init__.py`, `persona/__init__.py`: export new surfaces.
- `agent_servicer.py`: import; extend `BUILTIN_TOOLS` +
  `_builtin_tool_schemas()`; add `_get_persona_state_store()` lazy handle;
  add `_dispatch_builtin` branches (life → persona_id+store+data_dir;
  qzone-comment → env OneBotClient like publish).
- Tests: `test_persona_life.py` (state IO, history archive, diary cap,
  event-seed override + isolation, mood mirror) and `test_qzone_comment.py`
  (feeds3 parse, comment form + @reply, friends via MockTransport, error
  envelopes).

### Verify
`ruff` + `mypy` (changed files) + `pytest` (new + adjacent suites) +
import-linter (layering unchanged). Adversarial multi-lens review before
finalize.

## Post-review hardening (applied)

Adversarial 4-lens review → 3 confirmed fixes:

1. **[high] WAL/busy_timeout** — `corlinman_persona.store.PersonaStore._open`
   now sets `journal_mode=WAL` + `synchronous=NORMAL` + `busy_timeout=5000`
   (matches every other corlinman sqlite store). The agent-side life tools,
   the decay job, and the placeholder resolver each open their own handle to
   `agent_state.sqlite`; without WAL a concurrent write tripped "database is
   locked".
2. **[medium] mood mirror** — `set_state` now mirrors the native `mood`
   column only when the model **explicitly** provides a mood (an omitted
   mood preserves the evolution-managed value; an explicit `""` clears it),
   so the life mood and `{{persona.mood}}` never silently drift.
3. **[medium] path traversal** — the `_valid_persona_slug` guard is applied
   to BOTH the bundled-pack and operator-override seed lookups.

## Persona-system integration (the "适配现在的 persona 系统" requirement)

The life is wired into the **current** persona system's prompt layer, not
just tool-readable. `set_state` mirrors `current` onto flat `state_json`
keys so the existing `corlinman_persona.PersonaResolver` surfaces them:

| placeholder | source |
|---|---|
| `{{persona.life_state}}` / `life_location` / `life_activity` / `life_companions` / `life_story_arc` | flat `state_json["life_*"]` |
| `{{persona.mood}}` | native `mood` column (explicit-mood mirror) |
| `{{persona.recent_topics}}` | `recent_topics` (activity push) |

**Keying caveat (open):** life-state is keyed by the bound **persona_id**
(reliably available at `start.extra["persona_id"]`), used as the
`agent_id` of the `agent_persona_state` row. The `{{persona.*}}` resolver
keys by `ctx.metadata["agent_id"]` (not yet stamped by `_context_metadata`
— a partial W5 wiring). So the placeholder surfacing lights up when the
humanlike persona runs under `agent_id == persona_id` (the natural
single-persona convention) or once the resolver wiring keys by persona_id.
If a deployment runs one agent across many personas and wants life keyed by
the runtime `agent_id` instead, flip the key in the servicer dispatch
branch (resolve `start.extra["agent_id"]` first, then persona_id).

## Persona-creation flow integration (the life lore)

Goal: let the `/persona` wizard give a new persona a **life lore** (the
event-seed library `persona_life_event_seed` draws from), via either of two
modes — agent auto-researches online, or agent fills from user materials.

Two new **authoring** tools (take an explicit `persona_id` arg, unlike the
runtime life tools which use the bound persona):

- `persona_life_set_seeds(persona_id, seeds, merge?)` — writes the
  operator-override `<DATA_DIR>/persona_life/<id>.events.yaml` (highest
  precedence). Slug-guarded, item/category-capped, atomic write,
  `merge` layers over the existing file. Called by the wizard right after
  `persona_create`.
- `persona_life_get_seeds(persona_id)` — returns the effective library
  (generic ← bundled ← override) + `has_override`; for the edit flow.

Both wired into `agent_servicer.py` (the `PERSONA_LIFE_TOOLS` branch reads
`persona_id` from the tool args, before the bound-persona resolution).

`bundled_skills/configure-persona/SKILL.md` extended:
- `allowed-tools` += the two tools.
- New **Stage 4b — 人生设定 / 事件种子（可选）** offering the explicit choice
  `自动调研 / 我来提供资料 / 跳过` (maps to the public-figure web-research
  branch vs the self-created materials branch), distilling into standard
  seed categories (`companion` / `mission_scenario` / `travel_destination` /
  `academy_scene` / `tension` / …).
- Stage 6 "落库后" writes the buffer via `persona_life_set_seeds`.
- Edit branch gains a "生活设定(事件种子)" option (`get_seeds` → `set_seeds`).
