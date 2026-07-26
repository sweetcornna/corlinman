from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[6]
INSTALL_SH = REPO_ROOT / "deploy" / "install.sh"
COMPOSE_QQ = REPO_ROOT / "docker" / "compose" / "docker-compose.qq.yml"


def _heredoc(text: str, anchor: str) -> str:
    for body in re.findall(r"<<EOF\n(.*?)\nEOF\n", text, flags=re.DOTALL):
        if anchor in body:
            return body
    raise AssertionError(f"missing heredoc containing {anchor!r}")


def test_native_template_is_instance_isolated_and_hardened() -> None:
    unit = _heredoc(INSTALL_SH.read_text(encoding="utf-8"), "managed NapCat instance %i")

    assert "corlinman-napcat@.service" not in unit
    assert "WorkingDirectory=${DATA_DIR}/.napcat/managed/instances/%i/runtime" in unit
    assert "Environment=HOME=${DATA_DIR}/.napcat/managed/instances/%i/runtime" in unit
    assert "EnvironmentFile=${DATA_DIR}/.napcat/managed/instances/%i/manager-secrets.env" in unit
    assert "EnvironmentFile=${DATA_DIR}/.napcat/managed/instances/%i/runtime.env" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "ReadWritePaths=${DATA_DIR}/.napcat/managed/instances/%i/runtime" in unit


def test_native_gateway_uses_manager_socket_not_root_commands() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    gateway = _heredoc(text, "corlinman-gateway --config")
    agent = _heredoc(text, "corlinman agent execution plane")
    manager = _heredoc(text, "managed NapCat lifecycle helper")

    assert "CORLINMAN_NAPCAT_MANAGER_SOCKET=/run/corlinman-napcat/manager.sock" in gateway
    assert "${qq_env}" in gateway
    assert "Environment=CORLINMAN_CHAT_BACKEND=grpc_agent" in text
    assert "User=${AGENT_USER}" in agent
    assert "CORLINMAN_NAPCAT_MANAGER_SOCKET" not in agent
    assert "CORLINMAN_PY_SOCKET=/run/corlinman-agent/agent.sock" in agent
    assert "CORLINMAN_PY_CONFIG=/run/corlinman-agent/py-config.json" in text
    assert "CORLINMAN_PY_CONFIG=/run/corlinman-agent/py-config.json" in agent
    assert "CORLINMAN_DATA_DIR=" not in agent
    assert "CORLINMAN_EXECUTION_STATE_DIR=${EXECUTION_STATE_DIR}" in gateway
    assert "CORLINMAN_EXECUTION_STATE_DIR=${EXECUTION_STATE_DIR}" in agent
    assert "WorkingDirectory=${EXECUTION_STATE_DIR}" in agent
    assert "ReadWritePaths=${EXECUTION_STATE_DIR} /run/corlinman-agent" in agent
    assert "ReadWritePaths=${DATA_DIR}" not in agent
    assert "User=root" in manager
    assert "Group=${NAPCAT_CLIENT_GROUP}" in manager
    assert (
        "SupplementaryGroups=${NAPCAT_CLIENT_GROUP} ${AGENT_USER} ${EXECUTION_GROUP}"
        in gateway
    )
    assert "SupplementaryGroups=${EXECUTION_GROUP}" in agent
    assert "--allowed-uid ${service_uid}" in manager
    assert "--socket-gid ${manager_socket_gid}" in manager
    assert "CORLINMAN_NAPCAT_RUNTIME_UID=${service_uid}" in manager
    assert "CORLINMAN_NAPCAT_RUNTIME_GID=${service_gid}" in manager
    assert "RuntimeDirectory=corlinman-napcat" in manager


def test_native_data_root_is_gateway_only() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    ownership = text[text.index("chown_runtime_paths()") : text.index("# ----- PATH")]

    assert 'sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"' in ownership
    assert 'sudo chmod 0750 "$DATA_DIR"' in ownership
    assert "$DATA_DIR/py-config.json" not in ownership
    assert 'chgrp -R "$AGENT_USER" "$DATA_DIR"' not in ownership


def test_docker_socket_is_mounted_only_into_manager_sidecar() -> None:
    payload = yaml.safe_load(COMPOSE_QQ.read_text(encoding="utf-8"))
    services = payload["services"]
    manager_volumes = services["napcat-manager"]["volumes"]
    gateway_volumes = services["corlinman"]["volumes"]
    agent_volumes = services["corlinman-agent"]["volumes"]

    assert any("/var/run/docker.sock" in value for value in manager_volumes)
    assert all("/var/run/docker.sock" not in value for value in gateway_volumes)
    assert all("/var/run/docker.sock" not in value for value in agent_volumes)
    assert services["napcat-manager"]["user"] == "0:0"
    assert "manager.sock" in services["napcat-manager"]["command"][-1]
    assert "--allowed-uid" in services["napcat-manager"]["command"][-1]
    assert "--socket-gid" in services["napcat-manager"]["command"][-1]
    assert services["napcat"]["ports"] == ["127.0.0.1:6099:6099"]
    assert services["napcat"]["expose"] == ["3001"]


def test_docker_agent_cannot_open_manager_socket_or_inherit_env_secrets() -> None:
    payload = yaml.safe_load(COMPOSE_QQ.read_text(encoding="utf-8"))
    services = payload["services"]
    gateway = services["corlinman"]
    agent = services["corlinman-agent"]

    assert gateway["environment"]["CORLINMAN_PROCESS_ROLE"] == "gateway"
    assert gateway["environment"]["CORLINMAN_CHAT_BACKEND"] == "grpc_agent"
    assert agent["environment"]["CORLINMAN_PROCESS_ROLE"] == "agent"
    assert agent["environment"]["CORLINMAN_PY_SOCKET"] == (
        "/run/corlinman-agent/socket/agent.sock"
    )
    assert "CORLINMAN_PY_ADDR" not in agent["environment"]
    assert gateway["environment"]["CORLINMAN_PY_SOCKET"] == (
        "/run/corlinman-agent/socket/agent.sock"
    )
    assert gateway["environment"]["CORLINMAN_PY_CONFIG"] == (
        "/run/corlinman-agent/config/py-config.json"
    )
    assert gateway["environment"]["CORLINMAN_PY_CONFIG_GID"] == "10004"
    assert agent["environment"]["CORLINMAN_PY_CONFIG"] == (
        "/run/corlinman-agent/config/py-config.json"
    )
    assert agent["user"] == "10002:10002"
    assert agent["group_add"] == ["10003", "10004"]
    assert all("napcat-manager-socket" not in value for value in agent["volumes"])
    assert "env_file" not in agent
    assert "CORLINMAN_NAPCAT_MANAGER_SOCKET" not in agent["environment"]
    manager_volumes = services["napcat-manager"]["volumes"]
    agent_volumes = agent["volumes"]
    assert "napcat-manager-state:/data" in manager_volumes
    assert all("napcat-manager-state" not in value for value in agent_volumes)
    assert "~/.corlinman:/data" not in agent_volumes
    assert "execution-state:/execution-state" in agent_volumes
    assert "execution-state:/execution-state" in gateway["volumes"]
    assert agent["environment"]["CORLINMAN_EXECUTION_STATE_DIR"] == "/execution-state"
    assert gateway["environment"]["CORLINMAN_EXECUTION_STATE_DIR"] == "/execution-state"
    assert "CORLINMAN_DATA_DIR" not in agent["environment"]
    assert "agent-socket:/run/corlinman-agent" in agent_volumes
    assert "agent-socket:/run/corlinman-agent" in gateway["volumes"]
    assert all(
        "agent-socket" not in value
        for service_name in ("napcat", "napcat-manager")
        for value in services[service_name].get("volumes", [])
    )


def test_docker_start_has_no_duplicate_unsafe_py_config_writer() -> None:
    start = (REPO_ROOT / "docker" / "start.sh").read_text(encoding="utf-8")

    assert 'open(py_config_path,"w")' not in start
    assert "json.dump(out" not in start
    assert "gateway boot path owns the only py-config writer" in start
    assert "umask 0007" in start


def test_docker_image_prepares_shared_agent_socket_mountpoint() -> None:
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")

    assert "groupadd --system --gid 10004 corlinman-agent-ipc" in dockerfile
    assert "useradd --system --uid 10002 --gid corlinman-agent" in dockerfile
    assert "mkdir -p /data /execution-state /run/corlinman-agent/config" in dockerfile
    assert "/run/corlinman-agent/socket" in dockerfile
    assert "chown corlinman-agent:corlinman-agent-ipc /run/corlinman-agent" in dockerfile
    assert "chown corlinman:corlinman-agent-ipc /run/corlinman-agent/config" in dockerfile
    assert "chmod 2770 /run/corlinman-agent/socket" in dockerfile
    assert "chmod 2750 /run/corlinman-agent/config" in dockerfile
    assert "USER corlinman" in dockerfile


def test_no_repository_known_napcat_operational_credential() -> None:
    compose = COMPOSE_QQ.read_text(encoding="utf-8")
    install = INSTALL_SH.read_text(encoding="utf-8")
    template = (REPO_ROOT / "deploy" / ".env.template").read_text(encoding="utf-8")

    for text in (compose, install, template):
        assert "corlinman-local-napcat" not in text
    assert "NAPCAT_WEBUI_TOKEN=" in template
    assert "openssl rand -hex 32" in install
    assert "${NAPCAT_WEBUI_TOKEN:?" in compose
    assert "docker compose --env-file ../../.env" in install


def test_native_secrets_are_not_inherited_by_agent() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    gateway = _heredoc(text, "corlinman-gateway --config")
    agent = _heredoc(text, "corlinman agent execution plane")
    legacy_napcat = _heredoc(text, "corlinman NapCat (QQ / OneBot v11)")

    assert "EnvironmentFile=-${PREFIX}/.env\nEnvironmentFile=-${DATA_DIR}/.napcat/legacy-secrets.env" in gateway
    assert "EnvironmentFile=" not in agent
    assert "EnvironmentFile=-${PREFIX}/.env\nEnvironmentFile=-${DATA_DIR}/.napcat/legacy-secrets.env" in legacy_napcat
