# Runbook - VPS production release update

- **Target**: `corlinman.cornna.xyz` / `43.133.12.98`
- **Mode**: native systemd gateway + separate Python agent service
- **Install prefix**: `/opt/corlinman`
- **Data dir**: `/opt/corlinman/data`
- **Last verified**: 2026-06-06 09:33 CST, `v1.18.2`

This runbook is for the hosted demo VPS. It is intentionally more specific than
the generic installer docs because this box has a legacy root-owned native
layout.

## Current topology

- Gateway: `corlinman.service`, native Python venv at
  `/opt/corlinman/repo/.venv`, listening on `127.0.0.1:6005` through nginx.
- Agent runtime: `corlinman-agent.service`, `uv run corlinman-python-server`,
  gRPC on `127.0.0.1:50051`.
- Static UI: nginx serves `/opt/corlinman/ui-static/`, populated from
  `ui/out/`.
- QQ/NapCat: Docker container `corlinman-napcat` on the same host.
- NapCat QR refresh: nginx must exact-match
  `/api/QQLogin/RefreshQRcode` to the gateway before the generic NapCat
  `/api/` proxy, so the embedded WebUI uses corlinman's no-op detection and
  restart fallback instead of trusting NapCat's best-effort refresh response.

Do **not** run the generic `install.sh --upgrade` path on this host until the
root-owned systemd layout has been deliberately migrated. The generic native
upgrader rewrites systemd units for the unprivileged service user; on this VPS
the active gateway unit must remain `User=root` because its venv is under the
root-owned repo path.

## Pre-check

```bash
ssh root@43.133.12.98

cd /opt/corlinman/repo
git status --short
git rev-parse HEAD
git describe --tags --always --dirty || true
.venv/bin/python -c 'import importlib.metadata as m; print(m.version("corlinman-server"))'

systemctl is-active corlinman
systemctl is-active corlinman-agent
curl -fsS http://127.0.0.1:6005/health
docker ps --filter name=corlinman --format '{{.Names}} {{.Status}} {{.Image}}'
```

Expected:

- `git status --short` is empty.
- gateway and agent are `active`.
- `/health` returns `{"status":"ok","mode":"ok"}`.
- `corlinman-napcat` remains running if QQ is enabled.

## Upgrade

Set the release explicitly. Do not deploy bare `main` to production.

```bash
set -euo pipefail

PREFIX=/opt/corlinman
REPO_DIR=$PREFIX/repo
UI_DIR=$PREFIX/ui-static
TARGET_REF=vX.Y.Z
UV=/root/.local/bin/uv

cd "$REPO_DIR"
before_sha=$(git rev-parse HEAD)
before_desc=$(git describe --tags --always --dirty 2>/dev/null || printf '%s' "$before_sha")
ts=$(date +%Y%m%d-%H%M%S)
ui_backup="$PREFIX/ui-static.backup.$ts"

test -z "$(git status --short)"
rsync -a "$UI_DIR/" "$ui_backup/"

git fetch --depth 1 origin "refs/tags/$TARGET_REF"
git reset --hard FETCH_HEAD

"$UV" sync --all-packages --frozen --no-dev
pnpm -C ui install --frozen-lockfile
pnpm -C ui build
rsync -a --delete "$REPO_DIR/ui/out/" "$UI_DIR/"

systemctl restart corlinman-agent.service
systemctl restart corlinman.service
```

If `systemctl restart corlinman.service` hangs in `deactivating
(stop-sigterm)` and the journal already shows `Application shutdown complete`,
the old gateway process has failed to exit after a graceful shutdown. Capture
the status, then kill only the old main PID so systemd can finish the pending
restart:

```bash
systemctl status corlinman --no-pager | sed -n '1,60p'
systemctl kill --kill-who=main -s SIGKILL corlinman
```

Use this only for the stuck old process during a restart. It will briefly return
502 at the public edge until the new gateway has bound port 6005.

## Verify

```bash
cd /opt/corlinman/repo

git rev-parse HEAD
git describe --tags --always --dirty || true
.venv/bin/python -c 'import importlib.metadata as m; print(m.version("corlinman-server"))'

systemctl is-active corlinman
systemctl is-active corlinman-agent
curl -fsS http://127.0.0.1:6005/health
curl -fsS -o /tmp/corlinman-openapi.json -w '%{http_code}\n' \
  http://127.0.0.1:6005/openapi.json
curl -sS -o /dev/null -w '%{http_code}\n' \
  http://127.0.0.1:6005/admin/system/info
curl -sS -X POST -o /dev/null -w '%{http_code}\n' \
  http://127.0.0.1:6005/api/QQLogin/RefreshQRcode
```

Expected:

- package version matches the release tag.
- both services are `active`.
- `/health` is OK.
- `/openapi.json` returns `200`.
- `/admin/system/info` returns `401` without credentials; that means the route
  is registered and auth-gated.
- `/api/QQLogin/RefreshQRcode` returns `401` without credentials; that means
  the gateway compatibility route is present and auth-gated.

Public checks:

```bash
for route in /health /login /marketplace /admin/system/info; do
  curl -sS -o /dev/null -w "$route %{http_code}\n" \
    "https://corlinman.cornna.xyz$route"
done
```

Expected: `/health`, `/login`, and `/marketplace` are `200`;
`/admin/system/info` is `401`.

## Rollback

Use the `before_sha` and `ui_backup` from the upgrade step.

```bash
set -euo pipefail

PREFIX=/opt/corlinman
REPO_DIR=$PREFIX/repo
UI_DIR=$PREFIX/ui-static
UV=/root/.local/bin/uv
before_sha=<previous-sha>
ui_backup=<backup-path>

cd "$REPO_DIR"
git reset --hard "$before_sha"
"$UV" sync --all-packages --frozen --no-dev
rsync -a --delete "$ui_backup/" "$UI_DIR/"
systemctl restart corlinman-agent.service
systemctl restart corlinman.service
curl -fsS http://127.0.0.1:6005/health
```

Keep the UI backup for at least one release cycle.

## 2026-06-06 09:33 CST deployment record

Release deployed:

- tag: `v1.18.2`
- commit: `bd2789a6da90138c1fac47b3e0f8f887b7beff79`
- package: `corlinman-server==1.18.2`
- UI backup: not changed; this release did not rebuild `ui/out/`

Verification results:

- remote repo reset to `HEAD=bd2789a6da90138c1fac47b3e0f8f887b7beff79`
- `systemctl is-active corlinman` -> `active`
- `systemctl is-active corlinman-agent` -> `active`
- local `/health` -> `{"status":"ok","mode":"ok"}`
- local `/openapi.json` -> `200`
- local `/admin/system/info` without credentials -> `401`
- local `POST /api/QQLogin/RefreshQRcode` without credentials -> `401`
- public `/health`, `/login`, `/marketplace` -> `200`
- public `/admin/system/info` -> `401`
- public `POST /api/QQLogin/RefreshQRcode` without credentials -> `401`
  JSON `missing_authorization`
- deployed-runtime status-link smoke -> pass:
  ordinary QQ reply sent only the answer, while a `subagent_spawn` turn sent
  exactly one standalone `实时状态` link and did not append that link to the
  final answer bubble.

Operational note: `corlinman.service` entered `deactivating (stop-sigterm)`;
the gateway had already logged `Application shutdown complete`, so the old
main process was killed with
`systemctl kill --kill-who=main -s SIGKILL corlinman`. systemd then completed
the restart and the new gateway became healthy.

## 2026-06-05 22:22 CST deployment record

Release deployed:

- tag: `v1.18.1`
- commit: `8558071808a47a6080d7c97bcea27472597bc7fc`
- package: `corlinman-server==1.18.1`
- UI backup: not changed; this release did not rebuild `ui/out/`

Verification results:

- remote repo reset to `HEAD=8558071808a47a6080d7c97bcea27472597bc7fc`
- `systemctl is-active corlinman` -> `active`
- `systemctl is-active corlinman-agent` -> `active`
- local `/health` -> `{"status":"ok","mode":"ok"}`
- local `POST /api/QQLogin/RefreshQRcode` without credentials -> `401`
  JSON `missing_authorization`
- public `POST /api/QQLogin/RefreshQRcode` without credentials -> `401`
  JSON `missing_authorization`
- public `/health`, `/login`, `/marketplace` -> `200`
- public `/admin/system/info` -> `401`
- nginx active site contains two exact
  `location = /api/QQLogin/RefreshQRcode` blocks before the generic NapCat
  `/api` proxy blocks.

Operational note: the gateway restart took longer than the interactive shell
timeout, but the new main process came up cleanly; no `systemctl kill` was
needed for this deployment.

## 2026-06-05 20:37 CST deployment record

Release deployed:

- tag: `v1.18.0`
- commit: `ad9fa04821f88c287eb6b038b5d41f98e81df89e`
- package: `corlinman-server==1.18.0`
- UI backup: `/opt/corlinman/ui-static.backup.20260605-203358`

Verification results:

- remote repo clean, `HEAD=ad9fa04821f88c287eb6b038b5d41f98e81df89e`
- `systemctl is-active corlinman` -> `active`
- `systemctl is-active corlinman-agent` -> `active`
- local `/health` -> `{"status":"ok","mode":"ok"}`
- local `/openapi.json` -> `200`
- public `/health`, `/login`, `/marketplace` -> `200`
- public `/admin/system/info` -> `401`

Operational note: the old gateway process accepted SIGTERM and logged
application shutdown, but did not exit promptly. It was killed with
`systemctl kill --kill-who=main -s SIGKILL corlinman`; systemd then completed
the restart and the new gateway became healthy.
