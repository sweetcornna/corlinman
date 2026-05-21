#!/usr/bin/env bash
# corlinman one-line installer (Python plane, v1.x).
#
# Usage (any one of):
#   curl -fsSL https://raw.githubusercontent.com/ymylive/corlinman/main/deploy/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/ymylive/corlinman/main/deploy/install.sh | bash -s -- --mode docker
#   curl -fsSL https://raw.githubusercontent.com/ymylive/corlinman/main/deploy/install.sh | bash -s -- --mode native
#   curl -fsSL https://raw.githubusercontent.com/ymylive/corlinman/main/deploy/install.sh | bash -s -- --mode native --china
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
#   --version <ref>   Git ref / branch to install from (default: main).
#   --with-qq         Also deploy NapCat (QQ OneBot client) as a Docker
#                     container and wire corlinman's `[channels.qq]` block to
#                     it (OneBot WS :3001, WebUI/scan-login :6099, both bound
#                     to 127.0.0.1). Native mode only. Finish the QQ login
#                     from the admin UI scan flow. CORLINMAN_WITH_QQ=1 also works.
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
REPO="ymylive/corlinman"
USE_CHINA=""
ENABLE_DOCKER_SANDBOX="${CORLINMAN_ENABLE_DOCKER_SANDBOX:-}"
WITH_QQ="${CORLINMAN_WITH_QQ:-}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode) MODE="$2"; shift 2 ;;
        --mode=*) MODE="${1#--mode=}"; shift ;;
        --version) REF="$2"; shift 2 ;;
        --version=*) REF="${1#--version=}"; shift ;;
        --china) USE_CHINA="1"; shift ;;
        --with-qq) WITH_QQ="1"; shift ;;
        --enable-docker-sandbox) ENABLE_DOCKER_SANDBOX="1"; shift ;;
        -h|--help)
            head -48 "$0" | sed -n '2,$p' | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "unknown argument: $1" >&2; exit 1 ;;
    esac
done

log()  { printf "\033[1;34m==>\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m!\033[0m %s\n" "$*" >&2; }
die()  { printf "\033[1;31m✗\033[0m %s\n" "$*" >&2; exit 1; }
require() { command -v "$1" >/dev/null 2>&1 || die "required tool '$1' not on PATH"; }

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

    log "building image"
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
    fi
    cat >> "$PREFIX/corlinman.yml" <<EOF
    environment:
      BIND: 0.0.0.0
      CORLINMAN_DATA_DIR: /data
      CORLINMAN_CONFIG: /data/config.toml
EOF

    log "starting"
    (cd "$PREFIX" && docker compose -f corlinman.yml up -d)

    cat <<EOF

✅ corlinman running at http://localhost:${PORT}
   open http://localhost:${PORT}/onboard to walk the 4-step wizard.
   logs: docker compose -f $PREFIX/corlinman.yml logs -f
   stop: docker compose -f $PREFIX/corlinman.yml down
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

    log "uv sync --all-packages (this can take a few minutes on first install)"
    (cd "$PREFIX/repo" && uv sync --all-packages --frozen --no-dev)

    mkdir -p "$DATA_DIR"

    if [[ "$(uname -s)" == "Linux" ]]; then
        log "writing systemd unit"
        local uv_path; uv_path="$(command -v uv)"
        sudo tee /etc/systemd/system/corlinman.service >/dev/null <<EOF
[Unit]
Description=corlinman gateway (Python)
After=network.target

[Service]
Type=simple
WorkingDirectory=${PREFIX}/repo
ExecStart=${uv_path} run corlinman-gateway --config ${DATA_DIR}/config.toml --port ${PORT}
Environment=CORLINMAN_DATA_DIR=${DATA_DIR}
Environment=BIND=0.0.0.0
Environment=PORT=${PORT}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
        sudo systemctl daemon-reload
        sudo systemctl enable --now corlinman
        log "service status: $(systemctl is-active corlinman)"
    fi

    cat <<EOF

✅ corlinman installed under $PREFIX/repo
   data dir: $DATA_DIR
   gateway port: $PORT
   open: http://localhost:${PORT}/onboard
   manual run: cd $PREFIX/repo && uv run corlinman-gateway

EOF
}

# ----- NapCat / QQ channel (--with-qq) ---------------------------------------
# Deploys the NapCat QQ client as a Docker container and wires corlinman's
# `[channels.qq]` block to it. NapCat's OneBot v11 WS server is pinned to
# :3001 via the image's built-in `MODE=ws` template; the WebUI + scan-login
# API sit on :6099. Both are published to 127.0.0.1 only — the corlinman
# admin UI drives the QR scan-login over them. `self_ids` stays empty until
# the operator completes that scan (corlinman fills it in afterwards).
install_napcat() {
    require docker
    if [[ "$MODE" != "native" ]]; then
        warn "--with-qq targets --mode native; with --mode docker, join NapCat to"
        warn "the corlinman container network by hand. Skipping QQ auto-wiring."
        return 0
    fi
    local napcat_dir="${PREFIX}/napcat"
    log "deploying NapCat container (QQ OneBot client)"
    mkdir -p "$napcat_dir/app" "$napcat_dir/config" "$napcat_dir/ntqq"
    docker rm -f corlinman-napcat >/dev/null 2>&1 || true
    docker run -d --name corlinman-napcat --restart unless-stopped \
        -p 127.0.0.1:6099:6099 \
        -p 127.0.0.1:3001:3001 \
        -e NAPCAT_UID=1000 -e NAPCAT_GID=1000 -e TZ="${TZ:-Asia/Shanghai}" \
        -e MODE=ws \
        -v "$napcat_dir/app:/app/napcat" \
        -v "$napcat_dir/config:/app/napcat/config" \
        -v "$napcat_dir/ntqq:/app/.config/QQ" \
        mlikiowa/napcat-docker:latest \
        || die "NapCat container failed to start"

    # NapCat writes its WebUI token to config/webui.json on first boot;
    # corlinman needs it to drive the scan-login API.
    log "waiting for NapCat to write its WebUI token"
    local token="" i
    for i in $(seq 1 25); do
        sleep 2
        token=$(docker exec corlinman-napcat sh -c 'cat /app/napcat/config/webui.json 2>/dev/null' \
            | grep -oE '"token"[[:space:]]*:[[:space:]]*"[^"]+"' | head -1 \
            | sed -E 's/.*"([^"]+)"[[:space:]]*$/\1/')
        [[ -n "$token" ]] && break
    done
    [[ -n "$token" ]] || warn "could not read NapCat WebUI token — set [channels.qq].napcat_access_token by hand"

    # Wire corlinman's QQ channel. Append only if absent so re-runs stay idempotent.
    local cfg="${DATA_DIR}/config.toml"
    mkdir -p "$DATA_DIR"
    touch "$cfg"
    if grep -qE '^\[channels\.qq\]' "$cfg" 2>/dev/null; then
        log "[channels.qq] already in $cfg — leaving as-is"
    else
        log "writing [channels.qq] -> $cfg"
        cat >> "$cfg" <<EOF

[channels.qq]
enabled = true
# self_ids is auto-filled by corlinman once the QQ scan-login completes.
self_ids = []
ws_url = "ws://127.0.0.1:3001"
napcat_url = "http://127.0.0.1:6099"
napcat_access_token = "${token}"

[channels.qq.reply]
on_at_mention = true
on_direct_message = true
EOF
    fi

    if command -v systemctl >/dev/null 2>&1; then
        systemctl restart corlinman 2>/dev/null || true
    fi

    cat <<EOF

✅ NapCat deployed (container: corlinman-napcat — OneBot WS :3001, WebUI :6099)
   Finish QQ login from the corlinman admin UI -> Channels -> QQ -> scan-login.
   NapCat state persists under ${napcat_dir}.
EOF
}

# ----- entry -----------------------------------------------------------------
main() {
    autodetect_china
    apply_china_mirrors
    case "$MODE" in
        docker) install_docker ;;
        native) install_native ;;
        *) die "unknown --mode: $MODE (expected: docker | native)" ;;
    esac
    if [[ -n "$WITH_QQ" ]]; then
        install_napcat
    fi
}

main "$@"
