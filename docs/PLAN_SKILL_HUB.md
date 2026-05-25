# PLAN — Skill library: wire local UI + ClawHub browse/install

**Initiative**: #60 — Skill library: bundled defaults + openclaw skill hub linkage.
**Target release**: v1.5.0.
**Status**: PLANNING — awaiting user confirm.
**Author**: claude-opus-4-7. **Date**: 2026-05-25.

---

## Context

Memory `[[project-starter-skills-bundle]]` records that on 2026-05-18 we shipped 16 starter skills under `corlinman_server/bundled_skills/` and auto-seed them into `<data_dir>/profiles/default/skills/` on first boot. That part is **done**.

Two gaps remain for the user's "bundled defaults + openclaw hub linkage" ask:

| Gap | Evidence |
|---|---|
| **A. `/admin/skills` page is mocked** | `ui/app/(admin)/skills/page.tsx:38-40` comment: "The gateway endpoint (/admin/skills) isn't wired yet — today we serve from a static mock module" |
| **B. No openclaw / ClawHub integration** | Grep for "clawhub", "openclaw" returns CREDITS.md acknowledgements only; no proxy module, no UI |

ClawHub's API is real and has anonymous read access (confirmed via `docs.openclaw.ai/clawhub` research):
- `GET https://clawhub.ai/api/v1/skills?limit=&cursor=&sort=trending|downloads|stars|updated|createdAt`
- `GET https://clawhub.ai/api/v1/skills/{slug}` + `/{slug}/versions` + `/{slug}/versions/{ver}`
- `GET https://clawhub.ai/api/v1/search?q=`
- `GET https://clawhub.ai/api/v1/download?slug=&version=` → tarball
- Anonymous: 3000/min read, 1200/min download. `X-RateLimit-*` + `Retry-After`.
- Skills are directory bundles: a root `SKILL.md` (YAML frontmatter) + arbitrary supporting files.

## Goal

Operators can browse the openclaw ClawHub from `/admin/skills`, click **Install** on any skill, and have it appear in their profile's `skills/` directory ready for the next agent turn — no terminal, no manual git pull.

## Non-goals

- Publishing skills *to* ClawHub from inside corlinman (writes need GitHub OAuth → `clh_*` tokens; defer).
- A full package manager (no dependency resolution, no auto-update).
- Skill execution sandboxing changes (uses the existing allowed-tools machinery).
- Editing bundled skills via UI (the bundle is read-only; users edit their profile copies).

---

## Architecture

```
┌─────────────── UI ───────────────┐    ┌─────── Gateway ───────┐    ┌── ClawHub ──┐
│  /admin/skills                   │    │  /admin/skills/...    │    │  api.v1     │
│  ┌─────────┐  ┌───────────────┐  │ ←→ │                       │ ←→ │  search     │
│  │ Installed│ │ Browse Hub    │  │    │  installed/  proxy/   │    │  download   │
│  │  list    │ │  search/grid  │  │    │  install/   detail/   │    │             │
│  └─────────┘  └───────────────┘  │    └───────────────────────┘    └─────────────┘
└──────────────────────────────────┘            │
                                       <data_dir>/profiles/<slug>/skills/<name>/
                                       (read by skills.registry on agent boot)
```

Two surfaces:

- **Installed tab** — wraps the existing `/admin/curator/{slug}/skills` endpoint. Replaces today's mock.
- **Browse Hub tab** — new `/admin/skills/hub/*` endpoints that proxy ClawHub (server-side, so we control TTL/cache + the user never sees ClawHub HTML).

---

## Waves

### W1 — Server-side ClawHub client + admin endpoints

**W1.1 — ClawHub client module**
- New file: `python/packages/corlinman-server/src/corlinman_server/system/skill_hub/client.py`
- Async httpx client. Methods: `search(q, limit)`, `list_skills(sort, cursor, limit)`, `get_skill(slug)`, `get_versions(slug)`, `download_tarball(slug, version)` → returns bytes + content-hash.
- Honors `X-RateLimit-Remaining` (per-instance circuit breaker), respects `Retry-After`.
- LRU + TTL cache (60s for list/search, 5min for skill detail).
- Base URL configurable via `CORLINMAN_SKILL_HUB_BASE_URL` (defaults to `https://clawhub.ai/api/v1`).

**W1.2 — Install pipeline**
- New module: `system/skill_hub/installer.py`. Function: `install_skill(profile_slug, hub_slug, version="latest") -> InstallReport`.
- Steps: (1) download tarball, (2) verify content-hash if available, (3) untar to `<data_dir>/profiles/<slug>/skills/<hub-slug>/`, (4) reject path-traversal entries, (5) refuse if target dir already exists unless `force=True`, (6) audit log entry `skill.installed`.
- Uninstall = `rm -rf` the dir (only after a name re-type confirm at UI level).

**W1.3 — Admin endpoints (`routes_admin_b/skills.py`, new)**
- `GET /admin/skills` — wraps the curator list (default profile = "default", optional `?profile=`). Returns rows including origin (`bundled` | `user-edited` | `hub:<slug>@<ver>`).
- `POST /admin/skills/{name}/pin` — proxies to existing curator pin.
- `DELETE /admin/skills/{name}` — uninstall (gated: refuse if origin == "bundled").
- `GET /admin/skills/hub/search?q=` — search proxy.
- `GET /admin/skills/hub/featured?sort=trending` — list proxy.
- `GET /admin/skills/hub/skills/{slug}` — detail proxy.
- `POST /admin/skills/hub/install` — `{ slug, version, profile? }` → install pipeline. Returns request_id; install runs as async task with SSE progress.
- `GET /admin/skills/hub/install/{request_id}/events/live` — SSE.

**W1.4 — Tests**
- `tests/system/skill_hub/test_client.py` — mocked httpx responses for the 5 verbs + rate-limit handling + cache.
- `tests/system/skill_hub/test_installer.py` — happy path + path-traversal rejection + force-overwrite + audit-log entries.
- `tests/gateway/routes_admin_b/test_skills.py` — 8-10 endpoint scenarios.

### W2 — UI

**W2.1 — Wire Installed tab to real data**
- `ui/app/(admin)/skills/page.tsx`: replace mock import with `listProfileSkills` from a new `ui/lib/api.ts` function. Origin badge (bundled / user / hub).
- `ui/components/skills/installed-list.tsx` — extract from page for testability.
- Delete confirm dialog with name re-type; bundled skills get a tooltip "ships with corlinman; edit your profile copy" + disabled state.

**W2.2 — Browse Hub tab**
- `ui/components/skills/hub-tab.tsx` (new). Search input (debounced 300ms), sort dropdown (trending / downloads / stars / updated), grid of `HubSkillCard`.
- `HubSkillCard` shows: emoji, name, description, stars, downloads, version, security scan summary chip.
- Click → `HubSkillDetailDrawer` with versions list, full SKILL.md preview (fetch via `/file?path=SKILL.md`), Install button.
- Install button → POST install, opens progress modal subscribed to SSE; on success toasts + refetches Installed tab.

**W2.3 — i18n + playground hint**
- ~25 new keys: `skills.hub.*`, `skills.install.*`, `skills.origin.*` in en + zh-CN.
- Playground sidebar shows skill count + a "browse hub" CTA when count is low.

### W3 — Polish, docs, release

**W3.1 — E2E + audit log**
- `ui/tests/e2e/skill-hub.spec.ts`: 3 stub scenarios (search → detail → install / delete user skill / delete bundled rejected).
- Audit log integration: `skill.hub.searched`, `skill.installed`, `skill.uninstalled` rows.

**W3.2 — Docs + CHANGELOG**
- New `docs/skill-hub.md` operator deep dive (~1500 words).
- `docs/quickstart.md` paragraph.
- `CHANGELOG.md` v1.5.0 block.
- Update `[[project-starter-skills-bundle]]` memory: bundle + hub now wired.

---

## Resolved decisions (locked in 2026-05-25)

1. **Default sort on Browse tab** → `trending`.
2. **Multi-profile install** → install to the active profile only; no profile picker in v1.5.
3. **Featured / curated subset** → **skip**; search-only.
4. **Offline fallback** → **Banner + Retry button** (no stale local cache fallback).
5. **`web_search.md` naming** → **leave alone**; revisit when something else is touching researcher.yaml.

---

## Test acceptance

- All 1609 server tests + new 30+ continue green.
- `pnpm typecheck` + `pnpm build` clean.
- Manual smoke: search "web", see results, install one, refresh — appears in Installed tab with `hub:web-search@1.0.0` origin badge.
- Audit log shows the install row.

## Dispatch plan

I'll run W1.1+W1.2 in one dev agent, W1.3 in a second, W1.4 in a third, all parallel. After W1 lands, W2.1 + W2.2 + W2.3 parallel. W3 last. Same pattern as the multi-agent v1.4.0 rollout.

---

## Awaiting

User to:
- Confirm the plan
- Answer the 5 open questions (or accept the recommended defaults)
- Authorize parallel agent dispatch
