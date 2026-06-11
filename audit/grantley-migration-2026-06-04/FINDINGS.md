# 格兰 (Grantley) persona migration — recon findings (2026-06-04)

Source server: `https://cornna.qzz.io/` → Cloudflare → origin `43.133.12.98`
(`VM-0-16-debian`, runs `corlinman.service` + `corlinman-agent.service` at
`/opt/corlinman/repo`, data at `/opt/corlinman/data`). SSH via root.

## What the VPS actually has that is grantley-specific

| Artifact | Where on VPS | Already in repo? |
|---|---|---|
| Persona record `grantley` (display_name, summary, system_prompt 3270 chars) | `data/personas.sqlite` | **Yes — repo is a SUPERSET (3600 chars)** |
| `daily_job.json` (opt-in QZone daily publish, enabled=false) | `data/bundled_personas/grantley/daily_job.json` | **Yes — byte-identical** |
| Life seeds | bundled (no operator override `persona_life/` on VPS) | **Yes — `life_seeds/grantley.yaml`** |
| Persona assets (avatar/立绘) | none for grantley (4 assets all belong to `vivian`) | n/a |
| Grantley memories / evolution state | none grantley-specific (140 memory files: 0 match; evolution = skill `plan.md` rubric only) | n/a |
| Conversation history | `agent_journal.sqlite` (173 turns / 2522 msgs) | private chat logs — NOT a persona definition |

## Key diff: repo ⊃ production

`default_grantley.md` (repo) == production `system_prompt` **except** the repo
adds a whole section the VPS lacks:

```
## 此刻的我（实时状态）
- 心情：{{persona.mood}}
- 精神状态：{{persona.fatigue}}
- 最近在聊：{{persona.recent_topics}}
- 现在在做：{{persona.life_activity}}
- 人在哪：{{persona.life_location}}
- 身边有谁：{{persona.life_companions}}
- 状态：{{persona.life_state}}
- 当前剧情线：{{persona.life_story_arc}}
```

So the live-state placeholder wiring exists in the repo, not on the VPS.

## No richer original source on this VPS

The persona body credits "蒸馏自 openclaw grantley-perspective SKILL.md".
That original SKILL.md is **not present** anywhere on `43.133.12.98`
(searched `/usr/lib/node_modules/openclaw`, `/root/.openclaw`, `/opt`, `/tmp`,
and all data DBs for `亚戈/奥斯卡/铁三角/虎兽人/grantley-perspective`). The only
hits are the persona record + chat logs in the runtime DBs.

## Conclusion

The complete grantley persona **system** (record + body + life seeds + life
tools + qzone tools + daily-job template + creation wizard) is **already in
the local repo**, and the repo's persona body is *more* complete than the live
VPS. There is no un-migrated grantley persona system on this VPS to bring over.

Real remaining options (require user decision):
1. Already done — optionally backport the live-state block TO the VPS.
2. Import the original, un-distilled `openclaw grantley-perspective SKILL.md`
   (richer than the distillation) — but that source is NOT on this VPS; needs
   a pointer (GitHub / other machine).
3. Pull production *runtime* data (chat-derived memory) into a local instance —
   private data, mostly empty for grantley.
