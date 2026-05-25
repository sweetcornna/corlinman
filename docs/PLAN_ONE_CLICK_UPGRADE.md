# PLAN — One-Click Upgrade

**Status:** draft v1.0 · 2026-05-25
**Goal:** click the `<UpdateBubble>` chip or the "Upgrade" button on `/admin/system` → corlinman upgrades itself, end to end, with a live progress panel. No terminal. Two privileged paths (Docker socket + native systemd helper) selected by runtime mode.

Replaces the v1.2.0 "copy-paste upgrade command" UX. The copy-paste flow stays as a fallback for air-gapped or hardened deploys.

---

## 0. Diagnosis (audited 2026-05-25)

| Surface | Current state |
|---|---|
| `/etc/systemd/system/corlinman.service` | `Type=simple`, runs as **root** (no `User=` line), `ExecStart=uv run corlinman-gateway`. Source: `install.sh:572-589`. |
| `upgrade_native()` | `git fetch + reset --hard FETCH_HEAD → uv sync --frozen → systemctl restart corlinman.service`. Source: `install.sh:608-653`. |
| `docker-compose.yml` | Container user is **non-root** `corlinman` (uid auto, system user). No `docker.sock` mount by default. Sandbox overlay exists but mounts it `:ro` — useless for upgrade writes. |
| `upgrade_docker()` | `docker inspect → docker pull → docker compose up -d --no-deps corlinman`. Source: `install.sh:662-702`. |
| Python Docker SDK | **Not a dep**. Need `docker>=7.0`. |
| `.path` systemd units | None. We'd be introducing the pattern. |
| Runtime mode detection | **No signal today**. Need to plumb `CORLINMAN_RUNTIME_MODE=native|docker` from install.sh. |
| Audit log | None for system events. Recommend append-only JSONL at `$DATA_DIR/system-audit.log`. |

### 0.1 The two paths

**Docker path (Docker SDK over `/var/run/docker.sock`):**
- Mount socket read-write (NOT `:ro`) into the gateway container.
- Gateway adds the `corlinman` system user to the host's `docker` group, OR runs `chmod g+rw /var/run/docker.sock` via an entrypoint trick. (Need to pick — see §1.3.)
- Gateway calls `docker.from_env().images.pull('ghcr.io/ymylive/corlinman:v1.2.1')` then triggers compose-equivalent recreate via the Docker API.
- Same machinery as Watchtower / Portainer; well-trodden.

**Native path (file-watched privileged helper):**
- New systemd unit `corlinman-upgrader.service` (Type=oneshot, User=root) + `corlinman-upgrader.path` (PathChanged=`$DATA_DIR/.upgrade-request`).
- Gateway writes a JSON request file → systemd fires the upgrader → upgrader validates the tag, calls `install.sh --upgrade --version vX.Y.Z`, writes a status JSON the gateway polls.
- Helper has a *whitelisted* surface: it only accepts upgrade requests for tags that exist in `ymylive/corlinman` releases.

### 0.2 Safety posture (user-confirmed)

1. **Session cookie required** — same admin auth that gates every other `/admin/*` mutation.
2. **Confirm dialog typing the exact tag** — user types `v1.2.1` into a text field; button stays disabled until match. Prevents click-throughs and AI-assisted misclicks.
3. **No downgrade allowed** by default — `Version(target) > Version(current)` enforced server-side. Override via `?allow_downgrade=true` query param + explicit confirm — used only by emergency rollback.
4. **Tag whitelist** — only tags that exist in the `ymylive/corlinman` GitHub releases list. Upgrader script re-validates before calling install.sh.
5. **Single in-flight upgrade** — `POST /admin/system/upgrade` returns 409 if a status JSON shows `state=running`.
6. **Structured audit log** — every request + state transition appended to `$DATA_DIR/system-audit.log` (JSONL). Visible on the `/admin/system` page.

---

## 1. Target architecture

### 1.1 Mode detection

- `install.sh install_native()` sets `Environment=CORLINMAN_RUNTIME_MODE=native` in the systemd unit.
- `install.sh install_docker()` (and the bundled `docker-compose.yml`) sets `CORLINMAN_RUNTIME_MODE=docker` on the container.
- Gateway reads `os.environ.get("CORLINMAN_RUNTIME_MODE")` at startup; persists on `AppState.runtime_mode`. Falls back to:
  - `os.path.exists("/.dockerenv")` → `docker`
  - Else → `unknown` (one-click upgrade disabled, copy-paste UX still works)

### 1.2 New backend module `corlinman_server/system/upgrader/`

Files:
- `__init__.py` — exports `UpgraderProtocol`, `DockerUpgrader`, `NativeUpgrader`, `UpgradeRequest`, `UpgradeStatus`, `resolve_upgrader(mode, ...)`.
- `protocol.py` — abstract `UpgraderProtocol`:
  ```python
  class UpgraderProtocol(Protocol):
      async def start(self, target_tag: str, actor: str) -> UpgradeRequest: ...
      async def status(self, request_id: str) -> UpgradeStatus | None: ...
      async def is_available(self) -> bool: ...  # docker.sock writable / upgrader unit installed
  ```
- `docker_upgrader.py` — uses `docker` Python SDK:
  1. `client = docker.from_env(timeout=120)` (will auto-pick `/var/run/docker.sock`)
  2. `images.pull("ghcr.io/ymylive/corlinman", tag=target)` — stream layers, emit progress
  3. Find the corlinman container, capture old image id
  4. Recreate via `container.stop() → containers.run(...)` mirroring the compose service spec, OR easier: shell-out to `docker compose -f /app/compose/corlinman.yml up -d --no-deps corlinman` (compose CLI is in the runtime image after this round). The compose-CLI fallback keeps env/volume parity with the operator's compose file at zero extra plumbing.
  5. Wait for new container to be healthy (check the existing HEALTHCHECK status with a 60s timeout)
  6. Write status JSON throughout.
- `native_upgrader.py`:
  1. Validate target_tag in GitHub releases (re-uses `UpdateChecker`).
  2. Write `$DATA_DIR/.upgrade-request` with `{request_id, tag, requested_at, requested_by, mode: "native"}` — atomic via temp + rename.
  3. Poll `$DATA_DIR/.upgrade-status` for that `request_id` until terminal state or 5min timeout.
  4. Stream status to the SSE consumer (see §1.4).

### 1.3 Docker socket access

The gateway container user is `corlinman` (non-root). On the host, `/var/run/docker.sock` is owned by `root:docker` (mode 660). Two opt-in approaches:

**(a) Group-id alignment at install time (preferred)**:
- `install.sh --enable-one-click-upgrade` (new flag) — detects the host's `docker` group GID and renders `docker-compose.yml` with `group_add: ["<gid>"]` so the `corlinman` user inside the container joins the docker group.
- Bind-mount `/var/run/docker.sock:/var/run/docker.sock` (no `:ro`).
- No image change required; works with the stock Dockerfile.

**(b) Entrypoint chmod hack (rejected)**:
- Container entrypoint runs `chmod 666 /var/run/docker.sock` before dropping privs.
- Too aggressive — alters host file permissions.

We ship (a). The opt-in flag means default deploys don't ship with docker-socket access; the user explicitly turns it on.

### 1.4 Native systemd helper

Three new files written by `install.sh install_native()`:

**`deploy/corlinman-upgrader.sh`** (new, ships in the repo):
- Reads `$CORLINMAN_DATA_DIR/.upgrade-request`
- Validates schema (json fields, request_id is uuid-shaped, tag matches semver regex)
- Validates tag against `https://api.github.com/repos/ymylive/corlinman/releases` (cached for 10 min in `/var/cache/corlinman/upgrader-tag-cache.json`)
- Writes `.upgrade-status state=running`
- Calls `bash $INSTALL_PREFIX/repo/deploy/install.sh --upgrade --version <tag>`
- Captures last 4kB of combined stdout+stderr → `.upgrade-status.log_excerpt`
- On exit code 0 → `state=succeeded`; non-zero → `state=failed`
- Always touches `.upgrade-request.processed` and **deletes** `.upgrade-request` so the path unit doesn't refire on every reboot

**`/etc/systemd/system/corlinman-upgrader.path`** (rendered by install.sh):
```ini
[Unit]
Description=Watch for corlinman upgrade requests

[Path]
PathChanged=/opt/corlinman/data/.upgrade-request
Unit=corlinman-upgrader.service

[Install]
WantedBy=multi-user.target
```

**`/etc/systemd/system/corlinman-upgrader.service`** (rendered by install.sh):
```ini
[Unit]
Description=corlinman one-shot upgrader
After=network-online.target

[Service]
Type=oneshot
User=root
ExecStart=/opt/corlinman/repo/deploy/corlinman-upgrader.sh
StandardOutput=journal
StandardError=journal
SyslogIdentifier=corlinman-upgrader
TimeoutStartSec=600
```

`install.sh install_native()` enables both units. Existing deploys can re-run `install.sh --upgrade` once to land them (no `--upgrade` needed; the install path is idempotent).

### 1.5 Endpoints

**`POST /admin/system/upgrade`** — kicks off an upgrade:
- Body: `{tag: "v1.2.1", typed_confirmation: "v1.2.1"}` (typed_confirmation MUST equal `tag`).
- Resolves the right upgrader by `AppState.runtime_mode`.
- Validates: tag in GitHub releases · `Version(tag) > Version(current)` (override flag) · no other upgrade in flight.
- Returns `{request_id, state: "queued"}` 202 Accepted.
- 409 if another upgrade is running.
- 503 if `upgrader.is_available()` is False (e.g. native mode without the helper installed).

**`GET /admin/system/upgrade/{request_id}/status`** — poll:
- Returns the current `UpgradeStatus`. Pollable every 1-2s by the UI.

**`GET /admin/system/upgrade/{request_id}/events`** — SSE:
- Streams progress events (image-pull layer % for Docker, journal tail for native) until terminal state.

**`GET /admin/system/audit`** — paginated audit log:
- Returns parsed JSONL entries from `$DATA_DIR/system-audit.log`. Shown on `/admin/system` "Recent activity" card.

### 1.6 Frontend

**`<UpdateBubble>` (modified)**:
- Click the chip → opens a small inline popover (anchored to the bubble) with two actions:
  - **Upgrade now** → opens the typed-confirmation modal
  - **View release notes** → navigates to `/admin/system` (current behavior)
- Dismiss button stays.

**`<UpgradeConfirmModal>` (new)**:
- Title: "Upgrade to v1.2.1"
- Brief release-note excerpt (first 2 lines from `release_notes_md`)
- Text input — placeholder shows the tag; button disabled until typed exactly
- "Cancel" / "Upgrade" buttons; on Upgrade → POST `/admin/system/upgrade`, switch to live-progress view
- Live progress view subscribes to the SSE events stream, shows: phase (`validating → pulling → restarting → healthcheck`) + log tail panel + a spinner
- Terminal states: green check + "Upgrade complete — reloading in 5s…" → auto window.location.reload, OR red error + "View details" → expanded log

**`/admin/system` page additions**:
- The existing "Upgrade commands" tabs get a sibling **"Upgrade now"** primary button. Copy-paste stays as the fallback tab.
- New "Recent activity" card at the bottom — paginated audit-log table (`actor`, `tag`, `state`, `started`, `finished`, expand for log excerpt).

### 1.7 Audit log

`$DATA_DIR/system-audit.log` — append-only JSONL. One line per state transition:

```json
{"ts": "2026-05-25T12:00:00Z", "event": "system.upgrade.requested", "request_id": "abc", "tag": "v1.2.1", "actor": "ops"}
{"ts": "2026-05-25T12:00:01Z", "event": "system.upgrade.started", "request_id": "abc"}
{"ts": "2026-05-25T12:00:45Z", "event": "system.upgrade.completed", "request_id": "abc", "before": "1.2.0", "after": "1.2.1"}
```

Append-only is enough for now; rotation handled by the existing log-rotation infrastructure (or out-of-scope this round, accept unbounded growth — single line per upgrade, won't be large).

---

## 2. Tasks (3 waves, 7 background agents)

### Wave 1 — Backend foundations (3 parallel)

#### W1.1 `corlinman_server/system/upgrader/` module + abstract protocol + Docker impl

- **Owner:** Backend Architect
- **Files:**
  - `python/packages/corlinman-server/src/corlinman_server/system/upgrader/__init__.py` (new)
  - `python/packages/corlinman-server/src/corlinman_server/system/upgrader/protocol.py` (new)
  - `python/packages/corlinman-server/src/corlinman_server/system/upgrader/docker_upgrader.py` (new)
  - `python/packages/corlinman-server/src/corlinman_server/system/upgrader/state.py` (new) — in-memory request tracker keyed by `request_id`, persisted-on-flush to `.upgrade-state.json`
  - `python/packages/corlinman-server/pyproject.toml` — add `docker>=7.1.0` dep
  - Tests: `tests/system/upgrader/test_docker_upgrader.py` — mocks `docker.from_env()`; covers pull progress events, healthcheck success path, image-not-found failure path
- **ETA:** 7h

#### W1.2 Native upgrader + helper script + systemd path watcher

- **Owner:** Backend Architect (Python) + DevOps (bash + systemd)
- **Files:**
  - `python/packages/corlinman-server/src/corlinman_server/system/upgrader/native_upgrader.py` (new) — writes request JSON, polls status JSON
  - `deploy/corlinman-upgrader.sh` (new) — the privileged shell script
  - `install.sh install_native()` — write the two new systemd units + enable them + set `CORLINMAN_RUNTIME_MODE=native` in main service unit. Idempotent over re-runs.
  - `install.sh install_docker()` + `docker-compose.yml` — set `CORLINMAN_RUNTIME_MODE=docker` env
  - Tests: `tests/system/upgrader/test_native_upgrader.py` (Python side); a small bash test for `corlinman-upgrader.sh` validating tag format
- **ETA:** 8h

#### W1.3 Endpoints + audit log writer + mode detection + UpdateChecker integration

- **Owner:** Backend Architect
- **Files:**
  - `routes_admin_b/system.py` — extend with `POST /admin/system/upgrade`, `GET /upgrade/{id}/status`, `GET /upgrade/{id}/events` (SSE), `GET /admin/system/audit`
  - `corlinman_server/system/audit.py` (new) — `SystemAuditLog` append-only writer + reader
  - `gateway/lifecycle/entrypoint.py` — read `CORLINMAN_RUNTIME_MODE`, resolve the right `UpgraderProtocol` impl, stash on AdminState
  - Tests: `tests/gateway/routes_admin_b/test_system_upgrade.py` — 8 cases (typed confirmation enforcement, downgrade refusal, single-flight 409, mode unknown 503, full happy path with mocked upgrader, audit-log written, SSE stream emits, status polling)
- **ETA:** 6h

### Wave 2 — Frontend (2 parallel)

#### W2.1 `<UpgradeConfirmModal>` + live progress UI + bubble popover

- **Owner:** Frontend Developer
- **Files:**
  - `ui/components/system/upgrade-confirm-modal.tsx` (new) — typed confirmation + release-note excerpt + Upgrade button
  - `ui/components/system/upgrade-progress.tsx` (new) — SSE-driven progress panel (phase pills + log tail)
  - `ui/components/system/update-bubble.tsx` — extend with popover ("Upgrade now" / "View release notes")
  - `ui/components/system/__tests__/upgrade-confirm-modal.test.tsx` — 3 cases (button disabled until typed match, POST fires on click, displays release-note excerpt)
  - `ui/lib/api.ts` — `triggerUpgrade(tag, typed)`, `getUpgradeStatus(id)`, `streamUpgradeEvents(id)`
- **ETA:** 6h

#### W2.2 `/admin/system` page primary Upgrade button + audit-log card + i18n

- **Owner:** Frontend Developer
- **Files:**
  - `ui/app/(admin)/system/page.tsx` — add a primary "Upgrade to vX.Y.Z" button above the copy-paste tabs (becomes the recommended action when `upgrader.is_available === true`)
  - `ui/components/system/audit-card.tsx` (new) — paginated table of recent upgrade events
  - `ui/lib/api.ts` — `listSystemAudit(opts)`
  - `ui/lib/locales/{en,zh-CN}.ts` — new keys (12-ish): `system.upgrade.button`, `system.upgrade.confirm.title`, `system.upgrade.confirm.typeLabel`, `system.upgrade.phases.*`, `system.audit.*`
- **ETA:** 5h

### Wave 3 — install.sh wiring + smoke + docs (2 parallel)

#### W3.1 install.sh `--enable-one-click-upgrade` + e2e smoke

- **Owner:** DevOps + API Tester
- **Files:**
  - `install.sh` — new flag `--enable-one-click-upgrade` (defaults to OFF). In docker mode: append `group_add: [{$DOCKER_GID}]` + r/w socket mount to the generated compose. In native: just confirm the helper units land (they're always written by W1.2's `install_native()` patch).
  - `deploy/install.sh --help` block: document the flag
  - `ui/tests/e2e/system-upgrade.spec.ts` (new) — 3 stubbed tests:
    1. Confirm modal blocks until typed match
    2. Upgrade-progress panel renders SSE phases
    3. Audit log card renders past events
- **ETA:** 4h

#### W3.2 Docs + CHANGELOG + security note

- **Owner:** Technical Writer
- **Files:**
  - `docs/system-updates.md` — expand the "Limitations" section into a new "One-click upgrade" section: how the two paths work, how to enable, how to disable, what the safety guarantees are, audit-log location
  - `CHANGELOG.md` — entry under `[Unreleased]` covering the work (next minor: v1.3.0)
  - `docs/PLAN_ONE_CLICK_UPGRADE.md` is already in tree from this commit
- **ETA:** 2h

---

## 3. Parallelization

```
Wave 1 (3 parallel):       W1.1   W1.2   W1.3
                              │      │      │
Wave 2 (2 parallel after W1):  W2.1   W2.2
                                  │
Wave 3 (2 parallel after W2):  W3.1   W3.2
```

Total wall-clock ~1.5 working days with 3 concurrent agents.

---

## 4. Explicitly out of scope

- **GHCR image signing / cosign verification** — defer; we trust GHCR's identity proof for v1.3.0
- **Multi-host upgrade orchestration** — each gateway upgrades itself; no fleet coordination
- **Rollback automation** — operator runs `install.sh --upgrade --version v(N-1)` manually; we don't auto-rollback on healthcheck failure (a stuck container is still safer than a flip-flop)
- **Customizing the upgrade command per-deployment** — sufficient: the helper calls `install.sh --upgrade --version vX.Y.Z`; operators who've forked install.sh are responsible for keeping that contract
- **Heartbeat / stuck-upgrade detection** — basic 5-minute timeout in MVP; richer health probes deferred
- **API token auth on the upgrade endpoint** — admin session cookie is enough for now; PAT-style token can come later if we expose corlinman to a CI bot

---

## 5. Risks

| Risk | Mitigation |
|---|---|
| Operator clicks "Upgrade now" by accident → unexpected restart | Typed confirmation (must type the tag) + audit log + single-flight |
| `docker.sock` mount = root on host | Off by default; operator opts in with `--enable-one-click-upgrade` after reading the security note |
| Compromised admin session can trigger arbitrary upgrade | Only forward upgrades to whitelisted tags; tag is always validated against GitHub releases (so worst case = an attacker re-installs a real corlinman version, not a poisoned image) |
| Native helper service runs as root | Whitelisted action (calls install.sh with a validated tag), tag-only input surface, no shell-injectable params |
| `install.sh --upgrade` fails mid-flight | Upgrader script captures stdout/stderr to log_excerpt; operator sees the error inline + audit log records it |
| Healthcheck never goes green after upgrade | 60s timeout per phase; status flips to `failed` and the UI surfaces the message + journalctl pointer |
| Two parallel admin sessions both click upgrade | `POST /upgrade` checks state-tracker before allowing; second returns 409 with the in-flight request_id |
| ETag-pinned UpdateChecker doesn't see the just-installed version | After successful upgrade, helper writes `request_id-success-marker` so the gateway invalidates its `.update_check.json` ETag on next start |

---

## 6. Decision points before kickoff

- [ ] Plan accepted as-is, or trim subset?
- [ ] OK to add `docker>=7.1.0` to `corlinman-server` deps (~3MB image growth)?
- [ ] OK that `--enable-one-click-upgrade` defaults to OFF in install.sh (operator opt-in)?
- [ ] Tag this work for `v1.3.0` minor bump when shipped?

---

**End of plan v1.0.**
