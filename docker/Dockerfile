# corlinman v1.x — pure Python multi-stage Dockerfile.
#
# BuildKit required: RUN steps below use `--mount=type=cache` to share apt /
# uv / pnpm download caches across builds. Set `DOCKER_BUILDKIT=1` or use
# `docker buildx build` (default on modern Docker / in CI via
# docker/build-push-action).
#
# Stages:
#   1. py-builder  → install workspace + deps into a venv with uv.
#   2. ui-builder  → build the Next.js static admin export.
#   3. runtime     → slim Python 3.12 image, copies the prebuilt venv,
#                    bakes the UI static export at /app/ui-static.
#
# Build (no China mirror):
#   docker buildx build -f docker/Dockerfile --target runtime -t corlinman:dev .
#
# Build (China mirror — uses mirrored base images + Tsinghua PyPI):
#   docker buildx build -f docker/Dockerfile --target runtime \
#       --build-arg PYTHON_BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim-bookworm \
#       --build-arg NODE_BASE_IMAGE=docker.m.daocloud.io/library/node:22-bookworm-slim \
#       --build-arg PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
#       --build-arg UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
#       -t corlinman:dev .

ARG PYTHON_BASE_IMAGE=python:3.12-slim-bookworm
ARG NODE_BASE_IMAGE=node:22-bookworm-slim

# ---------------------------------------------------------------------------
# Stage 1: builder — install workspace into /opt/venv
# ---------------------------------------------------------------------------
FROM ${PYTHON_BASE_IMAGE} AS py-builder

ARG PIP_INDEX=https://pypi.org/simple
ARG UV_INDEX_URL=https://pypi.org/simple
ARG UV_HTTP_TIMEOUT=120
ARG DEBIAN_MIRROR=deb.debian.org

# Optional Debian mirror swap (saves minutes in CN networks).
RUN rm -f /etc/apt/sources.list.d/yarn.list /etc/apt/sources.list.d/nodesource.list \
        /etc/apt/sources.list.d/*yarn* /etc/apt/sources.list.d/*node* \
    && if [ "$DEBIAN_MIRROR" != "deb.debian.org" ]; then \
        sed -i "s|deb.debian.org|$DEBIAN_MIRROR|g" /etc/apt/sources.list.d/debian.sources \
            || sed -i "s|deb.debian.org|$DEBIAN_MIRROR|g" /etc/apt/sources.list; \
    fi

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        protobuf-compiler \
        curl \
        ca-certificates \
        git

# Install uv — pinned for reproducibility.
ENV UV_INSTALL_DIR=/usr/local/bin
ENV UV_INDEX_URL=${UV_INDEX_URL}
ENV UV_HTTP_TIMEOUT=${UV_HTTP_TIMEOUT}
ENV PIP_INDEX_URL=${PIP_INDEX}
RUN curl -fsSL https://astral.sh/uv/0.5.30/install.sh | sh

WORKDIR /build

# Copy the workspace files needed for resolution first (cache-friendly).
COPY pyproject.toml uv.lock README.md ./
COPY python ./python
COPY proto ./proto
COPY scripts/gen-proto.sh ./scripts/gen-proto.sh
RUN sed -i 's/\r$//' scripts/gen-proto.sh && chmod +x scripts/gen-proto.sh

# Docker contexts can contain ignored local bytecode directories from packages
# that no longer exist. uv's workspace glob treats every python/packages/*
# directory as a member, so prune directories that do not contain a package
# manifest before resolving the workspace.
RUN find python/packages -mindepth 1 -maxdepth 1 -type d \
        ! -exec test -f "{}/pyproject.toml" \; -print -exec rm -rf {} +

# Sync the gateway package and its explicit runtime dependencies into /opt/venv.
# --frozen ensures we use uv.lock; package-scoped sync keeps local Docker
# rebuilds from depending on unrelated workspace experiments.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --package corlinman-server --frozen --no-dev

# Install proto generation tools into an isolated venv. Keeping these out of
# /opt/venv preserves the package-scoped runtime sync above.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/proto-tools \
    && uv pip install --python /opt/proto-tools/bin/python \
        "grpcio-tools==1.80.0" \
        "protobuf==6.33.6" \
        "ruff==0.15.14"

# Regenerate proto stubs (the generated files are committed but rebuild
# defensively — keeps the image self-consistent even if a stale commit
# slips through).
RUN UV_PROJECT_ENVIRONMENT=/opt/proto-tools \
    GEN_PROTO_UV_RUN_ARGS=--no-sync \
    bash scripts/gen-proto.sh

# ---------------------------------------------------------------------------
# Stage 2: UI builder — produce a static Next.js export in /build/ui/out
# ---------------------------------------------------------------------------
FROM ${NODE_BASE_IMAGE} AS ui-builder

ARG NPM_REGISTRY=

ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0

WORKDIR /build

# Copy manifests first so dependency resolution remains cache-friendly.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY ui/package.json ./ui/package.json

RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    corepack enable \
    && corepack prepare pnpm@10.33.0 --activate \
    && if [ -n "$NPM_REGISTRY" ]; then pnpm config set registry "$NPM_REGISTRY"; fi \
    && pnpm install --frozen-lockfile

COPY ui ./ui

RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    pnpm -C ui build

# ---------------------------------------------------------------------------
# Stage 3: runtime — slim image with just the venv + sources + UI static
# ---------------------------------------------------------------------------
FROM ${PYTHON_BASE_IMAGE} AS runtime

ARG DEBIAN_MIRROR=deb.debian.org
RUN rm -f /etc/apt/sources.list.d/yarn.list /etc/apt/sources.list.d/nodesource.list \
        /etc/apt/sources.list.d/*yarn* /etc/apt/sources.list.d/*node* \
    && if [ "$DEBIAN_MIRROR" != "deb.debian.org" ]; then \
        sed -i "s|deb.debian.org|$DEBIAN_MIRROR|g" /etc/apt/sources.list.d/debian.sources \
            || sed -i "s|deb.debian.org|$DEBIAN_MIRROR|g" /etc/apt/sources.list; \
    fi

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system corlinman \
    && useradd --system --gid corlinman --home-dir /app \
        --no-create-home --shell /usr/sbin/nologin corlinman

# Bring across the prebuilt venv and only the source the venv needs.
# uv's editable .pth files reference absolute paths like
# /build/python/packages/<name>/src ; the runtime stage stores the source
# at /app/python instead, so we add a /build/python -> /app/python
# symlink to keep those .pth references resolvable. Without this the
# venv boots with ``ModuleNotFoundError: corlinman_server`` on every
# console-script entry point.
COPY --from=py-builder /opt/venv /opt/venv
COPY --from=py-builder /build/python /app/python
COPY --from=py-builder /build/proto /app/proto
RUN mkdir -p /build && ln -s /app/python /build/python && ln -s /app/proto /build/proto

# Root workspace pyproject so resolve_app_version() finds the release-spaced
# ``corlinman`` [project].version inside the container (the sub-package
# metadata is NOT bumped per release — see system/app_version.py).
COPY pyproject.toml /app/pyproject.toml

# Release stamp escape hatch (highest precedence in resolve_app_version()).
# Empty by default so local builds fall through to /app/pyproject.toml; CI
# passes the tag ref (release-image.yml). A non-version ref like ``main``
# is ignored by the resolver, so passing github.ref_name is always safe.
ARG CORLINMAN_VERSION=
ENV CORLINMAN_VERSION=${CORLINMAN_VERSION}

# Activate the venv.
ENV PATH=/opt/venv/bin:$PATH
ENV PYTHONUNBUFFERED=1

# UI static export built from this exact source tree.
COPY --from=ui-builder /build/ui/out /app/ui-static

# Default supervisor: launch agent sidecar + gateway in parallel.
COPY docker/start.sh /app/start.sh
RUN mkdir -p /data \
    && sed -i 's/\r$//' /app/start.sh \
    && chmod +x /app/start.sh \
    && chown -R corlinman:corlinman /app /data

EXPOSE 6005
WORKDIR /app

ENV CORLINMAN_DATA_DIR=/data
ENV CORLINMAN_UI_DIR=/app/ui-static
ENV BIND=0.0.0.0
ENV PORT=6005

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:6005/health || exit 1

USER corlinman

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/start.sh"]
