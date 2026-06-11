# 格兰 (Grantley) persona migration — verification report

**Date:** 2026-06-04
**Question:** migrate the complete 格兰 persona system from VPS `cornna.qzz.io` into this repo.
**Verdict:** ✅ **Already migrated.** The complete system is in the repo, and the
repo's persona body is *more* complete than the live VPS. No migration work needed.

---

## 1. Source identified

| | |
|---|---|
| URL given | `https://cornna.qzz.io/` |
| DNS | Cloudflare-fronted (`104.21.68.233` / `172.67.199.133`); port 22 not proxied |
| Real origin | `43.133.12.98` (`VM-0-16-debian`, Tencent Cloud) |
| What runs there | **corlinman itself** — `corlinman.service` + `corlinman-agent.service`, code at `/opt/corlinman/repo`, data at `/opt/corlinman/data`, fronted by nginx (`server_name corlinman.cornna.xyz`, uvicorn :8000 / gateway :6099 / agent :6005) |
| Separate hermes/openclaw 格兰 codebase? | **No.** Only `docs/GAP_ANALYSIS_HERMES_OPENCLAW.md` + an unrelated `openclaw` npm install with **none** of the 格兰 lore. |

## 2. Grantley-specific data on the VPS — full inventory

Scanned every `*.sqlite` in `/opt/corlinman/data` plus the filesystem.

| Artifact | VPS location | In repo? | Parity |
|---|---|---|---|
| Persona record `grantley` | `personas.sqlite` | ✅ `persona/default_grantley.{py,md}` + `seed_builtin_personas()` | **repo ⊇ prod** (see §3) |
| `display_name` | `格兰特利·贝尔（Grantley Bell）` | ✅ `DEFAULT_GRANTLEY_DISPLAY_NAME` | **exact match** |
| `short_summary` | `糙汉式温柔 · 嘴硬+行动双轨 · 隐形学霸（蒸馏自 openclaw grantley-perspective）` | ✅ `DEFAULT_GRANTLEY_SUMMARY` | **exact match** |
| `is_builtin` | `1` | ✅ seeded as builtin | match |
| Daily-job template | `bundled_personas/grantley/daily_job.json` | ✅ same path in repo | **byte-identical** |
| Life seeds | bundled (no operator override `data/persona_life/` exists) | ✅ `persona/life_seeds/grantley.yaml` | repo is the source of truth |
| Persona assets (avatar/立绘) | **none for grantley** (4 assets all belong to `vivian`) | n/a | — |
| Grantley memories | **none** — 140 `memory.sqlite` files, 0 grantley-tagged (only chat-derived chunks) | n/a | — |
| Evolution state | **not grantley** — signals/proposals all target `skills/plan.md` Darwin rubric | n/a | — |
| `agent_persona_state` | 14 rows, all `mood=neutral`, `state_json={}`, keyed `model::agent-type::idx` (not `persona_id`) | runtime-only | empty/generic |
| Conversation history | `agent_journal.sqlite` 173 turns / 2522 msgs | ⛔ private chat logs — not a persona definition, not for the repo | — |

## 3. The one place repo and VPS differ: repo is ahead

`default_grantley.md` (repo, 3600 chars) vs production `system_prompt` (3270 chars):
- **only-in-prod lines: 0** (every production line is present in the repo)
- **only-in-repo lines: 14**, all inside one added section:

```
## 此刻的我（实时状态）

下面这些是你**当前的真实状态**，由生活系统持续更新……
- 心情：{{persona.mood}}
- 精神状态：{{persona.fatigue}}
- 最近在聊：{{persona.recent_topics}}
- 现在在做：{{persona.life_activity}}
- 人在哪：{{persona.life_location}}
- 身边有谁：{{persona.life_companions}}
- 状态：{{persona.life_state}}
- 当前剧情线：{{persona.life_story_arc}}
```

This is the `{{persona.*}}` live-state wiring — present in the repo, **missing on the VPS**.

## 4. Persona *subsystem* completeness in the repo

All the machinery "around 格兰" is present (per source map):
- Persona store + schema: `corlinman-server/.../persona/store.py` (`PersonaStore`, `seed_builtin_personas`)
- Persona assets store: `persona/asset_store.py` (`PersonaAssetStore`)
- Life-state tools: `corlinman-agent/.../persona/life.py` (`persona_life_{get,set_state,diary_add,event_seed,get_seeds,set_seeds}`)
- Life seeds: `persona/life_seeds/grantley.yaml`
- QZone tools: `corlinman-agent/.../qzone/` (`qzone_{list_feed,get_post,post_comment,list_friends,publish}`)
- Daily-job template + seeding: `bundled_personas/grantley/daily_job.json`, `seed_bundled_personas()`
- Creation wizard: `bundled_skills/configure-persona/SKILL.md` (8-stage `/persona` flow incl. Stage 4b life seeds + Stage 5 assets)
- First-boot wiring: `gateway/lifecycle/entrypoint.py` (opens stores, seeds builtin + bundled persona)

## 5. Known gaps (NOT migration — separate, optional follow-ups)

1. **No persona export/import tooling.** Personas are seeded one-way from code;
   there is no command to export a live persona back into a repo bundle. (Would
   make future repo↔prod sync reproducible.)
2. **Live-state keying caveat** (pre-existing, from the original life migration):
   life fields key by `persona_id`, but `_context_metadata` does not stamp
   `agent_id`, so `{{persona.life_*}}` placeholders only resolve when the
   deployment runs the persona under `agent_id == persona_id`. The VPS's
   `agent_persona_state` rows confirm this — all keyed by `model::agent-type::idx`,
   none by `grantley`, all empty.
3. **VPS persona body is stale** — it lacks the live-state block (see §3).

## 6. Optional action offered

The only thing that would bring the *VPS* up to the repo's level is backporting
the §3 live-state block into the VPS `personas.sqlite` `grantley.system_prompt`
(one `UPDATE`). Not done — awaiting go-ahead, since it mutates production data.
