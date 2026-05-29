# corlinman AI deployment prompt

Paste this entire file into an AI coding agent (Claude Code, Cursor, etc.) when
you want it to deploy or upgrade corlinman on a remote VPS. The prompt assumes
the agent has shell access (SSH or local) and can install packages.

The instructions are written for the AI, not for you — give it the prompt, the
target host, and any credentials, then let it execute.

---

## System prompt

You are deploying **corlinman v1.x** (pure-Python self-hosted LLM toolbox) to a
Linux host. The user will give you `--host`, optionally `--user`, the auth
mechanism (SSH key path or ssh-agent), and the deployment mode (`docker` or
`native`). Carry out the steps below, reporting after each phase.

> **Note about `install.sh`**: the installer now self-checks the host. Before
> you start running individual diagnostic commands, remember that `install.sh`
> already does a preflight (disk ≥ 5 GB, RAM ≥ 1 GB, port 6005 free, docker ≥ 24,
> required tools, OS support) and a post-boot `/health` poll. You can lean on
> those signals instead of duplicating them — see Phase 4.

### Phase 0 — Inventory the target

1. `ssh USER@HOST 'uname -srm; cat /etc/os-release; df -h /; free -h'` —
   record arch (must be x86_64 or aarch64), distro, free disk (need 5 GB+),
   free RAM (need 1 GB+). The installer will re-check, but having this in
   your report helps you reason about borderline cases.
2. Check existing service: `ssh USER@HOST 'systemctl is-active corlinman 2>/dev/null; docker ps --filter name=corlinman --format "{{.Names}}: {{.Status}}" 2>/dev/null'`.
3. If a Rust-era corlinman is running (binary at `/opt/corlinman/bin/corlinman-gateway`
   or container based on `ghcr.io/sweetcornna/corlinman:v0.*`), record it for the
   stop-and-replace step.
4. **If `corlinman` is already running on the same major version**, skip to
   **Phase 2.5 (Upgrade)** instead of a fresh install. The data dir is
   preserved across upgrades by design — don't tear it down.

### Phase 1 — Stop the old service (if any)

Only run this on a FRESH install where Phase 0 found a Rust-era or unrelated
service. Skip for in-place upgrades.

- Systemd: `sudo systemctl stop corlinman && sudo systemctl disable corlinman`
- Compose: `cd /opt/corlinman && docker compose -f corlinman.yml down` (legacy)
  or `cd /opt/corlinman/repo/docker/compose && docker compose down` (current)
- Backup the data dir before touching anything:
  `sudo tar -czf /tmp/corlinman-data-$(date +%s).tar.gz -C /opt/corlinman data || true`

### Phase 2 — Install the new Python plane (fresh)

Two paths. Pick **docker** by default; pick **native** if the host can't run
docker or the user wants systemd-managed Python.

#### `--mode docker`
```bash
ssh USER@HOST 'curl -fsSL https://raw.githubusercontent.com/sweetcornna/corlinman/main/deploy/install.sh \
  | bash -s -- --mode docker'
```
The script:
- Preflights the host (disk / RAM / port 6005 / docker 24+ / required tools).
- Tries `docker pull ghcr.io/sweetcornna/corlinman:latest` first (multi-arch image
  shipped by `.github/workflows/release-image.yml`). On 404/network failure
  falls back to a local `docker buildx build` — slower but always works.
- Brings up `docker/compose/docker-compose.yml` with `CORLINMAN_TAG=latest`.
- Waits for `/health` to return 200 (cap: `CORLINMAN_HEALTH_TIMEOUT`, default 60 s).
- Prints a single line with the `/login` URL + default credentials.

For China-region hosts, append `--china` — the script will rewrite PyPI, GitHub
raw, and Docker Hub URLs to mirror endpoints (Tsinghua / gh-proxy / DaoCloud).
The detection is automatic (`curl pypi.org` TTFB > 3 s → enabled); use the
flag to force it.

For QQ-channel deploys, append `--with-qq` (docker mode only) — layers the
NapCat sidecar and materializes `.env` from `deploy/.env.template`.

#### `--mode native`
```bash
ssh USER@HOST 'curl -fsSL https://raw.githubusercontent.com/sweetcornna/corlinman/main/deploy/install.sh \
  | bash -s -- --mode native'
```
Same `--china` flag. Native mode does not need docker but does install `uv`,
clones the repo to `/opt/corlinman/repo`, runs `uv sync --frozen --no-dev`, and
registers a systemd unit `corlinman.service`. The `/health` poll runs at the end
of native install too.

### Phase 2.5 — In-place upgrade (if Phase 0 detected an existing install)

```bash
ssh USER@HOST 'curl -fsSL https://raw.githubusercontent.com/sweetcornna/corlinman/main/deploy/install.sh \
  | bash -s -- --upgrade --version vX.Y.Z'
```
The `--upgrade` flag auto-detects docker vs native from the live state on the
host (looks for `corlinman.service` then `docker ps -a --filter name=corlinman`).
- **Docker mode**: pulls the new image, falls back to local rebuild on pull
  miss, then `docker compose up -d --no-deps corlinman` (NapCat untouched if
  it was running). Reports before/after image digest.
- **Native mode**: `git fetch --depth 1 + reset --hard FETCH_HEAD`,
  `uv sync --frozen --no-dev`, `systemctl restart corlinman.service`. Reports
  before/after SHA.

`$CORLINMAN_DATA_DIR` is **never** touched. Always pass `--version vX.Y.Z` (the
explicit release tag) for production upgrades; the bare `--upgrade` defaults to
`main` which is fine for staging but not for prod.

### Phase 3 — Bootstrap config

Two paths depending on how the user wants to configure LLM providers:

- **Web wizard**: the gateway boots and serves `/login`. Sign in with the
  seeded default `admin` / `root` — you'll be redirected to **Account &
  Security** and forced to rotate the password. From there the optional
  `/onboard` wizard wires LLM providers. Good for desktop users.
- **Headless CLI** (recommended for VPS): `corlinman init` is an interactive
  TTY wizard that walks the same flow — admin password rotation, provider
  pick from the registered list, secret paste, default model alias, optional
  embedding. Outputs TOML bit-identical to what the web finalize endpoint
  writes. Run it over SSH:
  ```bash
  ssh USER@HOST 'sudo -u root /opt/corlinman/repo/.venv/bin/corlinman init'
  # or (docker mode): docker exec -it corlinman corlinman init
  ```
- **Pre-existing config**: if `/opt/corlinman/data/config.toml` was preserved
  from a backup, leave it in place; the new gateway reads the same schema.
- **Scripted**: `POST /admin/config` accepts a TOML body and atomically swaps
  it in (see `docs/runbook.md` for the JSON envelope and restart-required
  fields).

### Phase 4 — Verify

Run all four:

1. `curl -fsS http://HOST:6005/health` — expect `{"status":"ok"}` with the
   per-check breakdown (`chat_service`, `db`, `providers`, `plugin_registry`).
   Note: `install.sh` already polled this and only printed success if it
   returned 200, so a fresh install where Phase 2 reported `✅ corlinman is
   live` has already passed this gate.
2. `curl -fsS http://HOST:6005/v1/models` — expect a JSON list (may be empty
   on a fresh install until providers are configured via Phase 3).
3. `ssh USER@HOST 'corlinman doctor --json'` — every check should be `ok` or
   `warn`; never `fail`. There are nine checks now: `data_dir`, `config`,
   `python`, `packages`, `runtime_config`, `provider_registry`,
   `runtime_wiring`, `must_change_password`, `port_bindable`. A `warn` on
   `must_change_password` means the seed `admin/root` is still active —
   expected on Day 1, must be cleared before declaring the deploy done.
4. If providers are configured, send a real chat. Pull the OpenAI-style
   API key shown at `/admin/tokens` (admin UI → Tokens) and pass it as the
   Bearer credential:
   ```bash
   curl -fsS http://HOST:6005/v1/chat/completions \
     -H "Authorization: Bearer $CORLINMAN_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"ping"}]}'
   ```
   Expect a JSON envelope with `choices[0].message.content`.

### Phase 5 — Cleanup

- Delete the Rust binary install: `sudo rm -rf /opt/corlinman/bin /opt/corlinman/ui-static` (only if you confirmed phase 4 passes).
- Old systemd unit (Rust) is overwritten by Phase 2 install.
- Compose: leave the old image layers alone — `docker image prune -a -f` if you
  want to reclaim disk.

### Phase 6 — Report

Send the user:
- Target host, mode, ref/SHA installed (or before/after digest for upgrades).
- `/health` response (whole-object, not just `status`).
- `corlinman doctor --json` output. Highlight any `warn` checks and what
  they mean operationally (especially `must_change_password`).
- Per-provider quick test outcome (one line each).
- Disk + memory usage delta vs Phase 0.

### Stop conditions

Pause and ask the user if:
- Any phase emits a non-zero exit code you can't recover from.
- Phase 0 finds < 5 GB free or < 1 GB RAM (the installer will refuse too).
- Phase 2 install.sh exits non-zero (read the last 30 lines and diagnose;
  the preflight block emits readable `[✓]`/`[✗]` lines so you can spot
  exactly which check failed).
- Phase 4 `/health` returns 5xx for > 60 seconds (the installer's own
  `wait_for_health` would have warned; if the user got `⚠️ health probe
  timed out` in Phase 2 stdout, debug before claiming success).
- The user has not configured any provider and you're tempted to mock one
  to make the chat test pass — don't.

---

## What the AI should NOT do

- Don't `rm -rf` the data dir without an explicit backup confirmation.
- Don't change the listening port without telling the user; nginx + TLS often
  hard-wire `127.0.0.1:6005`.
- Don't run `--china` mirror flag automatically on hosts outside CN; it adds
  latency on US/EU hosts. The installer's autodetect (`pypi.org` TTFB > 3 s)
  is the safe default — let it decide.
- Don't push secrets (provider API keys, admin password hash) into git, even
  to a private repo. Use `.env` files in the data dir, gitignored.
- Don't bypass the preflight with environment shenanigans. If the host
  doesn't meet the minimums, the deploy will be flaky — fail loudly.
- Don't run a fresh install over an existing one. Use `--upgrade` to
  preserve `$DATA_DIR` and the user's local edits to the systemd unit.

## What the AI should always do

- Read `pyproject.toml` for the current workspace version before pinning a tag.
- Use `uv sync --all-packages --frozen` so the lockfile is the source of truth.
- After every shell command, log the exit code and the last 5 lines.
- If `--china` is on, confirm by hitting `pypi.tuna.tsinghua.edu.cn` once.
- After deploy, run `corlinman doctor` AND `curl /health` AND a real chat —
  three signals, not one. The installer's own `/health` poll counts as the
  first signal but doctor + chat must still be exercised.
- For production upgrades, always pass `--version vX.Y.Z` (the explicit
  release tag), never bare `--upgrade` (which defaults to `main`).
