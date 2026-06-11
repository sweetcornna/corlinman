import os
import shutil
import subprocess
import textwrap
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


def test_dockerfile_normalizes_shell_scripts_for_windows_checkouts() -> None:
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "sed -i 's/\\r$//' scripts/gen-proto.sh" in dockerfile
    assert "sed -i 's/\\r$//' /app/start.sh" in dockerfile


def test_docker_proto_generation_uses_tool_venv_without_workspace_sync() -> None:
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    gen_proto = (REPO_ROOT / "scripts" / "gen-proto.sh").read_text(
        encoding="utf-8"
    )

    assert "uv venv /opt/proto-tools" in dockerfile
    assert "UV_PROJECT_ENVIRONMENT=/opt/proto-tools" in dockerfile
    assert "GEN_PROTO_UV_RUN_ARGS=--no-sync" in dockerfile
    assert "RUN bash scripts/gen-proto.sh" not in dockerfile
    assert "GEN_PROTO_UV_RUN_ARGS" in gen_proto
    assert "uv_run_quiet() {" in gen_proto
    assert "uv_run_quiet python -m grpc_tools.protoc" in gen_proto
    assert "uv_run_quiet python -" in gen_proto
    assert "uv_run_quiet ruff format --isolated" in gen_proto


def test_gen_proto_default_path_handles_empty_optional_uv_args(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    script_dir = repo / "scripts"
    proto_dir = repo / "proto" / "corlinman" / "v1"
    bin_dir = tmp_path / "bin"
    script_dir.mkdir(parents=True)
    proto_dir.mkdir(parents=True)
    bin_dir.mkdir()

    shutil.copy2(
        REPO_ROOT / "scripts" / "gen-proto.sh",
        script_dir / "gen-proto.sh",
    )
    (proto_dir / "agent.proto").write_text(
        'syntax = "proto3"; package corlinman.v1;\n',
        encoding="utf-8",
    )
    (bin_dir / "uv").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            [[ "${1:-}" == "run" ]]
            shift
            for arg in "$@"; do
              case "$arg" in
                --python_out=*)
                  out="${arg#--python_out=}"
                  mkdir -p "$out/corlinman/v1"
                  touch "$out/corlinman/v1/agent_pb2.py"
                  touch "$out/corlinman/v1/agent_pb2.pyi"
                  ;;
              esac
            done
            """
        ),
        encoding="utf-8",
    )
    (bin_dir / "uv").chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env.pop("GEN_PROTO_UV_RUN_ARGS", None)
    env.pop("GEN_PROTO_SKIP_FORMAT", None)

    subprocess.run(
        ["bash", "scripts/gen-proto.sh"],
        cwd=repo,
        env=env,
        check=True,
    )


def test_docker_selective_install_includes_scheduler_runtime_dependencies() -> None:
    server_pyproject = (
        REPO_ROOT / "python" / "packages" / "corlinman-server" / "pyproject.toml"
    ).read_text(encoding="utf-8")

    assert '"corlinman-evolution-engine"' in server_pyproject
    assert '"corlinman-shadow-tester"' in server_pyproject
    assert "corlinman-evolution-engine = { workspace = true }" in server_pyproject
    assert "corlinman-shadow-tester = { workspace = true }" in server_pyproject
