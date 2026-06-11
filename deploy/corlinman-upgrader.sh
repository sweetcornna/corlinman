#!/usr/bin/env bash
# corlinman one-shot privileged upgrader.
#
# Triggered by corlinman-upgrader.path watching
# $CORLINMAN_DATA_DIR/.upgrade-request. Runs as root (User=root in the
# service unit) so it can invoke install.sh and systemctl. The blast
# radius is constrained to:
#
#   * The target tag MUST exist in https://api.github.com/repos/sweetcornna/corlinman/releases
#   * The tag MUST match a strict semver regex (no `;`, `&&`, shell metas)
#   * The target tag MUST be > current installed version (no downgrade)
#     unless UPGRADER_ALLOW_DOWNGRADE=1 is exported (CI / emergency
#     rollback path).
#
# Inputs the script does NOT trust:
#   * The JSON request file contents — every field is regex-validated
#     before use.
#   * The tag — even after JSON validation, it gets curl-checked against
#     the live GitHub release list.
#
# Inputs it trusts:
#   * $CORLINMAN_DATA_DIR env (set by the service unit)
#   * $INSTALL_PREFIX env (set by the service unit)
#   * The install.sh at $INSTALL_PREFIX/repo/deploy/install.sh is the
#     canonical upgrade path.
#
# Idempotent and re-runnable for debugging: running it manually with a
# hand-crafted request file MUST work.

set -euo pipefail

DATA_DIR="${CORLINMAN_DATA_DIR:-/opt/corlinman/data}"
INSTALL_PREFIX="${INSTALL_PREFIX:-/opt/corlinman}"
REPO_OWNER="${UPGRADER_REPO_OWNER:-sweetcornna}"
REPO_NAME="${UPGRADER_REPO_NAME:-corlinman}"

# When invoked from corlinman-upgrader.service (User=root, systemd's
# restrictive default PATH) install.sh's `require uv` died because uv
# lives at /root/.local/bin/uv. Older units (≤ v1.8.9) shipped without
# the PATH override now emitted by install.sh; this in-script probe
# patches them at runtime so the helper doesn't need a unit rewrite to
# recover. Idempotent for newer units where PATH is already set.
for _path_candidate in \
    /root/.local/bin \
    "${INSTALL_PREFIX}/.local/bin" \
    /usr/local/lib/node_modules/.bin; do
    if [[ -d "$_path_candidate" ]]; then
        case ":$PATH:" in
            *":$_path_candidate:"*) ;;
            *) PATH="${_path_candidate}:${PATH}" ;;
        esac
    fi
done
for _path_candidate in /home/*/.local/bin; do
    [[ -d "$_path_candidate" ]] || continue
    case ":$PATH:" in
        *":$_path_candidate:"*) ;;
        *) PATH="${_path_candidate}:${PATH}" ;;
    esac
done
unset _path_candidate
export PATH

REQUEST_FILE="${DATA_DIR}/.upgrade-request"
STATUS_FILE="${DATA_DIR}/.upgrade-status"
PROCESSED_FILE="${DATA_DIR}/.upgrade-request.processed"
LOG_FILE="${UPGRADER_LOG_FILE:-/tmp/corlinman-upgrader.log}"
INSTALL_SH="${INSTALL_PREFIX}/repo/deploy/install.sh"

# Strict regexes — bash's =~ uses ERE. The leading "v" is optional on
# input: gateways < v1.20.1 wrote the update checker's *stripped*
# display form ("1.20.0") into the request file; we canonicalize back
# to the release tag form right after validation (see below).
TAG_REGEX='^v?[0-9]+\.[0-9]+\.[0-9]+(-[a-z0-9.-]+)?$'
UUID_REGEX='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'

now_ms() {
    # date +%N is non-portable on macOS; pure python3 is the easiest
    # cross-platform path. Falls back to seconds*1000 if python3 absent
    # (the script runs on Linux systemd so python3 is always available
    # after `uv` install, but the fallback keeps the script bootstrappable).
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import time; print(int(time.time()*1000))'
    else
        echo $(($(date +%s) * 1000))
    fi
}

# Write a status JSON atomically. Usage:
#   write_status <request_id> <state> [error] [log_excerpt_file]
# The status file always contains a single JSON object the gateway can
# poll. We rewrite the whole file on every transition (no append) — the
# polling reader expects a single fully-valid JSON document.
write_status() {
    local request_id="$1"
    local state="$2"
    local error="${3:-}"
    local log_excerpt_file="${4:-}"

    local started_at finished_at
    started_at="${UPGRADER_STARTED_AT:-$(now_ms)}"
    finished_at=""
    case "$state" in
        succeeded|failed|stalled) finished_at=$(now_ms) ;;
    esac

    local log_excerpt=""
    if [[ -n "$log_excerpt_file" && -f "$log_excerpt_file" ]]; then
        # jq -Rs converts file contents to a JSON string (escapes,
        # newlines preserved). Cap at 4 KiB so the status file stays
        # under any reasonable inotify/read budget.
        log_excerpt=$(tail -c 4096 "$log_excerpt_file" | jq -Rs .)
    fi

    local tmp="${STATUS_FILE}.tmp.$$"
    jq -n \
        --arg request_id "$request_id" \
        --arg state "$state" \
        --arg error "$error" \
        --arg started_at "$started_at" \
        --arg finished_at "$finished_at" \
        --argjson log_excerpt "${log_excerpt:-null}" \
        '{
            request_id: $request_id,
            state: $state,
            error: (if $error == "" then null else $error end),
            started_at: ($started_at | tonumber),
            finished_at: (if $finished_at == "" then null else ($finished_at | tonumber) end),
            log_excerpt: $log_excerpt
        }' >"$tmp"
    mv -f "$tmp" "$STATUS_FILE"
}

fail() {
    local request_id="$1"
    local error="$2"
    local log_file="${3:-}"
    write_status "$request_id" "failed" "$error" "$log_file"
    # Move the request out of the watched path so the .path unit doesn't
    # refire on every reboot.
    if [[ -f "$REQUEST_FILE" ]]; then
        mv -f "$REQUEST_FILE" "$PROCESSED_FILE" || true
    fi
    exit 1
}

# ---------------------------------------------------------------------------
# 1. Sanity-check: request file present.
# ---------------------------------------------------------------------------
if [[ ! -f "$REQUEST_FILE" ]]; then
    # The path unit can fire on phantom events (e.g. systemd restart);
    # exit cleanly so it doesn't go into a tight failure loop.
    echo "no upgrade request at $REQUEST_FILE — nothing to do" >&2
    exit 0
fi

# ---------------------------------------------------------------------------
# 2. jq is non-optional.
# ---------------------------------------------------------------------------
if ! command -v jq >/dev/null 2>&1; then
    echo "fatal: jq is required for corlinman-upgrader.sh; install jq" >&2
    # We can't write a status file without jq either, so just bail.
    exit 2
fi

# ---------------------------------------------------------------------------
# 3. Parse + validate request JSON.
# ---------------------------------------------------------------------------
REQUEST_JSON=$(cat "$REQUEST_FILE")
if ! echo "$REQUEST_JSON" | jq -e . >/dev/null 2>&1; then
    echo "fatal: $REQUEST_FILE is not valid JSON" >&2
    # Move it aside so we don't re-fire on the same garbage.
    mv -f "$REQUEST_FILE" "$PROCESSED_FILE" || true
    exit 1
fi

REQUEST_ID=$(echo "$REQUEST_JSON" | jq -r '.request_id // empty')
TAG=$(echo "$REQUEST_JSON" | jq -r '.tag // empty')
REQUESTED_BY=$(echo "$REQUEST_JSON" | jq -r '.requested_by // empty')

if [[ -z "$REQUEST_ID" || ! "$REQUEST_ID" =~ $UUID_REGEX ]]; then
    echo "fatal: request_id missing or malformed: ${REQUEST_ID:-<empty>}" >&2
    mv -f "$REQUEST_FILE" "$PROCESSED_FILE" || true
    exit 1
fi

if [[ -z "$TAG" || ! "$TAG" =~ $TAG_REGEX ]]; then
    # tag_invalid is the canonical error keyword the tests look for.
    fail "$REQUEST_ID" "tag_invalid"
fi

# Canonicalize to the GitHub release tag form: tag_name on the releases
# API carries a leading "v" (v1.20.0), and install.sh fetches that ref
# verbatim. Idempotent for already-prefixed tags.
TAG="v${TAG#v}"

# Capture the start time so write_status can stamp the same value across
# every transition.
UPGRADER_STARTED_AT=$(now_ms)
export UPGRADER_STARTED_AT

# ---------------------------------------------------------------------------
# 4. Mark running.
# ---------------------------------------------------------------------------
write_status "$REQUEST_ID" "running"

# ---------------------------------------------------------------------------
# 5. Validate tag against live GitHub release whitelist.
# ---------------------------------------------------------------------------
# Skip in test mode (so unit tests don't hit the network). When the
# UPGRADER_SKIP_TAG_CHECK env is set we trust the regex alone.
if [[ -z "${UPGRADER_SKIP_TAG_CHECK:-}" ]]; then
    RELEASES_URL="https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/releases?per_page=100"
    # ``-L`` matters: GitHub 301s old owner paths after a transfer/rename
    # and without it curl returns the redirect-stub JSON body, which jq
    # tries to iterate as an array and dies with "Cannot iterate over
    # object" — surfacing as ``tag_not_in_releases`` even though the tag
    # exists at the new location.
    if ! curl -fsSL -m 15 "$RELEASES_URL" \
            | jq -e --arg t "$TAG" '.[] | select(.tag_name==$t)' >/dev/null 2>&1; then
        fail "$REQUEST_ID" "tag_not_in_releases"
    fi
fi

# ---------------------------------------------------------------------------
# 6. No-downgrade check (best-effort).
# ---------------------------------------------------------------------------
# We strip the leading "v" off both sides before comparing. The
# comparison is via `sort -V` (GNU sort version-sort) which handles
# semver correctly. macOS BSD sort lacks -V — but this script only runs
# from a systemd unit on Linux, so that's fine.
if [[ "${UPGRADER_ALLOW_DOWNGRADE:-0}" != "1" ]]; then
    PYTHON_BIN="${INSTALL_PREFIX}/repo/.venv/bin/python"
    if [[ -x "$PYTHON_BIN" ]]; then
        # The `try/except` lives in a here-doc style -c argument so we
        # can write real Python (not a one-liner). Empty string on
        # PackageNotFoundError keeps the downgrade gate fail-open: if
        # we can't determine current version, we don't block.
        CURRENT_VERSION=$("$PYTHON_BIN" - <<'PYEOF' 2>/dev/null || echo ""
from importlib.metadata import version, PackageNotFoundError
try:
    print(version('corlinman-server'))
except PackageNotFoundError:
    print('')
PYEOF
)
    else
        CURRENT_VERSION=""
    fi

    if [[ -n "$CURRENT_VERSION" ]]; then
        TARGET_STRIPPED="${TAG#v}"
        # printf + sort -V: if the highest sorted value isn't the target,
        # the target is <= current → refuse.
        HIGHEST=$(printf "%s\n%s\n" "$CURRENT_VERSION" "$TARGET_STRIPPED" \
            | sort -V | tail -n 1)
        if [[ "$HIGHEST" != "$TARGET_STRIPPED" || "$CURRENT_VERSION" == "$TARGET_STRIPPED" ]]; then
            fail "$REQUEST_ID" "downgrade_refused"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 7. Call install.sh --upgrade --version <tag>.
# ---------------------------------------------------------------------------
if [[ ! -x "$INSTALL_SH" && ! -f "$INSTALL_SH" ]]; then
    fail "$REQUEST_ID" "install_sh_missing"
fi

# Truncate the log file so we only capture this run's output.
: >"$LOG_FILE"

INSTALL_EXIT=0
# Combine stdout+stderr, tee to a file (capped at 4 KiB on read in
# write_status), and don't let pipefail mask install.sh's exit code.
bash "$INSTALL_SH" --upgrade --version "$TAG" >>"$LOG_FILE" 2>&1 || INSTALL_EXIT=$?

# ---------------------------------------------------------------------------
# 8/9. Write terminal status.
# ---------------------------------------------------------------------------
if [[ "$INSTALL_EXIT" -eq 0 ]]; then
    write_status "$REQUEST_ID" "succeeded" "" "$LOG_FILE"
    # Success: remove the request entirely (and the processed marker).
    rm -f "$REQUEST_FILE" "$PROCESSED_FILE" 2>/dev/null || true
    exit 0
else
    write_status "$REQUEST_ID" "failed" "install_sh_exit_${INSTALL_EXIT}" "$LOG_FILE"
    # Failure: move the request out of the way so the path unit doesn't
    # refire on the same broken request indefinitely.
    mv -f "$REQUEST_FILE" "$PROCESSED_FILE" || true
    exit 1
fi
