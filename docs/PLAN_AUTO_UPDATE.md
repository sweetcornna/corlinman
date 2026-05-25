# PLAN — Auto-Update Detection + Upgrade Page

**Status:** draft v1.0 · 2026-05-25
**Goal:** corlinman knows when a new GitHub release lands, surfaces a tiny TopNav bubble + a dedicated `/admin/system` upgrade page with release notes, and gives operators copy-paste upgrade commands. No in-app one-click upgrade in MVP — that requires sudo escalation the gateway doesn't have.

---

## 0. Diagnosis

Three parallel Explore passes settled the design space.

### 0.1 corlinman current state

| Surface | State |
|---|---|
| Canonical version | README badge `v1.1.1` (manual); `pyproject.toml` still `0.1.0`; latest git tag `v1.1.1` — **drift across 3 sources** |
| `/healthz.version` | Backend `_package_version()` reads `importlib.metadata.version("corlinman-server")`; live deploy returns `{"status":"ok","mode":"ok"}` (no version) |
| TopNav mount point | `ui/components/layout/nav.tsx:46-54` right slot: HealthDot ↔ LanguageToggle — natural for the bubble |
| `/admin/system` page | Does not exist (greenfield) |
| `/admin/dev-settings` | Exists; dev-mode entry, not About — leave alone |
| Scheduler | `JobSpec` + `[[scheduler.jobs]]` TOML; can host periodic update poll |
| In-process upgrade trigger | Blocked — `install.sh --upgrade` needs sudo + shell. **MVP: copy command to clipboard.** |

### 0.2 hermes pattern (reference)

| Mechanism | hermes choice | corlinman adapts |
|---|---|---|
| Version source | `__version__ = "0.14.0"` in `hermes_cli/__init__.py` + `/api/status` | Same — read from `importlib.metadata` so it tracks the actual installed dist |
| Update poll | `git ls-remote` for git installs / `pypi.org/pypi/{pkg}/json` for pip | We use GitHub releases API (need release notes; ls-remote gives no body) |
| Cache | `~/.hermes/.update_check` JSON, 6h TTL | Same TTL, journal-side cache |
| Comparison | Tuple semver (no prerelease) | `packaging.version.Version` (handles `v1.2.0-rc.1`, leading `v`, PEP 440) |
| Notification | CLI banner only — no persistent web bubble | We add the bubble (user explicitly asked) |
| Upgrade UI | Manual "Update Hermes" button → subprocess | We can't subprocess sudo — copy-to-clipboard instead |
| Pre-update backup | `_run_pre_update_backup()` | Defer — corlinman has `--upgrade` that preserves `$DATA_DIR` already |

### 0.3 GitHub API facts

- `GET /repos/{owner}/{repo}/releases/latest` — no auth needed for public repos; excludes prereleases (good)
- Unauthenticated: 60/hr/IP — plenty for 6h polling per instance
- **ETag + `If-None-Match` → 304 doesn't count against limit** (key optimization)
- Response carries `tag_name`, `body` (markdown), `html_url`, `published_at`, `prerelease`, `draft`
- Optional `CORLINMAN_GITHUB_TOKEN` env var bumps limit to 5000/hr (multi-instance ops)

---

## 1. Target architecture

### 1.1 Backend

**New module** `corlinman_server/system/update_checker.py`:
- `UpdateChecker.poll() -> UpdateStatus` — hits GitHub releases API with stored ETag; 304 returns cached result; 200 parses + diffs against current version via `packaging.version.Version`.
- Persistent cache in `$DATA_DIR/.update_check.json` — `{etag, last_checked_at, latest_tag, latest_body_md, latest_url, published_at}`. 6h TTL between background polls; manual refresh bypasses TTL.
- Returns `UpdateStatus(current, latest, available: bool, release_url, release_notes_md, published_at, last_checked_at, prerelease_seen: list[tag])`.
- Configurable via `[system.update_check]` TOML stanza: `enabled`, `interval_hours` (default 6), `include_prereleases` (default false), `github_token = { env = ... }`.

**New routes** `routes_admin_b/system.py`:
- `GET /admin/system/info` — returns `UpdateStatus` + deploy-mode hint (`docker` / `native` detected from env). Auth-gated.
- `POST /admin/system/check-updates` — force-poll now (rate-limited 1/min server-side); returns fresh `UpdateStatus`.
- `GET /admin/system/upgrade-commands` — returns `{native, docker, docker_with_qq}` strings populated with the latest tag so users get a one-line copy.

**Scheduler integration**:
- One canonical `[[scheduler.jobs]]` registered at startup: `name="system.update_check"`, cron `0 0 */6 * * *`, action `RunTool { name = "system.update_check" }`. The tool handler calls `UpdateChecker.poll()`.
- Existing scheduler infra in `corlinman_server/scheduler/` accepts this without invasive changes.

**Version unification**:
- `pyproject.toml` workspace version → bump to `1.1.1` (sync with the actual deployed tag).
- `ui/package.json` → `1.1.1`.
- Future `release.yml` workflow should auto-sync; out of scope this round.

### 1.2 Frontend

**New `ui/components/system/update-bubble.tsx`**:
- Mounted in TopNav between HealthDot and LanguageToggle.
- Polls `/admin/system/info` on mount + every 30s (cheap — backend cache makes most calls 304-equivalent).
- States:
  - No update or fetch failed → renders nothing (silent).
  - Update available → small amber dot + chip with `vX.Y.Z`. Click → navigates to `/admin/system`.
  - Dismissed for this tag → silent until the next release (`corlinman_update_dismissed_tag` in localStorage).
- Accessible: `aria-label="Update available, version X.Y.Z"`, keyboard-focusable.
- Light pulse animation (gentle 2s loop, respects `prefers-reduced-motion`).

**New `ui/app/(admin)/system/page.tsx`**:
- Layout (vertical stacks):
  1. **Header**: title "系统信息 / System" + Refresh button (calls `POST /check-updates`).
  2. **Version card**: current version (mono badge) · last checked (relative time) · prerelease channel toggle.
  3. **Update available banner** (only if `available`):
     - Big "vX.Y.Z available · published 2 days ago" line.
     - Sanitized markdown render of `release_notes_md` via `react-markdown` + `rehype-sanitize` (NO raw HTML, no script tags, no auto-loaded images from arbitrary URLs).
     - Link to full GitHub release page.
     - "Dismiss until next release" button → stash tag in localStorage.
  4. **Upgrade commands card**:
     - Tabs: `native` / `docker` / `docker --with-qq`.
     - Each tab shows a code block with the exact `bash install.sh --upgrade --version vX.Y.Z` line.
     - Copy button per tab. Toast on copy success.
  5. **Footnote**: link to `docs/runbook.md` and the explicit deploy upgrade notes.

**New `ui/components/system/release-notes.tsx`**: sanitized markdown renderer for GitHub release `body`. Uses `react-markdown` (likely already a dep — verify) + `rehype-sanitize`. Strips raw HTML, allows headings/lists/links/code blocks. No auto-execute, no script tags.

**New `ui/components/system/copy-upgrade-command.tsx`**: small button + monospace code block. Copies via `navigator.clipboard.writeText` with a fallback to a hidden textarea. Toast on success.

**Sidebar nav**: add `System` entry under the settings group (icon: `MonitorCog` from lucide-react). Route: `/admin/system`.

**i18n**: `system.*`, `update.*` keys in `en.ts` + `zh-CN.ts`.

**API client** `ui/lib/api.ts`:
```ts
export type UpdateStatus = {
  current: string;
  latest: string | null;
  available: boolean;
  release_url: string | null;
  release_notes_md: string | null;
  published_at: number | null;     // unix ms
  last_checked_at: number | null;  // unix ms
  prerelease_seen: string[];
};
export type UpgradeCommands = {
  native: string;
  docker: string;
  docker_with_qq: string;
};

export async function getSystemInfo(): Promise<UpdateStatus>;
export async function checkSystemUpdates(): Promise<UpdateStatus>;
export async function getUpgradeCommands(): Promise<UpgradeCommands>;
```

### 1.3 Persistence + state

| Where | Stored | TTL |
|---|---|---|
| `$DATA_DIR/.update_check.json` | ETag + latest tag + body + url + published_at | 6h between polls; manual refresh bypasses |
| `localStorage corlinman_update_dismissed_tag` | last dismissed tag | Cleared when newer tag appears |
| Scheduler history | success/fail per poll | Existing ring buffer (1k entries) |

---

## 2. Tasks (3 waves, 6 background agents)

### Wave 1 — Backend foundation (2 parallel)

#### W1.1 update_checker + system routes + scheduler hook

- **Owner:** Backend Architect
- **Files:**
  - `python/packages/corlinman-server/src/corlinman_server/system/__init__.py` (new)
  - `python/packages/corlinman-server/src/corlinman_server/system/update_checker.py` (new) — `UpdateChecker` + `UpdateStatus` dataclass; GitHub fetch with ETag; cache I/O; semver compare via `packaging.version.Version`; configurable interval + include_prereleases + token
  - `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_b/system.py` (new) — three endpoints
  - `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_b/__init__.py` (modify) — register router
  - `python/packages/corlinman-server/src/corlinman_server/scheduler/builtins.py` (or wherever scheduler tool registry lives) — register `system.update_check` tool wrapping `UpdateChecker.poll()`
  - `python/packages/corlinman-server/src/corlinman_server/gateway/lifecycle/entrypoint.py` (modify) — read `[system.update_check]` config + instantiate + ensure cron job exists
  - `python/packages/corlinman-server/src/corlinman_server/runtime_config.py` (or settings file) — add `SystemUpdateCheckConfig` dataclass
  - `pyproject.toml` workspace root — bump version `0.1.0 → 1.1.1`
  - `python/packages/corlinman-server/pyproject.toml` — same bump
  - Tests: `tests/system/test_update_checker.py` (mock httpx, ETag flow, 304 path, prerelease exclusion, version compare edge cases), `tests/gateway/routes_admin_b/test_system_routes.py`
- **Hermes pattern:** `hermes_cli/banner.py:124-267` cache file + tuple compare adapted to ETag + `packaging.version.Version`
- **GitHub API:** `/repos/ymylive/corlinman/releases/latest` with `If-None-Match` header; respect `X-RateLimit-Remaining`
- **Deps:** none
- **Validation:** 
  - Mock GitHub → 200 with newer tag → `available=True`, body parsed
  - Mock GitHub → 304 → cache returned untouched, no rate-limit consumption
  - Mock GitHub → 403 rate-limited → log warning, return last-cached status
  - Version sync: `uv run python -c "from importlib.metadata import version; print(version('corlinman-server'))"` → `1.1.1`
- **ETA:** 6h

#### W1.2 Frontend: api.ts types + TopNav bubble

- **Owner:** Frontend Developer + UI Designer
- **Files:**
  - `ui/lib/api.ts` (modify) — add 3 functions + 2 types from §1.2
  - `ui/components/system/update-bubble.tsx` (new) — TopNav bubble
  - `ui/components/system/__tests__/update-bubble.test.tsx` (new) — 3 cases (no update / update available / dismissed)
  - `ui/components/layout/nav.tsx` (modify) — mount `<UpdateBubble />` between HealthDot and LanguageToggle (right slot)
  - `ui/lib/locales/en.ts` + `zh-CN.ts` (modify) — add `update.bubble.label`, `update.bubble.dismiss`, etc.
- **Pattern:** VS Code-style quiet badge + dismissible per-version
- **Deps:** can stub the backend response shape; W1.1's contract is the type definitions in this same task
- **Validation:**
  - Stub `getSystemInfo` → no update → bubble renders null
  - Stub → update + not dismissed → bubble renders with `vX.Y.Z` chip
  - localStorage has dismissed tag matching latest → bubble silent
  - `pnpm -C ui typecheck` clean
- **ETA:** 4h

### Wave 2 — Upgrade page + sanitized release notes (2 parallel)

#### W2.1 `/admin/system` page + ReleaseNotes + CopyUpgradeCommand

- **Owner:** Frontend Developer
- **Files:**
  - `ui/app/(admin)/system/page.tsx` (new)
  - `ui/components/system/release-notes.tsx` (new) — sanitized markdown renderer
  - `ui/components/system/copy-upgrade-command.tsx` (new)
  - `ui/lib/utils.ts` (if needed) — small clipboard helper
  - `ui/components/layout/sidebar.tsx` (modify) — add `System` entry to settings group nav
  - Verify `react-markdown` and `rehype-sanitize` are in `ui/package.json` — if missing, add them
  - Tests: `ui/components/system/__tests__/release-notes.test.tsx` (XSS sanitization assertion), `__tests__/copy-upgrade-command.test.tsx`
  - i18n keys: `system.title`, `system.version.current`, `system.version.latest`, `system.update.available`, `system.update.upgradeCommands`, `system.update.dismiss`, `system.update.refresh`, copy-success toast text
- **Security:**
  - `rehype-sanitize` with default schema (strips `<script>`, event handlers, javascript: urls)
  - Test asserts a malicious release body containing `<script>alert(1)</script>` renders as visible text not executable
- **Deps:** W1.1 endpoint + W1.2 api.ts functions
- **Validation:**
  - Mock `getSystemInfo` returning a release with markdown body → page renders headings + list + code blocks
  - Page handles `available=false` cleanly (no banner, shows "you're on the latest")
  - Copy buttons paste correct `install.sh --upgrade` lines per mode
- **ETA:** 6h

#### W2.2 Settings stanza, scheduler builtin wiring, integration tests

- **Owner:** Backend Architect
- **Files:**
  - `docs/config.example.toml` (modify) — add documented `[system.update_check]` stanza
  - `python/packages/corlinman-server/src/corlinman_server/scheduler/builtins.py` — register `system.update_check` tool action
  - `tests/scheduler/test_system_update_check_job.py` (new) — cron tick fires the job; verifies cache file is touched
  - `tests/gateway/lifecycle/test_system_update_check_bootstrap.py` (new) — config disabled → no job; config enabled → job present
- **Deps:** W1.1
- **Validation:**
  - Boot the gateway with `[system.update_check] enabled=true` → scheduler `list_jobs()` includes `system.update_check`
  - Boot with `enabled=false` → not registered
  - Manual cron tick → poll runs → `.update_check.json` updated
- **ETA:** 4h

### Wave 3 — Smoke + docs (2 parallel)

#### W3.1 Playwright smoke spec

- **Owner:** API Tester
- **Files:**
  - `ui/tests/e2e/system-update.spec.ts` (new) — 3 tests:
    1. No update → TopNav bubble silent + `/admin/system` shows "you're on latest"
    2. Update available → bubble renders + click → `/admin/system` shows release notes + copy buttons
    3. Dismiss via localStorage → reload → bubble silent
  - Reuse stub patterns from `admin-pages-smoke.spec.ts`
- **Deps:** W2.1
- **Validation:** `pnpm -C ui exec playwright test --list` shows 3 new tests
- **ETA:** 3h

#### W3.2 Docs + CHANGELOG + sidebar nav

- **Owner:** Technical Writer
- **Files:**
  - `docs/observability.md` or new `docs/system-updates.md` — explain the feature, the config stanza, how to set `CORLINMAN_GITHUB_TOKEN`, how to disable
  - `docs/quickstart.md` — one-line addition pointing operators at `/admin/system` for upgrade
  - `CHANGELOG.md` — entry under `[Unreleased]`
- **Deps:** W2.1
- **ETA:** 2h

---

## 3. Parallelization

```
Wave 1 (2 parallel):       W1.1 (backend)    W1.2 (frontend bubble + api.ts)
                                │
Wave 2 (2 parallel after W1):  W2.1 (system page)   W2.2 (scheduler + integration tests)
                                │
Wave 3 (2 parallel after W2):  W3.1 (e2e smoke)   W3.2 (docs + CHANGELOG)
```

Total wall-clock ~1 working day with 2 concurrent agents.

---

## 4. Explicitly out of scope

- One-click in-app upgrade (gateway can't sudo; MVP is copy-paste)
- Auto-rollback on failed upgrade (install.sh's --upgrade already preserves data dir)
- GHCR digest auto-detection for docker installs (relies on tag comparison only this round)
- Persistent per-user dismiss state on the backend (localStorage is enough)
- Auto-bump pyproject.toml from release tag in CI (version sync done manually this round; a future release.yml automates)
- Telegram/QQ channel notifications when an update lands (no demand yet)

---

## 5. Risks

| Risk | Mitigation |
|---|---|
| GitHub rate-limit hit by a busy operator constantly refreshing | Server-side 1/min rate limit on `POST /check-updates`; ETag means most polls are free |
| Release body markdown contains XSS payload | `rehype-sanitize` strips raw HTML + javascript: URLs; test asserts a `<script>` payload doesn't execute |
| Operator runs corlinman behind a corporate proxy that blocks `api.github.com` | Background poll catches `httpx.HTTPError`, returns stale-cached result; UI shows "Last checked 3 days ago — check connectivity" |
| Version drift across pyproject.toml + ui/package.json + README + git tag | This wave syncs to `1.1.1`; a follow-up CI step (out of scope) will auto-sync on tag |
| `importlib.metadata.version("corlinman-server")` returns `0.0.0` in editable dev installs | Fallback chain: env var `CORLINMAN_VERSION` → `importlib.metadata` → git rev-parse → `0.0.0-dev` |
| Operator on prerelease channel gets noise from rc tags | Defaults to stable-only; toggle in `[system.update_check]` |

---

## 6. Decision points before kickoff

- [ ] Plan accepted as-is, or trim subset (e.g. drop W2.2 scheduler wiring if operators are OK with "click Refresh on the page")?
- [ ] OK to bump `pyproject.toml` + `ui/package.json` to `1.1.1` (sync with current git tag)?
- [ ] Add `CORLINMAN_GITHUB_TOKEN` to `[system.update_check]` config now, or skip in MVP?
- [ ] Default poll interval — 6h (hermes default) or 24h (less chatty)?

---

**End of plan v1.0.**
