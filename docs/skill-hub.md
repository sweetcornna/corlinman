# Skill library + ClawHub

corlinman ships with a curated starter pack of procedural-knowledge skills
on day one, and operators can extend that set from `/admin/skills` by
browsing the [openclaw ClawHub](https://clawhub.ai) — a community
registry of `SKILL.md` bundles. Installs land directly in the active
profile's `skills/` directory and are picked up by the agent's
`SkillRegistry` on the next refresh. No terminal, no `git pull`, no
manual unpacking.

This document is the operator-facing deep dive. The implementation plan
lives at [`docs/PLAN_SKILL_HUB.md`](PLAN_SKILL_HUB.md). For how skills
plug into the wider agent runtime — registry, hot reload, and the
`SOUL.md` injection point — see [Architecture](architecture.md).

---

## What is it

Two skill tiers, both backed by the same registry + `skills/` directory:

- **Bundled defaults** — `SKILL.md` files baked into the
  `corlinman-server` wheel under `bundled_skills/`. On first boot the
  server copies them into `<data_dir>/profiles/default/skills/` so a
  brand-new install has useful skills the moment it's reachable. The
  bundle is read-only from the UI's perspective: the Delete button on
  `/admin/skills` is disabled with a tooltip ("ships with corlinman;
  edit your profile copy"), and the gateway refuses `DELETE` requests
  on bundled rows with `409 bundled_protected`. See
  [[project-starter-skills-bundle]] for the memory record covering the
  seed pipeline.
- **Hub installs** — anything an operator fetches from ClawHub through
  `/admin/skills` lands as a directory under the same `skills/` root.
  These rows carry an `origin` tag of `hub:<slug>@<version>` and a
  `.openclaw-meta.json` sidecar file the uninstall pipeline uses to
  authorise an `rm -rf`.

Both tiers share the registry; from the agent's perspective there's
nothing distinguishing a bundled skill from a hub-installed one. The
split exists only at the operator surface so the UI can gate
destructive actions.

---

## Layout

Skills live under each profile, not at the gateway root. For the
default profile:

```
<data_dir>/profiles/default/skills/
├── memory/                          # bundled — no sidecar
│   └── SKILL.md
├── deep-research/                   # bundled
│   └── SKILL.md
├── ...
├── web-search/                      # hub-installed
│   ├── SKILL.md
│   ├── prompts/
│   │   └── refine.md
│   └── .openclaw-meta.json          # sidecar — see below
└── my-custom-skill/                 # operator-authored
    └── SKILL.md
```

The `.openclaw-meta.json` sidecar is written by the installer the
moment extraction succeeds. Its shape:

```json
{
  "slug": "web-search",
  "version": "1.0.0",
  "installed_at": "2026-05-25T14:32:08Z",
  "source": "clawhub",
  "content_hash": "sha256:1d4e7a..."
}
```

The uninstall pipeline refuses to delete any skill directory that
lacks this sidecar — that's the second-line defence against an
operator (or compromised admin session) blowing away a bundled or
operator-authored skill via the UI. The Delete button is also gated
client-side, but the sidecar check on the server is the authoritative
guard. See [Safety](#safety) below.

---

## Installing from the hub

### Admin UI walkthrough

1. Visit `/admin/skills`. The default tab is **Installed** — the live
   list of every skill in the active profile, with origin badges
   (`bundled` / `user` / `hub`) and pin / delete affordances per row.
2. Switch to the **Browse Hub** tab. A search box at the top defaults
   to the **Trending** sort; the grid shows one card per skill with
   emoji, name, stars, downloads, and the latest version.
3. Type a few characters (search is debounced at 300ms). The grid
   re-queries `/admin/skills/hub/search?q=…`.
4. Click any card to open the detail drawer. The drawer fetches the
   detail proxy (`/admin/skills/hub/skills/{slug}`) and renders the
   security-scan chip, the version list, the homepage link (if any),
   and the SKILL.md README excerpt.
5. Click **Install**. A progress modal opens with a three-stage bar:
   `download.started → extract.started → installed`. The bar is
   driven off the SSE stream `/admin/skills/hub/install/{request_id}
   /events/live` (one `event: phase` frame per state change).
6. On success, the modal turns its primary button into "Done", a toast
   confirms the install, and the Installed tab automatically refetches
   so the new row appears with the `hub:<slug>@<version>` badge.

### curl recipe

The same flow from the host shell, useful for batch onboarding or for
operators running corlinman behind an SSO proxy that blocks the
EventSource handshake:

```bash
# 1. Search.
curl -b admin-cookie 'http://localhost:6005/admin/skills/hub/search?q=web'

# 2. Detail.
curl -b admin-cookie \
  'http://localhost:6005/admin/skills/hub/skills/web-search'

# 3. Kick off the install. Returns 202 + a request_id.
REQ=$(curl -b admin-cookie -s -X POST \
  http://localhost:6005/admin/skills/hub/install \
  -H 'content-type: application/json' \
  -d '{"slug":"web-search"}' | jq -r .request_id)

# 4. Poll the status snapshot (or open the SSE stream in another tab).
curl -b admin-cookie "http://localhost:6005/admin/skills/hub/install/$REQ"
```

The install runs detached on the gateway side — the POST returns the
moment the request is queued, then the SSE / poll endpoint reports the
state machine.

### Audit log entries

Every install / uninstall lands two lines in
`<data_dir>/system-audit.log`:

```
skill.installed     actor=ops slug=web-search version=1.0.0 files_written=4
skill.uninstalled   actor=ops slug=web-search
```

The same rows surface under `/admin/system` next to the one-click
upgrade entries; see [system-updates](system-updates.md) for the audit
reader UI.

---

## ClawHub API

The gateway proxies a small slice of ClawHub's anonymous read surface.
The full client lives at `system/skill_hub/client.py`.

| Method | Path                                              | Behaviour                                  |
|--------|---------------------------------------------------|--------------------------------------------|
| GET    | `/admin/skills/hub/search?q=`                     | Free-text search; `{rows, offline}`.       |
| GET    | `/admin/skills/hub/featured?sort=`                | List by `trending / downloads / stars / updated`. |
| GET    | `/admin/skills/hub/skills/{slug}`                 | Detail with versions + scan + README.      |
| GET    | `/admin/skills/hub/skills/{slug}/file?path=…`     | Raw file fetch (used by README preview).   |
| POST   | `/admin/skills/hub/install`                       | Returns `202 {request_id}`.                |
| GET    | `/admin/skills/hub/install/{id}`                  | Read-once status snapshot.                 |
| GET    | `/admin/skills/hub/install/{id}/events/live`      | SSE; `event: phase\ndata: <status>\n\n`.   |

ClawHub's anonymous read limits are 3000 requests/min for search /
detail and 1200/min for tarball downloads. The proxy honours the
upstream `X-RateLimit-Remaining` and `Retry-After` headers — when
either signals exhaustion the gateway falls back to its local 60-second
LRU+TTL cache (5 minutes for detail) so a hot search doesn't quietly
fail. Override the upstream base URL with `CORLINMAN_SKILL_HUB_BASE_URL`
for air-gapped deploys pointing at an internal mirror.

### Offline behaviour

When the proxy can't reach ClawHub (DNS failure, 5xx, or the upstream
returns a quota-exhausted response with no cache fallback), the search
and featured endpoints respond with:

```json
{ "rows": [], "offline": true, "next_cursor": null }
```

The UI's `<HubTab>` watches for `offline === true` and renders the
banner + Retry button described in [Troubleshooting](#troubleshooting).
This is the locked-in design (see
[`docs/PLAN_SKILL_HUB.md`](PLAN_SKILL_HUB.md) §"Resolved decisions"):
no stale-local-cache fallback; the operator gets an honest "couldn't
reach the hub" state and a one-click Retry.

---

## Safety

The hub fetches arbitrary tarballs from an external service, extracts
them onto disk, and the agent then sources their `SKILL.md`. That's a
non-trivial surface and the installer pipeline takes it seriously.

| Guard                       | Where                                                 | Behaviour                                                                |
|-----------------------------|-------------------------------------------------------|--------------------------------------------------------------------------|
| Slug regex                  | `installer.py:_validate_name`                            | `^[a-z][a-z0-9-]{1,63}$`. Rejects path-traversal characters up-front.    |
| Path-traversal              | `installer.py:_safe_extract`                   | Refuses any member with `..` segments, absolute paths, or symlinks.      |
| 25 MiB total cap            | `installer.py:_MAX_TOTAL_UNCOMPRESSED_BYTES`          | Decompressed tarball size; protects against zip-bombs.                   |
| 10 MiB per-file cap         | `installer.py:_MAX_PER_FILE_BYTES`                    | A single huge member fails the install before extraction touches disk.   |
| Sidecar-gated uninstall     | `installer.py:uninstall_skill`                        | Refuses any directory missing `.openclaw-meta.json`.                     |
| Server-side bundled gate    | `gateway/routes_admin_b/marketplace/skills.py`                    | Returns `409 bundled_protected` on `DELETE /admin/skills/{bundled}`.     |
| Content-hash record         | Sidecar                                               | Tarball's `sha256` is recorded for forensic comparison.                  |
| Audit log                   | `system/audit.py`                                 | Every install + uninstall lands an entry with actor + slug + version.    |

The two size caps fire before any bytes leave the streaming
extractor, so a malicious 4-GiB tarball burns at most ~25 MiB of
gateway memory before the install is aborted with `UnsafeTarballError`.
The path-traversal check is per-member and walks the resolved path
against the profile root — a member named `../../etc/passwd` is
caught regardless of how the upstream encoded it.

The sidecar trick is the linchpin of the uninstall gate. Bundled
skills are seeded by `corlinman-server` itself and never get a
sidecar; ClawHub installs always write one. The uninstall pipeline
opens the directory, looks for the sidecar, and refuses if it's
missing — so even a UI bypass (`curl -X DELETE …`) can't `rm -rf`
something that didn't come from the hub.

---

## Bundled starter skills

The starter skills are versioned with the wheel and seeded into the
default profile on first boot. The exact count follows the installed
wheel; the core bundle includes:

| Skill                            | What it's for                                                     |
|----------------------------------|-------------------------------------------------------------------|
| `brainstorming`                  | Front-load creative-work flows with intent / requirements / design. |
| `code_review`                    | Review pending changes at a given effort level.                   |
| `deep-research`                  | Multi-pass investigation with citation discipline.                |
| `document-generator`             | Generate clean CJK-safe Markdown-to-PDF reports.                  |
| `executing-plans`                | Drive a written implementation plan through review checkpoints.   |
| `git-worktrees`                  | Isolate feature work in a git worktree before editing.            |
| `memory`                         | Persistent `MEMORY.md` updates the agent reads each turn.         |
| `note-taking`                    | Capture intermediate findings in a structured journal.            |
| `plan`                           | Surface "what's planned" before opening files.                    |
| `receiving-feedback`             | Process review feedback before implementing changes.              |
| `requesting-code-review`         | Pre-flight verification before asking for review.                 |
| `subagent-driven-development`    | Coordinate sibling agents through `subagent.spawn_many`.          |
| `systematic-debugging`           | Don't propose fixes until root-cause is identified.               |
| `test-driven-development`        | Write the failing test first; never gold-plate.                   |
| `verification-before-completion` | Run the verification commands before claiming "done".             |
| `visual-output-quality`          | Check PDFs/images/slides for readability, clipping, and overlap.  |
| `web_search`                     | Browse + cite web sources via the gateway's fetch tools.          |
| `writing-plans`                  | Produce a spec before touching code on multi-step tasks.          |

Adding to the bundle (for upstream contributions) lives at
`python/packages/corlinman-server/src/corlinman_server/bundled_skills/`
— drop a new `SKILL.md` in there, ship a PR. Adding to an operator's
*own* deploy is the simpler path: just write the skill under
`<data_dir>/profiles/<slug>/skills/<name>/SKILL.md`, refresh the
registry from the agents page (the curator does it automatically every
30s — see [Architecture](architecture.md) §SkillRegistry), and the
agent can call it on the next turn.

---

## Troubleshooting

### `HubUnavailableError` / hub tab shows "Skill hub unreachable"

The proxy can't reach ClawHub. Causes:

- Egress firewall blocks `clawhub.ai`. Whitelist the host or set
  `CORLINMAN_SKILL_HUB_BASE_URL` to an internal mirror.
- Upstream is rate-limited and the local cache is cold. The banner's
  Retry button forces a refetch; if that still fails, wait for the
  60-second TTL window to refresh.
- DNS resolution failure inside the container. Run
  `docker exec corlinman getent hosts clawhub.ai` to check.

The Installed tab keeps working in this state — only the Browse Hub
tab is offline-banner-gated.

### `SkillAlreadyInstalledError` on POST `/admin/skills/hub/install`

The target directory already exists. Two paths:

- It's a bundled skill (same `<name>` collision). The install refuses;
  rename your skill or pick a different one. The bundle is read-only.
- It's a previous hub install. Re-issue the POST with `force: true`
  to replace it, or `DELETE /admin/skills/{name}` first.

### `UnsafeTarballError` on install

The tarball contained a path-traversal entry, a symlink, an absolute
path, or a member exceeding the size caps. The install is aborted
before any bytes land on disk; the sidecar is never written. Surfacing
this is the gateway saying "I don't trust what ClawHub returned" —
report the slug + version to the corlinman issue tracker so we can
investigate, then pick a different skill in the meantime.

### "Could not remove `<name>`: no `.openclaw-meta.json` sidecar"

The skill directory exists but didn't come from a hub install — either
it's a bundled starter or an operator-authored copy that pre-dates the
hub flow. The UI's Delete button correctly refused; manually `rm -rf`
the directory if you're sure you want to remove it (the agent will
notice the registry change on the next refresh).

---

## See also

- [Architecture](architecture.md) — how `SkillRegistry` consumes the
  `skills/` directory and the 30s refresh debounce.
- [Profiles](profiles.md) — each profile gets its own `skills/`
  directory; the hub installs land in the active one.
- [`docs/PLAN_SKILL_HUB.md`](PLAN_SKILL_HUB.md) — implementation plan
  with the full wave breakdown.
