#!/usr/bin/env bash
# corlinman developer setup — one-shot bootstrap for a fresh clone.
# Usage: bash scripts/dev-setup.sh
# Idempotent: safe to re-run.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# Color output when stdout is a TTY.
if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
  C_OK="$(tput setaf 2)"
  C_FAIL="$(tput setaf 1)"
  C_RESET="$(tput sgr0)"
else
  C_OK=""
  C_FAIL=""
  C_RESET=""
fi

_prereq_fail=0
ok()   { echo "    [${C_OK}✓${C_RESET}] $*"; }
fail() { echo "    [${C_FAIL}✗${C_RESET}] $*" >&2; _prereq_fail=1; }

echo "==> [1/5] Checking prerequisites"

# git (almost always present, but log it for completeness).
if command -v git >/dev/null 2>&1; then
  ok "git: $(git --version | awk '{print $3}')"
else
  fail "git: not found"
  echo "        install: brew install git (mac) | sudo apt install git (debian)" >&2
fi

# Python ≥ 3.12.
if command -v python3 >/dev/null 2>&1 && \
   python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)" >/dev/null 2>&1; then
  ok "python3: $(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
else
  fail "python3: need >= 3.12"
  echo "        install: brew install python@3.12 (mac) | sudo apt install python3.12 (debian) | https://www.python.org/downloads/" >&2
fi

# Node ≥ 20.
if command -v node >/dev/null 2>&1; then
  _node_ver="$(node -v 2>/dev/null | sed 's/^v//')"
  _node_major="${_node_ver%%.*}"
  if [ -n "${_node_major}" ] && [ "${_node_major}" -ge 20 ] 2>/dev/null; then
    ok "node: ${_node_ver}"
  else
    fail "node: need >= 20 (have ${_node_ver:-unknown})"
    echo "        install: brew install node@20 (mac) | curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash && sudo apt install -y nodejs (debian)" >&2
  fi
else
  fail "node: not found"
  echo "        install: brew install node@20 (mac) | curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash && sudo apt install -y nodejs (debian)" >&2
fi

# uv.
if command -v uv >/dev/null 2>&1; then
  ok "uv: $(uv --version 2>/dev/null | awk '{print $2}')"
else
  fail "uv: not found"
  echo "        install: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
fi

# pnpm (or corepack which can provide pnpm).
if command -v pnpm >/dev/null 2>&1; then
  ok "pnpm: $(pnpm -v 2>/dev/null)"
elif command -v corepack >/dev/null 2>&1; then
  ok "corepack: present (will provide pnpm in step 4)"
else
  fail "pnpm: not found (and no corepack)"
  echo "        install: corepack enable (bundled with Node 20) | npm i -g pnpm" >&2
fi

# protoc.
if command -v protoc >/dev/null 2>&1; then
  ok "protoc: $(protoc --version | awk '{print $2}')"
else
  fail "protoc: not found"
  echo "        install: brew install protobuf (mac) | sudo apt install protobuf-compiler (debian)" >&2
fi

if [ "${_prereq_fail}" -ne 0 ]; then
  echo "" >&2
  echo "prerequisites missing — install the items above and re-run." >&2
  exit 1
fi

echo "==> [2/5] Installing git hooks (core.hooksPath=.git-hooks)"
git config core.hooksPath .git-hooks
chmod +x .git-hooks/pre-commit
echo "    done. FAST_COMMIT=1 bypasses hooks."

echo "==> [3/5] Python env (uv sync --all-packages --dev)"
uv sync --all-packages --dev
echo "    venv at $(uv run python -c 'import sys; print(sys.prefix)')"

echo "==> [4/5] UI deps (pnpm install)"
if ! command -v pnpm >/dev/null 2>&1; then
  if command -v corepack >/dev/null 2>&1; then
    corepack enable
  else
    echo "    pnpm/corepack not found; install Node 20 first" >&2
    exit 1
  fi
fi
pnpm install
echo "    done."

echo "==> [5/5] Generating Python gRPC stubs (scripts/gen-proto.sh)"
chmod +x scripts/gen-proto.sh
bash scripts/gen-proto.sh
echo "    done."

echo ""
echo "==> Health smoke (corlinman doctor)"
uv run corlinman doctor || echo "    doctor reported issues — see above. dev-setup completed but workspace may need attention."

echo ""
echo "corlinman dev-setup complete."
echo "Next: make dev   (or)   uv run corlinman --help   (or)   uv run corlinman-gateway"
