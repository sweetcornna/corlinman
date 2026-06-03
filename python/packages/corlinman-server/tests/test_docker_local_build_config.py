from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]


def test_dockerfile_base_images_are_build_arg_driven() -> None:
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "ARG PYTHON_BASE_IMAGE=python:3.12-slim-bookworm" in dockerfile
    assert "ARG NODE_BASE_IMAGE=node:22-bookworm-slim" in dockerfile
    assert "FROM ${PYTHON_BASE_IMAGE} AS py-builder" in dockerfile
    assert "FROM ${NODE_BASE_IMAGE} AS ui-builder" in dockerfile
    assert "FROM ${PYTHON_BASE_IMAGE} AS runtime" in dockerfile


def test_compose_forwards_local_build_args() -> None:
    compose = (
        REPO_ROOT / "docker" / "compose" / "docker-compose.yml"
    ).read_text(encoding="utf-8")

    assert (
        "PYTHON_BASE_IMAGE: ${PYTHON_BASE_IMAGE:-python:3.12-slim-bookworm}"
        in compose
    )
    assert "NODE_BASE_IMAGE: ${NODE_BASE_IMAGE:-node:22-bookworm-slim}" in compose
    assert "PIP_INDEX: ${PIP_INDEX:-https://pypi.org/simple}" in compose
    assert "UV_INDEX_URL: ${UV_INDEX_URL:-https://pypi.org/simple}" in compose
    assert "NPM_REGISTRY: ${NPM_REGISTRY:-}" in compose
    assert "DEBIAN_MIRROR: ${DEBIAN_MIRROR:-deb.debian.org}" in compose


def test_dockerfile_ignores_third_party_apt_sources_from_broad_base_images() -> None:
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert dockerfile.count("/etc/apt/sources.list.d/yarn.list") == 2
    assert dockerfile.count("/etc/apt/sources.list.d/nodesource.list") == 2


def test_uv_workspace_excludes_non_package_scaffold_dirs() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'exclude = ["python/packages/corlinman-embedding"]' in pyproject


def test_docker_runtime_installs_gateway_package_not_every_workspace_member() -> None:
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "uv sync --package corlinman-server --frozen --no-dev" in dockerfile
    assert "uv sync --all-packages --frozen --no-dev" not in dockerfile
