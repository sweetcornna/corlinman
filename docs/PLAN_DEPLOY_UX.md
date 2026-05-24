# PLAN — Deploy + Init UX Overhaul

**Status:** draft v1.0 · 2026-05-24 · parallel execution
**Goal:** drop "time-to-chat-ready" from ~20 min → ~2 min on a clean Linux box, and remove every redundant/confusing entrypoint along the way.

This plan is execution-ready: every task lists files, owner agent, deps,
and a binary validation step. Estimated 6 background agents in parallel,
~1 working day end-to-end if no scope drift.

---

## 0. Current state (audited 2026-05-24)

Three parallel Explore passes confirmed:

| Area | Reality | Pain |
|---|---|---|
| **Entrypoints** | `install.sh` (modern, full) + `server-bootstrap.sh` (stale QQ-only) coexist | Two scripts; bootstrap referenced nowhere except its own header — safe to delete |
| **Prebuilt image** | `docker-compose.yml:12` references `ghcr.io/ymylive/corlinman:dev` — **never pushed**. release-notes-v0.1.0.md:27 explicitly says "planned for 0.1.1 once a docker-equipped build host is available" | Every first install rebuilds locally (15-20 min); `docker compose pull` 404s |
| **CI** | `.github/workflows/ci.yml` lint/test only — no docker build job | No image pipeline at all |
| **Preflight** | `install.sh` jumps straight into `git clone` / `uv sync` — no disk/RAM/port/tool checks | Half-installed leftovers when prerequisites missing |
| **First boot** | `install.sh` echoes `:6005/onboard`; `docs/quickstart.md` says `:6005/login`; `admin_seed.py` seeds `admin/root`; `/onboard` wizard exists; `/admin/me` returns `must_change_password` ✓ | Three sources of truth disagree on the user's first click |
| **Upgrade** | `--upgrade` flag native-only; docker users have no path | Asymmetric |
| **CLI** | `corlinman doctor` ✓ (7 checks, `--json`), `corlinman onboard` (non-interactive skeleton only). No `corlinman init`. `corlinman tenant token list --json` referenced in AI_DEPLOY.md but **does not exist** | Headless servers must touch the web UI |
| **start.sh race** | `corlinman-python-server &; sleep 3; exec corlinman-gateway` | Blind 3s sleep; should be readiness probe on `127.0.0.1:50051` |
| **Templates** | `deploy/config.toml.template` duplicates an argon2 hash + `[admin]` block that `admin_seed.py` now writes automatically; only consumer was `server-bootstrap.sh` | Two sources of admin truth |

---

## 1. Target state

**60-second cold start, single command:**

```
curl -fsSL https://corlinman.io/install.sh | bash
   ↓ preflight (~2s):  disk ✓  ram ✓  port 6005 free ✓  docker 24+ ✓
   ↓ docker pull ghcr.io/ymylive/corlinman:v1.1.x  (~30s, prebuilt multi-arch)
   ↓ docker compose up -d  (~10s)
   ↓ poll /health until {"status":"ok"}  (~15s)
   ↓ print: open http://<HOST>:6005/login   admin / root
```

**Single entrypoint** — `install.sh` covers everything via flags.
**Single upgrade** — `install.sh --upgrade` works in both modes.
**Single first click** — `/login` (no more `/onboard`-vs-`/login` schism).
**Headless setup** — `corlinman init` interactive CLI for servers without a browser.
**One config source** — admin block is always seeded; `docs/config.example.toml` is the only reference template.

---

## 2. Tasks (8 waves, mostly parallel)

Tracking IDs map to `TaskList`. Each task is owner-tagged so background
agents can pick them up.

### A — Unify entrypoint  *(task #3)*

**Owner:** Backend Architect
**Files:**
- `deploy/install.sh` — add `--with-qq` flag; chain `docker-compose.qq.yml` when set. Inside docker_install(), drop the `corlinman.yml` override pattern; instead reuse `docker/compose/docker-compose.yml` from the cloned repo and layer overlays via `-f`.
- `deploy/install.sh` — auto-materialize `.env` from `deploy/.env.template` if user passes `--with-qq` (NapCat needs OPENAI / GEMINI / QQ vars).
- `deploy/server-bootstrap.sh` — **delete**.
- `docker/compose/docker-compose.yml:12` — change `image: ghcr.io/ymylive/corlinman:dev` → `image: ghcr.io/ymylive/corlinman:${CORLINMAN_TAG:-latest}` so `docker compose pull` lands on a real tag once task B ships.
- `README.md:130-167`, `deploy/AI_DEPLOY.md:43,51`, `docs/quickstart.md:30`, `docs/multi-agent-release-plan.md:97,99,341` — sweep refs.

**Deps:** task #2 (plan approved)
**Validation:**
1. `bash deploy/install.sh --help` lists `--with-qq`
2. `bash deploy/install.sh --mode docker --with-qq --dry-run` shows the chained compose command
3. `grep -r server-bootstrap.sh .` returns only release-notes archive matches
**ETA:** 2h

---

### B — GHCR prebuilt image  *(task #4)*

**Owner:** DevOps Automator
**Files:**
- `.github/workflows/release-image.yml` (new) — triggers on `push: tags: v*` and `push: branches: main`. Matrix-builds `linux/amd64,linux/arm64` via `docker/build-push-action@v6`. Pushes to `ghcr.io/ymylive/corlinman`. Tag schedule:
  - `vX.Y.Z`, `vX.Y`, `vX`, `latest` — only on tag pushes
  - `main-${{ github.sha }}` and `edge` — on main pushes
- `docker/Dockerfile:33,57` — add `--mount=type=cache,target=/root/.cache/uv` and `--mount=type=cache,target=/var/cache/apt`. Same for pnpm in ui-builder.
- `deploy/install.sh` docker path — `docker pull ghcr.io/ymylive/corlinman:${REF}`; on pull failure (404/network) fall through to the existing local build.
- `docs/multi-agent-release-plan.md:99` — fix the false claim once the workflow is green.

**Deps:** task #2
**Validation:**
1. CI green on a test tag → `docker manifest inspect ghcr.io/ymylive/corlinman:vX.Y.Z` shows amd64+arm64
2. `bash deploy/install.sh --mode docker --version vX.Y.Z` pulls instead of building (timer < 90s)
3. Cold cache → `docker buildx build` measured at < 5 min for incremental Python deps
**ETA:** 5h

---

### C — Preflight checks  *(task #5)*

**Owner:** Backend Architect
**Files:**
- `deploy/install.sh` — new `preflight()` function called from `main()` before either install path:
  - `disk`: `df --output=avail / | tail -1` ≥ 5 GiB
  - `ram`: `free -m | awk '/^Mem:/ {print $2}'` ≥ 1024 MiB (Linux); `sysctl hw.memsize` on Darwin
  - `port`: `ss -ltn | awk '{print $4}' | grep -q ":${PORT}$"` → fail with "port already in use"
  - `docker` version ≥ 24 (parse `docker version --format '{{.Server.Version}}'`)
  - `git`, `curl`, `tar` on PATH
  - prior install: warn if `/opt/corlinman/repo/.git` exists and user is not running `--upgrade`
- Format: `[\033[32m✓\033[0m]  disk: 12 GiB free` / `[\033[31m✗\033[0m]  port 6005 in use by PID 4421 (corlinman?)` — color when stdout is a TTY.

**Deps:** task #2
**Validation:**
1. Run on a host with port 6005 busy → exits non-zero before any clone
2. Run on a host with < 1 GB RAM → warns + exits
3. Run on healthy host → all green checks then continues
**ETA:** 3h

---

### D — First-experience alignment  *(task #6)*

**Owner:** Frontend Developer + Backend Architect (split)
**Files:**
- `deploy/install.sh` end of `install_docker()` / `install_native()` — replace blind success echo with `wait_for_health()`:
  ```bash
  for i in {1..30}; do
      curl -fsS http://localhost:${PORT}/health >/dev/null && break
      sleep 2
  done
  ```
  then print the SAME message in both paths:
  ```
  ✅ corlinman is live: http://<HOST>:${PORT}/login
     default login:  admin / root   ← change immediately
     data dir:       $DATA_DIR
     upgrade later:  bash deploy/install.sh --upgrade
  ```
- `docker/start.sh:60` — replace `sleep 3` with `wait_grpc_ready()`:
  ```sh
  for i in $(seq 1 30); do
      python3 -c "import socket; s=socket.socket(); s.settimeout(0.5); s.connect(('127.0.0.1',50051))" 2>/dev/null && break
      sleep 0.3
  done
  ```
- `docs/quickstart.md:30-71` — reorder so "First login at /login" is step 1, not "boot the gateway". Drop the `/onboard` reference from the front page; mention it later as "if you want to wire LLM keys from the UI".
- `deploy/AI_DEPLOY.md:63` — Phase 3 wording fix.
- `README.md` Quickstart block — match install.sh's final message verbatim.

**Deps:** task #2, task #4 (for the new echo block)
**Validation:**
1. Cold install → install.sh blocks until health=ok then prints `/login` (never `/onboard`)
2. `grep -rE '/onboard' README.md deploy/install.sh` returns 0
3. Docker container: `docker logs corlinman` shows no "py-config bootstrapped" race; sidecar always up before gateway
**ETA:** 4h

---

### E — Docker upgrade path  *(task #7)*

**Owner:** Backend Architect
**Files:**
- `deploy/install.sh` — new `upgrade_docker()`:
  ```bash
  before_digest=$(docker inspect corlinman --format '{{.Image}}' 2>/dev/null || echo "none")
  docker pull "ghcr.io/${REPO}:${REF}"
  (cd "$PREFIX/repo" && docker compose -f docker/compose/docker-compose.yml up -d --no-deps corlinman)
  after_digest=$(docker inspect corlinman --format '{{.Image}}')
  ```
  Detect mode in `main()`: `[[ -f /etc/systemd/system/corlinman.service ]]` → native; else if `docker ps -a --filter name=corlinman` matches → docker.
- Validation: `docker exec corlinman curl -fsS http://localhost:6005/health` returns 200 after restart.
- Echo before/after digests + data dir untouched warning.

**Deps:** task #4 (need real GHCR tags to pull)
**Validation:**
1. Deploy `v1.1.0`, then `install.sh --upgrade --version v1.1.1` → image swaps, data preserved
2. Native upgrade still works (no regression)
**ETA:** 3h

---

### F — Merge config templates  *(task #8)*

**Owner:** Minimal Change Engineer
**Files:**
- `deploy/config.toml.template` — **delete**. The argon2 hash + `must_change_password = true` block is now redundant with `admin_seed.py:218`. The QQ/Telegram/embedding blocks belong in `docs/config.example.toml`.
- `docs/config.example.toml` — verify it already covers QQ + Telegram + evolution + embedding stanzas; if not, port from the deleted template.
- `deploy/install.sh` — wherever it logs "see config template", replace with a pointer to `https://github.com/ymylive/corlinman/blob/main/docs/config.example.toml`.
- `docs/release-notes-ap1.0.0.md:141` — drop the line referencing the deleted file (or note its deletion).

**Deps:** task #2, task #3 (after server-bootstrap.sh is gone there are no consumers)
**Validation:**
1. `grep -r "config.toml.template" .` → 0 matches outside historical release notes
2. Fresh `install.sh` run produces a working config without any template copy
**ETA:** 1h

---

### G — corlinman init + doctor surface  *(task #9)*

**Owner:** AI Engineer
**Files:**
- `python/packages/corlinman-server/src/corlinman_server/cli/init.py` (new) — typer command. Flow:
  1. Print current admin status (seeded? must_change?)
  2. Prompt: change password now? (y/N)
  3. List builtin providers (anthropic / openai / gemini / deepseek / qwen / glm / mock)
  4. For chosen provider: paste-only secret prompt (`typer.prompt(hide_input=True)`)
  5. Default model alias write
  6. Optional: enable embedding (default: openai text-embedding-3-small)
  7. Reuse `/admin/onboard/finalize` request body shape — POST internally to localhost or write TOML directly via existing config writer
- `cli/main.py:51` — register `init` between `onboard` and `doctor`. Keep `onboard` for backwards compat but mark deprecated in `--help`.
- `cli/doctor.py:328` — add two checks:
  - `must_change_password`: warn if admin still uses default
  - `port_bindable`: try-bind PORT, warn if occupied by a non-corlinman process
- `deploy/AI_DEPLOY.md:76` — replace `corlinman tenant token list --json` with the actually-existing flow: `curl -fsS -H "Cookie: corlinman_session=…" :6005/admin/tokens` or the equivalent (verify with backend). If a CLI is preferred, scope-creep alert — drop the chat-test phase or add it as a separate task.

**Deps:** task #2
**Validation:**
1. `uv run corlinman init` on a fresh data dir walks the prompts and persists `config.toml` with a working provider
2. `uv run corlinman doctor --json` includes `must_change_password` + `port_bindable` checks
3. `grep -r "tenant token list" deploy/` returns 0 (after fixup)
**ETA:** 6h

---

### H — dev-setup polish  *(task #10)*

**Owner:** DevOps Automator
**Files:**
- `scripts/dev-setup.sh`:
  - Step 0: require Python 3.12 — `python3 -c 'import sys; assert sys.version_info >= (3,12)'`
  - Step 0: require Node ≥ 20 — `node -v | awk -F. '{exit !($1+0 >= 20)}'`
  - Step 4.5: detect `protoc`; on missing, print platform-specific install hint (`brew install protobuf` / `apt install protobuf-compiler`)
  - Step 5 (new): `uv run corlinman doctor` — quick health smoke
- `Makefile` — new `doctor:` target wrapping `uv run corlinman doctor`.

**Deps:** task #2
**Validation:**
1. Run on host without protoc → script prints clear install hint and exits 1 (no half-bootstrap)
2. Run on healthy host → `doctor` block at end shows all green checks
**ETA:** 2h

---

## 3. Execution plan (parallelization)

```
Wave 0 (now):       Task 1 (this plan) → Task 2 (user approval)
                                          │
                          ┌───────────────┼───────────────┐
Wave 1 (parallel):     Task A          Task B          Task H
                       (#3)            (#4)            (#10)
                                          │
                                          ▼
Wave 2 (depends B):    Task D          Task E
                       (#6)            (#7)
                                          │
                       Task C (#5), Task F (#8), Task G (#9) — independent of B,
                       launchable in Wave 1 alongside A/B/H
```

**Recommended dispatch:** 6 background agents in Wave 1 (A, B, C, F, G, H — all blocked only on plan approval), then 2 more in Wave 2 (D, E waiting on B). Total wall-clock ~1 working day.

---

## 4. Out of scope (this round)

- Implementing the missing `corlinman tenant token` CLI (defer; AI_DEPLOY.md just gets a wording fix instead)
- Image size optimization beyond cache mounts (separate effort)
- Windows native install (corlinman has never supported it; not on the roadmap)
- A "doctor --fix" auto-repair mode (would mask root causes; skip)
- Replacing `--china` autodetect with a smarter mirror probe (current works)

---

## 5. Risks

| Risk | Mitigation |
|---|---|
| GHCR build host doesn't exist in CI org yet | The workflow runs on `ubuntu-latest` GitHub-hosted runners — no special host needed. If org-level GHCR write needs a token, add `permissions: packages: write` at job level. |
| Existing native deploys (prod @ 43.133.12.98) break on next `install.sh --upgrade` | A is additive; --upgrade path is unchanged for native. Validate on the prod server with a `--dry-run` first. |
| `wait_for_health` 60s timeout too short on cold container start | Make timeout configurable: `CORLINMAN_HEALTH_TIMEOUT=90 bash install.sh`. |
| `docker compose up -d --no-deps corlinman` skips napcat upgrade in QQ mode | Note in echo: "qq sidecar unchanged; rerun without --upgrade to refresh napcat too". |
| Deleting `config.toml.template` breaks an unknown downstream script | Audit (task A) found only `server-bootstrap.sh` referenced it. Acceptable risk. |

---

## 6. Decision points before kickoff

- [ ] Plan accepted as-is, or trim to subset (e.g. drop G "init CLI" if 6h is too much)?
- [ ] OK to push real images to `ghcr.io/ymylive/corlinman` (uses Anthropic's GHCR quota — minimal)?
- [ ] OK to delete `deploy/server-bootstrap.sh` and `deploy/config.toml.template`?
- [ ] Default GHCR tag in compose: `latest` (rolling) or `v1.1.x` (pinned)? Recommend `latest` for `install.sh` default but pinned for prod.

---

**End of plan v1.0.** Next: user approves → I dispatch 6 background agents in Wave 1.
