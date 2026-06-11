# 格兰 persona system — integration/migration plan

**Date:** 2026-06-04 · **Basis:** `audit_findings.json` (9-dimension deep audit, 75 gaps: 22 high / 23 med / 30 low)

## Reframe (what's actually wrong)

The grantley **persona record + body** is fully migrated and the **system-prompt injection**
works end-to-end on 5 channels (QQ/OneBot, Telegram, Discord, Slack, Feishu). What's broken
is everything that makes him *alive over time*, plus a few wiring bugs and unmigrated surfaces.
The old "`agent_id` keying caveat" is **OUTDATED** — `_context_metadata` (agent_servicer.py:4321-4325)
*does* stamp `agent_id` from `persona_id`. The real break is the **missing state-row seed**.

## Root causes (the 22 high gaps dedupe to these)

| # | Root cause | Cascades to | Fix locus | Risk |
|---|---|---|---|---|
| R1 | **No `agent_persona_state` row is seeded for the built-in persona at boot** → `store.get('grantley')` is None → all `{{persona.mood/fatigue/recent_topics/life_*}}` render `""`, qzone life block empty | H1,H3,H4,H5,H9 + M10 | `c2_wiring.py:~230` / `entrypoint.py:~329` add `seed_builtin_persona_states` (use `corlinman_persona.seeder.seed_from_card`) | low |
| R2 | **Nothing drives life/mood/fatigue autonomously** — `apply_decay`/`decay-once` correct but unscheduled; life only changes if the model calls tools | H2,H6,H7,H8,H10,H22 + M3,M6 | new `scheduler/builtins/persona_decay.py` + `registry.py` + default-job registration (mirror `evolution_darwin_curate`) | low |
| R3 | **`image_with_refs` passes the wrong store** (life-state store instead of persona-body store) → 立绘 reference-image generation broken for every persona | H11,H12,H13 | `agent_servicer.py:2946` `_get_persona_state_store()` → `_get_persona_store()` (one line) | trivial |
| R4 | **qzone daily-publish never sets `persona_id`** → scheduler turns have no persona binding (no life block, no 立绘) | M8,M9,M11 + L18 | `qzone_daily.py:~765` `_build_internal_chat_request(..., persona_id=persona_id)` (one kwarg) | trivial |
| R5 | **Web `/v1/chat/completions` never injects a persona** → 格兰 not alive on the web chat surface | H19 | `chat.py:_build_internal_request`/`handle_chat` add a `[web.humanlike]` binding + inject | med |
| R6 | **2 of 7 channels (qq_official, wechat_official) can't bind a persona** | M20 | add humanlike fields + `inject_persona_if_enabled` to both handlers in `channels/service.py` | med |
| R7 | **No admin UI/API for life-state** (mood/fatigue/diary/life-seeds invisible; qzone page unlinked; reset/preview/decay buttons disabled) | H20,H21 + M21,M22,M23,L26,L28,L30 | new `/admin/personas/{id}/life-state|diary|life-seeds|decay|reset|preview` routes + UI panels + sidebar link + i18n | med |
| R8 | **Self-evolution loop only half-wired and never touches the persona body** (observer not started, engine/monitor unscheduled, apply is bookkeeping-only, shadow-tester has no console script, persona is not an evolution target, nuwa doc-only) | H14-H18 + M13-M18 | large multi-package effort | **high** |

**Data gaps (no code, need assets/decisions):** grantley has zero emoji/reference assets → emoji
block + 立绘 silently absent (L16,L17,M12); `lycaon`/`vivian` exist only on prod, not bundled (L5);
`seed_builtin_personas` is idempotent so prod's body never gets the repo's newer live-state block (M1);
original undistilled openclaw body not archived (L4).

## Execution waves

### Wave 1 — Persona liveness core + wiring bugs  ⟵ EXECUTE NOW (safe, high-leverage, in-scope)
- **A1 (R1):** boot-seed `agent_persona_state` row for built-in persona(s). Files: `gateway/lifecycle/c2_wiring.py`, `entrypoint.py`; reuse `corlinman_persona.seeder.seed_from_card`. + test.
- **A2 (R2):** `persona_decay` scheduler builtin (in-process, hourly) + register default job. Files: new `scheduler/builtins/persona_decay.py`, `scheduler/builtins/registry.py`, `scheduler_integration.py`. + test.
- **B1 (R3):** one-line store fix at `agent_servicer.py:2946`.
- **B2 (R4):** one-kwarg `persona_id` at `qzone_daily.py` `_build_internal_chat_request`.
- **Validation:** `uv run pytest` (persona/scheduler/qzone packages), `ruff`, `mypy`, `import-linter`.

*Wave-1 file ownership is partitioned so the parallel agents never touch the same file (A1=c2_wiring/entrypoint, A2=scheduler/*, B=agent_servicer+qzone_daily).* 

### Wave 2 — Surface persona everywhere (opt-in)
- **C1 (R5)** web /chat persona injection · **C2 (R6)** qq_official + wechat_official binding · **E3 (M1)** body-update migration so prod adopts the live-state block · **R7 partial** life-state read APIs.

### Wave 3 — Admin UX + autonomy depth (opt-in)
- **R7 full** life-state/diary/life-seeds viewers+editors, qzone sidebar link, reset/preview/decay buttons + i18n · **A3** autonomous life-advance daily builtin + post-turn topic/fatigue hook · **R4-asset** pass `asset_store` so daily 立绘 works.

### Wave 4 — Data & tooling (opt-in, some need assets)
- **E1** bundle default grantley emoji/reference 立绘 (needs images) · **E2** persona-export CLI (capture lycaon/vivian) · **E4** archive original openclaw body as reference doc.

### Deferred — Self-evolution (R8) — SEPARATE INITIATIVE, high-risk
Completing the darwin/nuwa observe→propose→shadow→apply→rollback loop and making the **persona body**
an evolution target (auto-rewriting `system_prompt`) is a large, high-risk effort of its own. Recommend
a dedicated design pass + explicit opt-in flag, NOT folded into the persona-migration work.
