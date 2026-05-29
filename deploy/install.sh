#!/usr/bin/env bash
# corlinman one-line installer + upgrader (Python plane, v1.1+).
#
# Usage (any one of):
#   # Fresh install (default: docker mode, latest main)
#   curl -fsSL https://raw.githubusercontent.com/sweetcornna/corlinman/main/deploy/install.sh | bash
#
#   # Fresh native install pinned to a release tag
#   curl -fsSL https://raw.githubusercontent.com/sweetcornna/corlinman/main/deploy/install.sh \
#     | bash -s -- --mode native --version v1.1.0
#
#   # In-place upgrade of an existing native deployment (preserves data)
#   curl -fsSL https://raw.githubusercontent.com/sweetcornna/corlinman/main/deploy/install.sh \
#     | bash -s -- --upgrade
#
#   # China-region (auto-detected, or force with --china)
#   curl -fsSL https://raw.githubusercontent.com/sweetcornna/corlinman/main/deploy/install.sh \
#     | bash -s -- --mode native --china
#
# Modes:
#   docker  (default) — builds a Docker image locally from this repo, brings
#                       up corlinman + newapi via compose. Needs Docker
#                       Engine 24+ with the compose v2 plugin.
#   native            — installs uv, clones the repo, runs `uv sync
#                       --all-packages`, registers a systemd unit invoking
#                       `corlinman-gateway`. Requires root or sudo on Linux.
#
# Flags:
#   --upgrade         In-place upgrade an existing deployment at
#                     $CORLINMAN_PREFIX — auto-detects docker vs native:
#                       native : refreshes repo, re-runs `uv sync --frozen`,
#                                restarts the systemd unit.
#                       docker : pulls (or rebuilds on miss) the image for
#                                --version and restarts only the corlinman
#                                service (--no-deps, so napcat is left
#                                alone in --with-qq stacks).
#                     Never touches $CORLINMAN_DATA_DIR. Re-running
#                     install.sh without --upgrade rewrites the systemd
#                     unit and compose override — use --upgrade to leave
#                     local edits alone.
#   --china           Use 2026-verified CN mirrors:
#                       PyPI    → pypi.tuna.tsinghua.edu.cn (Tsinghua TUNA)
#                       GitHub  → gh-proxy.com (clone + raw)
#                       Docker  → docker.m.daocloud.io (DaoCloud)
#                       Debian  → mirrors.tuna.tsinghua.edu.cn
#                     Autodetected when `curl https://pypi.org` TTFB > 3s.
#                     Override individual endpoints via env vars (see below).
#   --enable-docker-sandbox
#                     Mount /var/run/docker.sock so Docker-backed plugin
#                     sandboxing can spawn child containers. High-trust hosts
#                     only; disabled by default.
#   --enable-one-click-upgrade
#                     Docker mode only. Mount /var/run/docker.sock RW + add
#                     the in-container corlinman user to the host's `docker`
#                     group so `/admin/system` can run a one-click upgrade
#                     via the Docker SDK (pull + recreate). docker.sock is
#                     root-equivalent on the host — opt-in only. Native
#                     installs always land the corlinman-upgrader.{path,
#                     service} units; no flag needed there.
#   --with-qq         Docker mode only. Layer
#                     `docker/compose/docker-compose.qq.yml` on top of the
#                     base compose file so the NapCat QQ sidecar comes up
#                     alongside corlinman. Auto-materialises `.env` from
#                     `deploy/.env.template` if one isn't present; you'll be
#                     prompted to edit it (QQ_* / OPENAI_API_KEY) and re-run.
#   --skip-ui         Skip the Next.js UI build + ui-static placement.
#                     Use for headless deploys (no Node/pnpm) or when the
#                     UI is served from a separate container. The previous
#                     ui-static (if any) is preserved untouched. Also via
#                     CORLINMAN_SKIP_UI=1.
#   --version <ref>   Git ref / branch / tag to install from (default: main).
#
# Environment overrides:
#   CORLINMAN_PREFIX     install root for --mode native (default: /opt/corlinman)
#   CORLINMAN_DATA_DIR   data dir (default: $CORLINMAN_PREFIX/data or ~/.corlinman)
#   CORLINMAN_PORT       gateway port (default: 6005)
#   CORLINMAN_ENABLE_DOCKER_SANDBOX=1
#                       Same effect as --enable-docker-sandbox.
#   CN_PIP_INDEX         override PyPI mirror (default tuna)
#   CN_GH_PROXY          override GitHub clone proxy host (default gh-proxy.com).
#                        Empty = no proxy (direct github.com — works on some CN
#                        BGP networks including Tencent Cloud Tianjin).
#   CN_DOCKER_MIRROR     override Docker Hub mirror (default docker.m.daocloud.io)

set -euo pipefail

MODE="docker"
REF="${CORLINMAN_VERSION:-main}"
PREFIX="${CORLINMAN_PREFIX:-/opt/corlinman}"
DATA_DIR="${CORLINMAN_DATA_DIR:-${PREFIX}/data}"
PORT="${CORLINMAN_PORT:-6005}"
REPO="sweetcornna/corlinman"
USE_CHINA=""
ENABLE_DOCKER_SANDBOX="${CORLINMAN_ENABLE_DOCKER_SANDBOX:-}"
ENABLE_ONE_CLICK_UPGRADE="${CORLINMAN_ENABLE_ONE_CLICK_UPGRADE:-}"
UPGRADE_MODE=""
WITH_QQ=""
SKIP_UI="${CORLINMAN_SKIP_UI:-}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode) MODE="$2"; shift 2 ;;
        --mode=*) MODE="${1#--mode=}"; shift ;;
        --version) REF="$2"; shift 2 ;;
        --version=*) REF="${1#--version=}"; shift ;;
        --china) USE_CHINA="1"; shift ;;
        --enable-docker-sandbox) ENABLE_DOCKER_SANDBOX="1"; shift ;;
        --enable-one-click-upgrade) ENABLE_ONE_CLICK_UPGRADE="1"; shift ;;
        --upgrade) UPGRADE_MODE="1"; shift ;;
        --with-qq) WITH_QQ="1"; shift ;;
        --skip-ui) SKIP_UI="1"; shift ;;
        -h|--help)
            # Print the top-of-file usage block (everything between line 2
            # and the first non-`#` line) so all flags including --with-qq
            # are visible regardless of where the block ends up.
            awk 'NR>1 { if ($0 ~ /^#/) { sub(/^# ?/, ""); print } else { exit } }' "$0"
            exit 0
            ;;
        *) echo "unknown argument: $1" >&2; exit 1 ;;
    esac
done

log()  { printf "\033[1;34m==>\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m!\033[0m %s\n" "$*" >&2; }
die()  { printf "\033[1;31m✗\033[0m %s\n" "$*" >&2; exit 1; }
require() { command -v "$1" >/dev/null 2>&1 || die "required tool '$1' not on PATH"; }

# Dedicated unprivileged service account the gateway runs as. Mirrors the
# Docker image (docker/Dockerfile creates a `corlinman` system user/group and
# drops to it with `USER corlinman`). The native systemd unit MUST do the same
# so the internet-facing gateway (BIND=0.0.0.0) never runs as root.
SERVICE_USER="corlinman"

# ----- Unprivileged service user --------------------------------------------
# Create a system user/group `corlinman` (no home, nologin shell) for the
# gateway to run as. Guarded by an existence check so re-runs are idempotent.
# Linux-only (Darwin native installs have no systemd unit and run in the
# foreground as the invoking user).
ensure_service_user() {
    [[ "$(uname -s)" == "Linux" ]] || return 0
    if getent passwd "$SERVICE_USER" >/dev/null 2>&1; then
        return 0
    fi
    log "creating unprivileged system user '$SERVICE_USER'"
    # --user-group makes a matching system group; fall back to an explicit
    # groupadd + useradd pair on toolchains where --user-group is unavailable.
    sudo useradd --system --no-create-home --shell /usr/sbin/nologin \
        --user-group "$SERVICE_USER" 2>/dev/null \
        || { getent group "$SERVICE_USER" >/dev/null 2>&1 \
                || sudo groupadd --system "$SERVICE_USER"; \
             sudo useradd --system --no-create-home --shell /usr/sbin/nologin \
                --gid "$SERVICE_USER" "$SERVICE_USER"; } \
        || warn "could not create '$SERVICE_USER' user — gateway may fall back to root"
}

# ----- Lock down root-executed upgrade scripts -------------------------------
# `sudo chown -R "$(id -u):$(id -g)" "$PREFIX"` (run by both install paths)
# hands every file under $PREFIX — including the scripts corlinman-upgrader.
# service later executes as User=root — to the unprivileged install user. That
# is a local privilege-escalation: the unprivileged user could rewrite a
# root-executed script and get root code-exec on the next one-click upgrade.
# Re-chown the root-executed scripts back to root:root and strip group/other
# write so only root can modify them. MUST be invoked AFTER the recursive
# chown in each path.
secure_root_executed_scripts() {
    [[ "$(uname -s)" == "Linux" ]] || return 0
    local script
    for script in \
        "$PREFIX/repo/deploy/corlinman-upgrader.sh" \
        "$PREFIX/repo/deploy/install.sh"; do
        [[ -e "$script" ]] || continue
        sudo chown root:root "$script" || warn "could not chown $script to root"
        sudo chmod 0755 "$script" || warn "could not chmod $script"
    done
}

# ----- PATH augmentation -----------------------------------------------------
# install.sh is called both interactively (where ~/.bashrc has already
# pulled ~/.local/bin into PATH) and from `corlinman-upgrader.service`
# (User=root, systemd's restrictive default PATH = /usr/local/sbin:
# /usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin — no ~/.local/bin).
# In the latter case `require uv` fired `die` even though uv was happily
# installed at /root/.local/bin/uv, leaving operators with a cryptic
# "required tool 'uv' not on PATH" in the upgrader log and a one-click
# upgrade that never completes. Probe the well-known locations and add
# whichever exist to PATH before any `require` runs.
augment_path() {
    local d
    for d in "$HOME/.local/bin" /root/.local/bin /usr/local/lib/node_modules/.bin; do
        [[ -d "$d" ]] || continue
        case ":$PATH:" in
            *":$d:"*) ;;
            *) PATH="$d:$PATH" ;;
        esac
    done
    # Match *every* home dir's .local/bin so sudo-from-non-root flows
    # (e.g. ubuntu user with passwordless sudo) still find uv.
    local home_local
    for home_local in /home/*/.local/bin; do
        [[ -d "$home_local" ]] || continue
        case ":$PATH:" in
            *":$home_local:"*) ;;
            *) PATH="$home_local:$PATH" ;;
        esac
    done
    export PATH
}
augment_path

# ----- UI stage --------------------------------------------------------------
# Build the Next.js static export (``ui/out``) and place it at
# ``$PREFIX/ui-static`` — the directory the systemd unit points
# ``CORLINMAN_UI_DIR`` at. The gateway serves /admin/* from this dir, so
# without this stage operators end up running yesterday's UI against
# today's API and every admin page silently drifts (the symptom is
# stale page content, working endpoints, no error in the logs).
#
# Honoured switches:
#   * ``--skip-ui`` / ``CORLINMAN_SKIP_UI=1`` — headless deploys
#     (CI smoke, docker-only environments where the UI lives in a
#     separate container) skip the build entirely. The previous
#     ui-static is preserved as-is so the gateway keeps serving
#     whatever it had.
#
# Idempotent: re-runs do a clean rsync of ``ui/out/`` → ``ui-static/``
# with ``--delete`` so stale chunks (e.g. dropped pages like the old
# /playground/protocol demo) actually disappear.
build_and_place_ui() {
    if [[ -n "$SKIP_UI" ]]; then
        warn "--skip-ui set; leaving $PREFIX/ui-static untouched"
        return 0
    fi
    local ui_src="$PREFIX/repo/ui"
    if [[ ! -d "$ui_src" ]]; then
        warn "no ui/ at $ui_src; skipping UI build"
        return 0
    fi
    # Tooling probe. pnpm is the only supported package manager (matches
    # ui/package.json + the lockfile we ship). corepack on a modern Node
    # gives us pnpm without a global install — try that first.
    if ! command -v pnpm >/dev/null 2>&1; then
        if command -v corepack >/dev/null 2>&1; then
            log "enabling pnpm via corepack"
            corepack enable >/dev/null 2>&1 || true
        fi
    fi
    if ! command -v pnpm >/dev/null 2>&1; then
        warn "pnpm not on PATH and corepack failed — install Node 20+ + pnpm or pass --skip-ui"
        warn "skipping UI build; $PREFIX/ui-static will not be refreshed"
        return 0
    fi

    log "pnpm install --frozen-lockfile (ui)"
    (cd "$ui_src" && pnpm install --frozen-lockfile) || die "pnpm install failed"

    log "pnpm build (ui)"
    (cd "$ui_src" && pnpm build) || die "pnpm build failed"

    if [[ ! -d "$ui_src/out" ]]; then
        die "ui build did not produce ui/out — check next.config (expected output: 'export')"
    fi

    log "rsync ui/out → $PREFIX/ui-static"
    sudo mkdir -p "$PREFIX/ui-static"
    sudo rsync -a --delete "$ui_src/out/" "$PREFIX/ui-static/"
    log "ui-static refreshed ($(sudo find "$PREFIX/ui-static" -type f | wc -l) files)"
}

# ----- Health probe -----------------------------------------------------------
# Polls /health on the configured PORT until it returns 200, up to
# $CORLINMAN_HEALTH_TIMEOUT seconds (default 60). Returns 0 on first 200,
# 1 on timeout. Never `die`s — caller decides whether the timeout is fatal
# (it usually isn't: cold container starts can outlast the default window).
wait_for_health() {
    local url="http://localhost:${PORT}/health"
    local timeout="${CORLINMAN_HEALTH_TIMEOUT:-60}"
    local start
    start=$(date +%s)
    log "waiting for /health (timeout ${timeout}s)..."
    while (( $(date +%s) - start < timeout )); do
        if curl -fsS -m 2 "$url" >/dev/null 2>&1; then
            local elapsed=$(( $(date +%s) - start ))
            log "/health ok after ${elapsed}s"
            return 0
        fi
        sleep 1
    done
    warn "/health did not return 200 within ${timeout}s — service may still be starting"
    return 1
}

# ----- Unified success banner -------------------------------------------------
# Single source of truth for the post-install / post-upgrade echo. Both the
# docker and native paths converge here so the user sees the same text and
# the same first URL no matter how they installed. `$1` is the mode-specific
# logs hint ("docker compose ..." | "journalctl ..."). `$2` is an optional
# prefix (e.g. the warning sigil when wait_for_health timed out).
print_success() {
    local logs_hint="$1"
    local prefix="${2:-}"
    local header="✅ corlinman is live: http://localhost:${PORT}/login"
    if [[ -n "$prefix" ]]; then
        header="$prefix $header"
    fi
    cat <<EOF

$header
   default login:  admin / root   ← change immediately at /account/security
   data dir:       ${DATA_DIR}
   upgrade later:  bash deploy/install.sh --upgrade
   logs:           ${logs_hint}
EOF
}

# ----- Preflight --------------------------------------------------------------
# Validates host has enough headroom + required tools BEFORE any side effects
# (git clone, docker pull, sudo writes). Exits non-zero on any hard failure so
# half-installed leftovers don't pollute the box. Skipped in --upgrade mode —
# upgrade has its own minimal checks in upgrade_native().
preflight() {
    local has_tty=0
    [[ -t 1 ]] && has_tty=1
    # Color helpers — only paint if stdout is a TTY, plain text otherwise so
    # piping into a logger / CI summary stays readable.
    local ok fail
    if [[ "$has_tty" == "1" ]]; then
        ok=$'\033[32m\xe2\x9c\x93\033[0m'
        fail=$'\033[31m\xe2\x9c\x97\033[0m'
    else
        ok="OK"
        fail="FAIL"
    fi

    log "preflight checks"
    local errors=0

    # --- OS ---------------------------------------------------------------
    local uname_s
    uname_s="$(uname -s)"
    case "$uname_s" in
        Linux|Darwin)
            printf "  [%s] os: %s\n" "$ok" "$uname_s"
            ;;
        *)
            printf "  [%s] os: %s (only linux/darwin supported)\n" "$fail" "$uname_s"
            errors=$((errors + 1))
            ;;
    esac

    # --- Tools (always) ---------------------------------------------------
    local tool
    for tool in curl git tar; do
        if command -v "$tool" >/dev/null 2>&1; then
            printf "  [%s] tool: %s\n" "$ok" "$tool"
        else
            printf "  [%s] tool: %s (missing on PATH)\n" "$fail" "$tool"
            errors=$((errors + 1))
        fi
    done

    # --- Tools (docker only) ----------------------------------------------
    if [[ "$MODE" == "docker" ]]; then
        if command -v docker >/dev/null 2>&1; then
            if docker compose version >/dev/null 2>&1; then
                printf "  [%s] tool: docker (with compose v2 plugin)\n" "$ok"
            else
                printf "  [%s] tool: docker present but 'docker compose' v2 plugin missing\n" "$fail"
                errors=$((errors + 1))
            fi
        else
            printf "  [%s] tool: docker (missing on PATH)\n" "$fail"
            errors=$((errors + 1))
        fi
    fi

    # --- Disk space ($PREFIX target, fall back to /) ----------------------
    # 5 GiB minimum: image build + uv cache + node_modules + a little slack.
    local disk_target="/"
    [[ -d "$PREFIX" ]] && disk_target="$PREFIX"
    local avail_kb
    # POSIX df: column 4 is "Available" in 1K blocks on both Linux + macOS
    # when invoked with -k.
    avail_kb=$(df -k "$disk_target" 2>/dev/null | awk 'NR==2 {print $4}')
    if [[ -n "$avail_kb" && "$avail_kb" =~ ^[0-9]+$ ]]; then
        local avail_gib=$((avail_kb / 1024 / 1024))
        if [[ "$avail_gib" -ge 5 ]]; then
            printf "  [%s] disk: %s GiB free at %s\n" "$ok" "$avail_gib" "$disk_target"
        else
            printf "  [%s] disk: %s GiB free at %s (need >= 5 GiB)\n" "$fail" "$avail_gib" "$disk_target"
            errors=$((errors + 1))
        fi
    else
        printf "  [%s] disk: could not read df output for %s\n" "$fail" "$disk_target"
        errors=$((errors + 1))
    fi

    # --- RAM --------------------------------------------------------------
    # 1 GiB minimum so uv sync + the gateway boot don't OOM.
    local ram_mib=0
    if [[ "$uname_s" == "Linux" ]]; then
        # `free -m` total on column 2 of the Mem row.
        if command -v free >/dev/null 2>&1; then
            ram_mib=$(free -m 2>/dev/null | awk '/^Mem:/ {print $2}')
        elif [[ -r /proc/meminfo ]]; then
            ram_mib=$(awk '/^MemTotal:/ {print int($2/1024)}' /proc/meminfo)
        fi
    elif [[ "$uname_s" == "Darwin" ]]; then
        local memsize_bytes
        memsize_bytes=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
        ram_mib=$((memsize_bytes / 1024 / 1024))
    fi
    if [[ -n "$ram_mib" && "$ram_mib" =~ ^[0-9]+$ && "$ram_mib" -ge 1024 ]]; then
        printf "  [%s] ram: %s MiB total\n" "$ok" "$ram_mib"
    else
        printf "  [%s] ram: %s MiB total (need >= 1024 MiB)\n" "$fail" "${ram_mib:-?}"
        errors=$((errors + 1))
    fi

    # --- Port in use ------------------------------------------------------
    # Probe $PORT for an existing listener. Linux: ss -ltn. Darwin: lsof.
    local port_in_use=""
    if [[ "$uname_s" == "Linux" ]]; then
        if command -v ss >/dev/null 2>&1; then
            ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${PORT}$" && port_in_use="1"
        elif command -v netstat >/dev/null 2>&1; then
            netstat -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${PORT}$" && port_in_use="1"
        fi
    elif [[ "$uname_s" == "Darwin" ]]; then
        if command -v lsof >/dev/null 2>&1; then
            lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && port_in_use="1"
        fi
    fi
    if [[ -n "$port_in_use" ]]; then
        printf "  [%s] port: %s already in use\n" "$fail" "$PORT"
        errors=$((errors + 1))
    else
        printf "  [%s] port: %s free\n" "$ok" "$PORT"
    fi

    # --- Prior install (soft warning only) --------------------------------
    if [[ -d "$PREFIX/repo/.git" ]]; then
        warn "existing install detected at $PREFIX/repo — re-running install.sh will rewrite the systemd unit and compose override. Use --upgrade to leave them alone."
    fi

    if [[ "$errors" -gt 0 ]]; then
        die "$errors preflight check(s) failed; resolve the items above and re-run."
    fi
}

# ----- China autodetect -------------------------------------------------------
# A 3-second TTFB on pypi.org is the rough breakpoint where uv sync starts to
# painfully stall; below that we don't bother routing through a mirror.
autodetect_china() {
    if [[ -n "$USE_CHINA" ]]; then return 0; fi
    local t
    t=$(curl -o /dev/null -fsS -m 3 -w '%{time_starttransfer}' https://pypi.org/simple/ 2>/dev/null || echo "999")
    awk -v t="$t" 'BEGIN { exit !(t+0 > 3.0) }' && USE_CHINA="1"
    if [[ -n "$USE_CHINA" ]]; then
        log "slow pypi.org TTFB (${t}s) — enabling --china mirrors"
    fi
}

# Mirror endpoints used when USE_CHINA is set.
# Defaults are picked from a 2026-04 probe round of the most commonly cited
# CN mirrors — see docs/quickstart.md "China-region deployment" for the live
# probe matrix. Anything that died (ghproxy.com, mirror.ghproxy.com,
# jsdelivr CDN for raw GitHub files, dockerhub.icu, kkgithub.com from some
# Tencent BGP edges) was dropped from the default chain.
GITHUB_RAW="https://raw.githubusercontent.com"
GITHUB_CLONE_BASE="https://github.com"
PIP_INDEX="https://pypi.org/simple"
PIP_INDEX_FALLBACK=""
DOCKER_REGISTRY_MIRROR=""
NPM_REGISTRY=""
DEBIAN_MIRROR=""
apply_china_mirrors() {
    if [[ -z "$USE_CHINA" ]]; then return 0; fi
    local cn_pip="${CN_PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"
    local cn_gh_proxy="${CN_GH_PROXY-gh-proxy.com}"
    local cn_docker="${CN_DOCKER_MIRROR:-https://docker.m.daocloud.io}"

    PIP_INDEX="$cn_pip"
    PIP_INDEX_FALLBACK="https://mirrors.aliyun.com/pypi/simple/"
    NPM_REGISTRY="https://registry.npmmirror.com"
    DEBIAN_MIRROR="mirrors.tuna.tsinghua.edu.cn"
    DOCKER_REGISTRY_MIRROR="$cn_docker"

    if [[ -n "$cn_gh_proxy" ]]; then
        GITHUB_RAW="https://${cn_gh_proxy}/https://raw.githubusercontent.com"
        GITHUB_CLONE_BASE="https://${cn_gh_proxy}/https://github.com"
    fi

    export UV_INDEX_URL="$PIP_INDEX"
    export UV_DEFAULT_INDEX="$PIP_INDEX"
    export PIP_INDEX_URL="$PIP_INDEX"
    export UV_HTTP_TIMEOUT=300
    export NPM_CONFIG_REGISTRY="$NPM_REGISTRY"

    log "China mirrors ON: pip=${cn_pip##*/}, gh=${cn_gh_proxy:-direct}, docker=${cn_docker##*/}"
}

# ----- Docker path ------------------------------------------------------------
# Pulls the prebuilt image from GHCR when available (~30s) and falls back to
# a local buildx build (~5-15min) on miss. With --with-qq we layer the
# canonical QQ compose overlay on top of the repo's docker-compose.yml
# instead of writing a standalone $PREFIX/corlinman.yml override — that
# guarantees `napcat` comes up on the same network without us re-encoding
# its config here.
install_docker() {
    require docker
    if ! docker compose version >/dev/null 2>&1; then
        die "docker compose v2 plugin required. install Docker Engine 24+."
    fi

    # Configure Docker daemon to use the CN registry mirror, if needed and not
    # already present. Best-effort: a write failure (non-root, exotic distro)
    # just falls back to upstream.
    if [[ -n "$USE_CHINA" && -n "$DOCKER_REGISTRY_MIRROR" ]]; then
        if [[ ! -f /etc/docker/daemon.json ]] || ! grep -q "$DOCKER_REGISTRY_MIRROR" /etc/docker/daemon.json 2>/dev/null; then
            log "registering docker registry mirror $DOCKER_REGISTRY_MIRROR"
            sudo mkdir -p /etc/docker || true
            echo "{\"registry-mirrors\": [\"$DOCKER_REGISTRY_MIRROR\"]}" | sudo tee /etc/docker/daemon.json >/dev/null || \
                warn "failed to write /etc/docker/daemon.json; continuing"
            sudo systemctl restart docker || warn "could not restart docker; continuing"
        fi
    fi

    log "cloning repo (ref=$REF) into $PREFIX"
    sudo mkdir -p "$PREFIX"
    sudo chown -R "$(id -u):$(id -g)" "$PREFIX"
    if [[ -d "$PREFIX/repo/.git" ]]; then
        git -C "$PREFIX/repo" fetch --depth 1 origin "$REF"
        git -C "$PREFIX/repo" checkout "$REF"
        git -C "$PREFIX/repo" reset --hard FETCH_HEAD
    else
        local clone_url="${GITHUB_CLONE_BASE}/${REPO}.git"
        git clone --depth 1 --branch "$REF" "$clone_url" "$PREFIX/repo" \
            || git clone --depth 1 --branch "$REF" "https://github.com/${REPO}.git" "$PREFIX/repo"
    fi

    # Re-lock the root-executed upgrade scripts that the recursive chown above
    # just handed to the unprivileged install user (LPE mitigation, see fn).
    secure_root_executed_scripts

    # --- pull-first, build-on-miss ----------------------------------------
    # Prebuilt images get tagged at `ghcr.io/${REPO}:${REF}` by the release-image
    # workflow (see PLAN_DEPLOY_UX.md task B). Until that workflow ships, every
    # tag will 404 here and we fall through to the local buildx path — which
    # is exactly the legacy behaviour, no breakage.
    local image_ref="ghcr.io/${REPO}:${REF}"
    local pulled=""
    local pull_start pull_end pull_seconds
    log "trying to pull prebuilt image ($image_ref)"
    pull_start=$(date +%s)
    if docker pull "$image_ref" >/dev/null 2>&1; then
        pull_end=$(date +%s)
        pull_seconds=$((pull_end - pull_start))
        log "pulled in ${pull_seconds}s — skipping local build"
        # Tag as corlinman:local so the override compose file's `image:`
        # reference (and the legacy non-QQ standalone override below) keeps
        # working unchanged whether the image came from pull or build.
        docker tag "$image_ref" corlinman:local
        pulled="1"
    else
        warn "no prebuilt image for ref=$REF — building locally (5-15min)"
        local build_start build_end build_seconds
        build_start=$(date +%s)
        local extra_args=()
        if [[ -n "$USE_CHINA" ]]; then
            extra_args+=(
                --build-arg "PIP_INDEX=$PIP_INDEX"
                --build-arg "UV_INDEX_URL=$PIP_INDEX"
                --build-arg "DEBIAN_MIRROR=${DEBIAN_MIRROR:-mirrors.tuna.tsinghua.edu.cn}"
                --build-arg "NPM_REGISTRY=$NPM_REGISTRY"
            )
        fi
        (cd "$PREFIX/repo" && docker buildx build "${extra_args[@]}" \
            -f docker/Dockerfile --target runtime -t corlinman:local --load .)
        build_end=$(date +%s)
        build_seconds=$((build_end - build_start))
        log "built in ${build_seconds}s"
    fi
    # In both branches `corlinman:local` is now a valid local tag, so the
    # compose files (which expect that ref via the override below or via
    # CORLINMAN_TAG=local for the canonical compose) resolve cleanly.

    # --- compose orchestration --------------------------------------------
    # Two paths:
    #   plain : write a custom $PREFIX/corlinman.yml override (legacy
    #           behaviour — respects $DATA_DIR/$PORT/$ENABLE_DOCKER_SANDBOX).
    #   --with-qq : use the repo's canonical docker-compose.yml +
    #               docker-compose.qq.yml so NapCat comes up on the same
    #               network with the right env vars and volume layout.
    if [[ -n "$WITH_QQ" ]]; then
        if [[ "$ENABLE_DOCKER_SANDBOX" == "1" ]]; then
            warn "--enable-docker-sandbox is ignored in --with-qq mode; layer docker/compose/docker-compose.sandbox.yml manually if needed."
        fi
        # The canonical compose file picks the image via ${CORLINMAN_TAG};
        # we just built / pulled and tagged corlinman:local, so we point at
        # that and skip the GHCR roundtrip a second time. CORLINMAN_TAG=local
        # makes `image: ghcr.io/sweetcornna/corlinman:local` — point docker at
        # the matching local tag.
        docker tag corlinman:local "ghcr.io/${REPO}:local" >/dev/null 2>&1 || true

        # Materialise .env if missing so napcat (QQ_*) + corlinman
        # (OPENAI_API_KEY / GEMINI_API_KEY) have something to read at boot.
        local env_path="$PREFIX/repo/.env"
        local env_template="$PREFIX/repo/deploy/.env.template"
        local env_created=""
        if [[ ! -f "$env_path" ]]; then
            if [[ -f "$env_template" ]]; then
                cp "$env_template" "$env_path"
                chmod 600 "$env_path" 2>/dev/null || true
                env_created="1"
                log "materialised .env from deploy/.env.template"
            else
                warn ".env.template not found at $env_template — skipping .env bootstrap"
            fi
        fi
        if [[ -n "$env_created" ]]; then
            cat <<EOF

⚠️  edit $env_path with QQ_* / OPENAI_API_KEY then re-run:
      cd $PREFIX/repo/docker/compose && \\
        CORLINMAN_TAG=local docker compose -f docker-compose.yml -f docker-compose.qq.yml --profile qq up -d

EOF
            return 0
        fi

        log "starting (with-qq overlay)"
        (cd "$PREFIX/repo/docker/compose" && \
            CORLINMAN_TAG=local docker compose \
                -f docker-compose.yml \
                -f docker-compose.qq.yml \
                --profile qq up -d)

        local prefix=""
        wait_for_health || prefix="⚠️  health probe timed out —"
        print_success "docker logs -f corlinman  /  docker logs -f corlinman-napcat" "$prefix"
        cat <<EOF
   napcat WebUI:   http://127.0.0.1:6099 (SSH tunnel from your laptop if remote)
   config ref:     https://github.com/${REPO}/blob/main/docs/config.example.toml
   stop:           cd $PREFIX/repo/docker/compose && docker compose -f docker-compose.yml -f docker-compose.qq.yml --profile qq down
EOF
        return 0
    fi

    # --- legacy standalone override (no --with-qq) -----------------------
    log "writing compose override"
    mkdir -p "$DATA_DIR"
    cat > "$PREFIX/corlinman.yml" <<EOF
services:
  corlinman:
    image: corlinman:local
    container_name: corlinman
    restart: unless-stopped
    ports:
      - "${PORT}:6005"
    volumes:
      - "${DATA_DIR}:/data"
EOF
    if [[ "$ENABLE_DOCKER_SANDBOX" == "1" ]]; then
        warn "mounting /var/run/docker.sock for Docker-backed plugin sandboxing"
        cat >> "$PREFIX/corlinman.yml" <<EOF
      - /var/run/docker.sock:/var/run/docker.sock:ro
EOF
    elif [[ "$ENABLE_ONE_CLICK_UPGRADE" == "1" ]]; then
        # One-click upgrade requires read-write docker.sock so the
        # gateway can pull a new image + recreate its own container
        # via DockerUpgrader. We also need the in-container `corlinman`
        # system user to join the host's `docker` group so it can
        # actually open the socket (default ownership root:docker 660).
        local docker_gid
        docker_gid=$(getent group docker 2>/dev/null | cut -d: -f3 || true)
        if [[ -z "$docker_gid" ]]; then
            warn "host has no 'docker' group — one-click upgrade may fail with EACCES on the socket. Skipping group_add; mount only."
        else
            log "one-click upgrade enabled — adding container user to docker group (gid=$docker_gid)"
        fi
        cat >> "$PREFIX/corlinman.yml" <<EOF
      - /var/run/docker.sock:/var/run/docker.sock
EOF
        if [[ -n "$docker_gid" ]]; then
            cat >> "$PREFIX/corlinman.yml" <<EOF
    group_add:
      - "${docker_gid}"
EOF
        fi
    fi
    cat >> "$PREFIX/corlinman.yml" <<EOF
    environment:
      BIND: 0.0.0.0
      CORLINMAN_DATA_DIR: /data
      CORLINMAN_CONFIG: /data/config.toml
      CORLINMAN_RUNTIME_MODE: docker
EOF

    log "starting"
    (cd "$PREFIX" && docker compose -f corlinman.yml up -d)

    local prefix=""
    wait_for_health || prefix="⚠️  health probe timed out —"
    print_success "docker compose -f $PREFIX/corlinman.yml logs -f" "$prefix"
    cat <<EOF
   config ref:     https://github.com/${REPO}/blob/main/docs/config.example.toml
   stop:           docker compose -f $PREFIX/corlinman.yml down
EOF
}

# ----- Native path ------------------------------------------------------------
install_native() {
    require curl
    require git
    if [[ "$(uname -s)" != "Linux" && "$(uname -s)" != "Darwin" ]]; then
        die "unsupported OS for native mode: $(uname -s)"
    fi

    # Install uv if missing — fast Python package manager, single binary.
    if ! command -v uv >/dev/null 2>&1; then
        log "installing uv"
        if [[ -n "$USE_CHINA" ]]; then
            # Astral installer mirror via ghproxy
            curl -fsSL "${GITHUB_RAW/raw.githubusercontent.com/astral.sh}/uv/install.sh" | sh \
                || curl -fsSL https://astral.sh/uv/install.sh | sh
        else
            curl -fsSL https://astral.sh/uv/install.sh | sh
        fi
        export PATH="$HOME/.local/bin:$PATH"
    fi
    require uv

    log "cloning repo (ref=$REF) into $PREFIX"
    sudo mkdir -p "$PREFIX"
    sudo chown -R "$(id -u):$(id -g)" "$PREFIX"
    if [[ -d "$PREFIX/repo/.git" ]]; then
        git -C "$PREFIX/repo" fetch --depth 1 origin "$REF"
        git -C "$PREFIX/repo" checkout "$REF"
        git -C "$PREFIX/repo" reset --hard FETCH_HEAD
    else
        local clone_url="${GITHUB_CLONE_BASE}/${REPO}.git"
        # Try the (possibly proxied) URL first; if it 404s or hangs, fall back
        # to direct github.com — some CN BGP edges (e.g. Tencent Cloud
        # Tianjin) reach github.com faster than any public proxy.
        git clone --depth 1 --branch "$REF" "$clone_url" "$PREFIX/repo" \
            || git clone --depth 1 --branch "$REF" "https://github.com/${REPO}.git" "$PREFIX/repo"
    fi

    # Re-lock the root-executed upgrade scripts that the recursive chown above
    # just handed to the unprivileged install user (LPE mitigation, see fn).
    secure_root_executed_scripts

    log "uv sync --all-packages (this can take a few minutes on first install)"
    (cd "$PREFIX/repo" && uv sync --all-packages --frozen --no-dev)

    # Build + place the Next.js static export. Honors --skip-ui /
    # CORLINMAN_SKIP_UI for headless deploys. Without this step the
    # gateway serves whatever stale UI happened to be in ui-static/.
    build_and_place_ui

    mkdir -p "$DATA_DIR"

    if [[ "$(uname -s)" == "Linux" ]]; then
        # Create the unprivileged service account and hand it the runtime-
        # writable paths BEFORE writing the unit that runs as it. The gateway
        # owns its DATA_DIR (config.toml, sqlite, uploads) and reads the
        # ui-static export, so both must be readable/writable by SERVICE_USER.
        ensure_service_user
        log "chowning runtime paths to $SERVICE_USER"
        sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR" \
            || warn "could not chown $DATA_DIR to $SERVICE_USER"
        [[ -d "$PREFIX/ui-static" ]] \
            && sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$PREFIX/ui-static" \
            || true

        log "writing systemd unit"
        local uv_path; uv_path="$(command -v uv)"
        sudo tee /etc/systemd/system/corlinman.service >/dev/null <<EOF
[Unit]
Description=corlinman gateway (Python)
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${PREFIX}/repo
ExecStart=${uv_path} run corlinman-gateway --config ${DATA_DIR}/config.toml --port ${PORT}
EnvironmentFile=-${PREFIX}/.env
Environment=CORLINMAN_DATA_DIR=${DATA_DIR}
Environment=CORLINMAN_UI_DIR=${PREFIX}/ui-static
Environment=BIND=0.0.0.0
Environment=PORT=${PORT}
Environment=CORLINMAN_RUNTIME_MODE=native
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

        # --- one-click upgrader helper units (W1.2) -----------------------
        # PathChanged= watcher fires the one-shot upgrader.service whenever
        # the gateway atomically writes $DATA_DIR/.upgrade-request. The
        # upgrader runs as root (it has to call systemctl) but is locked
        # down by:
        #   * exec-only-one-command (corlinman-upgrader.sh)
        #   * strict tag regex + live GitHub releases whitelist in the
        #     script itself
        #   * TimeoutStartSec=600 hard cap
        # See deploy/corlinman-upgrader.sh for the full safety story.
        log "writing one-click upgrader units"
        # PATH explicitly extends systemd's restrictive default so
        # `require uv` (and pnpm lookups in build_and_place_ui) find tools
        # installed under /root/.local/bin or the install user's
        # ~/.local/bin without depending on the operator's interactive
        # shell config. Without this the upgrader silently failed with
        # "required tool 'uv' not on PATH" (see CHANGELOG v1.8.10).
        sudo tee /etc/systemd/system/corlinman-upgrader.service >/dev/null <<EOF
[Unit]
Description=corlinman one-shot upgrader
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
Environment=CORLINMAN_DATA_DIR=${DATA_DIR}
Environment=INSTALL_PREFIX=${PREFIX}
Environment=PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/bin/bash ${PREFIX}/repo/deploy/corlinman-upgrader.sh
StandardOutput=journal
StandardError=journal
SyslogIdentifier=corlinman-upgrader
TimeoutStartSec=600
EOF

        sudo tee /etc/systemd/system/corlinman-upgrader.path >/dev/null <<EOF
[Unit]
Description=Watch for corlinman upgrade requests
After=corlinman-upgrader.service

[Path]
PathChanged=${DATA_DIR}/.upgrade-request
Unit=corlinman-upgrader.service

[Install]
WantedBy=multi-user.target
EOF

        sudo systemctl daemon-reload
        sudo systemctl enable --now corlinman
        sudo systemctl enable --now corlinman-upgrader.path \
            || warn "could not enable corlinman-upgrader.path — one-click upgrade will fall back to copy-paste"
        log "service status: $(systemctl is-active corlinman)"
    fi

    local prefix=""
    wait_for_health || prefix="⚠️  health probe timed out —"
    print_success "journalctl -u corlinman -f" "$prefix"
    cat <<EOF
   config ref:     https://github.com/${REPO}/blob/main/docs/config.example.toml
   manual run:     cd $PREFIX/repo && uv run corlinman-gateway

EOF
}

# ----- Upgrade path (native deployments only) --------------------------------
# Idempotent: pulls REF into the existing repo, refreshes the venv, restarts
# the systemd unit. Never rewrites config.toml or touches $DATA_DIR.
upgrade_native() {
    require git
    require uv
    [[ -d "$PREFIX/repo/.git" ]] \
        || die "no existing native install at $PREFIX/repo — run install.sh without --upgrade for a fresh install"

    log "upgrading $PREFIX/repo to ref=$REF"
    # Shallow fetch of the requested ref into FETCH_HEAD. Tags only land
    # as a local ref when --tags is passed (and even then a shallow clone
    # may not import the tag object), so we never `git checkout <tag>` —
    # we reset --hard FETCH_HEAD directly, which points at whatever the
    # fetched ref resolved to (branch tip, tag commit, or arbitrary SHA).
    git -C "$PREFIX/repo" fetch --depth 1 origin "$REF"
    # Detect the resolved commit before we touch the worktree so the summary
    # at the end can show before/after SHAs.
    local before_sha
    before_sha="$(git -C "$PREFIX/repo" rev-parse --short HEAD)"
    git -C "$PREFIX/repo" reset --hard FETCH_HEAD
    local after_sha
    after_sha="$(git -C "$PREFIX/repo" rev-parse --short HEAD)"

    log "uv sync --frozen (refreshing venv)"
    (cd "$PREFIX/repo" && uv sync --all-packages --frozen --no-dev)

    # Rebuild + place the UI static export. Without this an upgrade
    # leaves $PREFIX/ui-static at the previous version while the Python
    # gateway flips to the new one — every admin page silently drifts.
    # ``--skip-ui`` keeps the legacy behaviour for headless / docker-
    # external-UI deployments.
    build_and_place_ui

    if [[ "$(uname -s)" == "Linux" ]] \
        && [[ -f /etc/systemd/system/corlinman.service ]]; then
        log "restarting corlinman.service"
        sudo systemctl restart corlinman.service
        sudo systemctl is-active corlinman.service || warn "service did not return active"
        # Restart the Python agent too if it's running (post-W5 deployments
        # have a sibling corlinman-agent.service that owns the gRPC plane).
        if [[ -f /etc/systemd/system/corlinman-agent.service ]]; then
            sudo systemctl restart corlinman-agent.service
        fi
    elif [[ "$(uname -s)" == "Linux" ]] \
        && [[ -f /etc/systemd/system/corlinman-gateway.service ]]; then
        # Older deployments registered the unit as corlinman-gateway.service.
        log "restarting corlinman-gateway.service (legacy unit name)"
        sudo systemctl restart corlinman-gateway.service
    else
        warn "no systemd unit found — restart corlinman manually"
    fi

    local prefix=""
    wait_for_health || prefix="⚠️  health probe timed out —"
    print_success "journalctl -u corlinman -f" "$prefix"
    cat <<EOF
   upgraded:       $before_sha → $after_sha (ref=$REF)

EOF
}

# ----- Upgrade path (docker deployments) -------------------------------------
# Sibling of upgrade_native(). Pull the requested ref's prebuilt image from
# GHCR, fall back to a local rebuild on miss, then restart only the
# corlinman service (no --build; image is already swapped). Data volume is
# untouched. With --with-qq compose stacks the napcat sidecar isn't
# refreshed here — rerun install.sh without --upgrade if you also want to
# bounce napcat.
upgrade_docker() {
    require docker
    docker compose version >/dev/null 2>&1 || die "docker compose v2 plugin required"
    [[ -d "$PREFIX/repo/.git" ]] \
        || die "no existing docker install at $PREFIX/repo — run install.sh without --upgrade for a fresh install"

    log "upgrading docker deployment to ref=$REF"
    local before_digest
    before_digest=$(docker inspect corlinman --format '{{.Image}}' 2>/dev/null || echo "<none>")

    # Pull new image; on miss, fetch the new source and buildx locally.
    local image_ref="ghcr.io/${REPO}:${REF}"
    if docker pull "$image_ref"; then
        export CORLINMAN_TAG="${REF}"
        log "pulled $image_ref"
    else
        warn "docker pull failed for $image_ref — rebuilding locally"
        (cd "$PREFIX/repo" && git fetch --depth 1 origin "$REF" && git reset --hard FETCH_HEAD)
        (cd "$PREFIX/repo" && docker buildx build -f docker/Dockerfile --target runtime -t "$image_ref" --load .)
        export CORLINMAN_TAG="${REF}"
    fi

    # Restart only the corlinman service. --no-deps avoids touching napcat /
    # any other sidecar; --build is intentionally omitted so the image we
    # just resolved (pulled or rebuilt) is what comes up.
    (cd "$PREFIX/repo/docker/compose" && docker compose up -d --no-deps corlinman)

    local after_digest
    after_digest=$(docker inspect corlinman --format '{{.Image}}')

    log "image: $before_digest → $after_digest"
    log "data dir: $DATA_DIR (untouched)"

    local prefix=""
    wait_for_health || prefix="⚠️  health probe timed out —"
    print_success "docker logs -f corlinman" "$prefix"
    cat <<EOF
   upgraded:       $before_digest → $after_digest (ref=$REF)

EOF
}

# ----- entry -----------------------------------------------------------------
main() {
    if [[ -n "$UPGRADE_MODE" ]]; then
        # Upgrade path doesn't need the china mirror dance for the venv path
        # (uv already cached most wheels); only the git fetch matters and we
        # still respect $CN_GH_PROXY through the existing remote. Skips
        # preflight too — upgrade_* have their own minimal checks.
        #
        # Auto-detect: native (systemd unit active) > docker (container
        # named `corlinman` exists) > die. We don't fall through to docker
        # on a stopped systemd unit on purpose; a half-deactivated native
        # deploy + a stale docker container would be ambiguous.
        if systemctl is-active --quiet corlinman.service 2>/dev/null \
            || systemctl is-active --quiet corlinman-gateway.service 2>/dev/null; then
            upgrade_native
        elif docker inspect corlinman >/dev/null 2>&1; then
            upgrade_docker
        else
            die "no existing corlinman deployment found (no systemd unit, no docker container named 'corlinman')"
        fi
        return
    fi
    # --with-qq only makes sense in docker mode (NapCat ships as a docker
    # image; the native path has no equivalent).
    if [[ -n "$WITH_QQ" && "$MODE" != "docker" ]]; then
        die "--with-qq requires --mode docker (got --mode=$MODE)"
    fi
    # Fresh install: validate the host BEFORE any side effects (git clone,
    # docker pull, sudo writes) so half-installed leftovers don't survive a
    # missing prerequisite.
    preflight
    autodetect_china
    apply_china_mirrors
    case "$MODE" in
        docker) install_docker ;;
        native) install_native ;;
        *) die "unknown --mode: $MODE (expected: docker | native)" ;;
    esac
}

main "$@"
