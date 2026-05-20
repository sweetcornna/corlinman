# corlinman v1.x — pure Python multi-stage Dockerfile.
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
# Build (China mirror — uses Tsinghua PyPI + ghproxy):
#   docker buildx build -f docker/Dockerfile --target runtime \
#       --build-arg PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
#       --build-arg UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
#       -t corlinman:dev .

# ---------------------------------------------------------------------------
# Stage 1: builder — install workspace into /opt/venv
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS py-builder

ARG PIP_INDEX=https://pypi.org/simple
ARG UV_INDEX_URL=https://pypi.org/simple
ARG DEBIAN_MIRROR=deb.debian.org

# Optional Debian mirror swap (saves minutes in CN networks).
RUN if [ "$DEBIAN_MIRROR" != "deb.debian.org" ]; then \
        sed -i "s|deb.debian.org|$DEBIAN_MIRROR|g" /etc/apt/sources.list.d/debian.sources \
            || sed -i "s|deb.debian.org|$DEBIAN_MIRROR|g" /etc/apt/sources.list; \
    fi

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        protobuf-compiler \
        curl \
        ca-certificates \
        git \
    && rm -rf /var/lib/apt/lists/*

# Install uv — pinned for reproducibility.
ENV UV_INSTALL_DIR=/usr/local/bin
ENV UV_INDEX_URL=${UV_INDEX_URL}
ENV PIP_INDEX_URL=${PIP_INDEX}
RUN curl -fsSL https://astral.sh/uv/0.5.30/install.sh | sh

WORKDIR /build

# Copy the workspace files needed for resolution first (cache-friendly).
COPY pyproject.toml uv.lock README.md ./
COPY python ./python
COPY proto ./proto
COPY scripts/gen-proto.sh ./scripts/gen-proto.sh

# Sync the entire workspace into /opt/venv. --frozen ensures we use uv.lock.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
RUN uv sync --all-packages --frozen --no-dev

# Regenerate proto stubs (the generated files are committed but rebuild
# defensively — keeps the image self-consistent even if a stale commit
# slips through).
RUN bash scripts/gen-proto.sh

# ---------------------------------------------------------------------------
# Stage 2: UI builder — produce a static Next.js export in /build/ui/out
# ---------------------------------------------------------------------------
FROM node:22-bookworm-slim AS ui-builder

ARG NPM_REGISTRY=

ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0

WORKDIR /build

# Copy manifests first so dependency resolution remains cache-friendly.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY ui/package.json ./ui/package.json

RUN corepack enable \
    && corepack prepare pnpm@10.33.0 --activate \
    && if [ -n "$NPM_REGISTRY" ]; then pnpm config set registry "$NPM_REGISTRY"; fi \
    && pnpm install --frozen-lockfile

COPY ui ./ui

RUN pnpm -C ui build

# ---------------------------------------------------------------------------
# Stage 3: runtime — slim image with just the venv + sources + UI static
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ARG DEBIAN_MIRROR=deb.debian.org
RUN if [ "$DEBIAN_MIRROR" != "deb.debian.org" ]; then \
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

# Activate the venv.
ENV PATH=/opt/venv/bin:$PATH
ENV PYTHONUNBUFFERED=1

# UI static export built from this exact source tree.
COPY --from=ui-builder /build/ui/out /app/ui-static

# Default supervisor: launch agent sidecar + gateway in parallel.
COPY docker/start.sh /app/start.sh
RUN mkdir -p /data \
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
