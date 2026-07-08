"""Layered permission settings (gap E1 — settings.json persistence).

``PermissionGate.from_layered_sources`` existed with zero production
callers: every deployment built its gate from ``from_env()`` alone, so a
durable permission rule had to live in an environment variable and the
console's "always" grants evaporated with the session. This module pins
the new loader:

* ``<data_dir>/settings.json`` — user layer (durable, machine-wide);
* ``<project_dir>/.corlinman/settings.local.json`` — project layer;
* ``CORLINMAN_AGENT_PERMISSIONS`` env — top layer (wins).

Later layers override earlier ones (last-match-wins stacking); with no
settings file present the gate is byte-identical to ``from_env()``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from corlinman_agent.permission import ALLOW, DENY, PermissionGate
from corlinman_agent.permission_settings import (
    build_permission_gate,
    persist_allow_rule,
    project_settings_path,
    user_settings_path,
)


def _write_settings(path: Path, permissions: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"permissions": permissions}), encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "CORLINMAN_AGENT_PERMISSIONS",
        "CORLINMAN_AGENT_STRICT_MODE",
        "CORLINMAN_AGENT_PERMISSION_MODE",
        "CORLINMAN_AGENT_PERMISSION_LAST_MATCH_WINS",
        "CORLINMAN_DATA_DIR",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_user_settings_path_prefers_data_dir_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path / "dd"))
    assert user_settings_path() == tmp_path / "dd" / "settings.json"
    # An explicit argument wins over the env var.
    assert user_settings_path(tmp_path / "x") == tmp_path / "x" / "settings.json"


def test_project_settings_path_defaults_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert project_settings_path() == tmp_path / ".corlinman" / "settings.local.json"
    assert (
        project_settings_path(tmp_path / "proj")
        == tmp_path / "proj" / ".corlinman" / "settings.local.json"
    )


# ---------------------------------------------------------------------------
# Layer stacking
# ---------------------------------------------------------------------------


def test_no_files_no_env_is_stock_allow_all(tmp_path: Path) -> None:
    gate = build_permission_gate(
        data_dir=tmp_path / "dd", project_dir=tmp_path / "proj"
    )
    assert isinstance(gate, PermissionGate)
    assert gate.decide("run_shell") == ALLOW
    assert gate.mode.value == "default"


def test_user_layer_rules_apply(tmp_path: Path) -> None:
    _write_settings(
        tmp_path / "dd" / "settings.json",
        {"rules": [{"tool": "run_shell", "action": "deny"}]},
    )
    gate = build_permission_gate(
        data_dir=tmp_path / "dd", project_dir=tmp_path / "proj"
    )
    assert gate.decide("run_shell") == DENY
    assert gate.decide("web_search") == ALLOW


def test_project_layer_overrides_user(tmp_path: Path) -> None:
    _write_settings(
        tmp_path / "dd" / "settings.json",
        {"rules": [{"tool": "run_shell", "action": "deny"}]},
    )
    _write_settings(
        tmp_path / "proj" / ".corlinman" / "settings.local.json",
        {"rules": [{"tool": "run_shell", "action": "allow"}]},
    )
    gate = build_permission_gate(
        data_dir=tmp_path / "dd", project_dir=tmp_path / "proj"
    )
    assert gate.decide("run_shell") == ALLOW


def test_env_layer_beats_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_settings(
        tmp_path / "proj" / ".corlinman" / "settings.local.json",
        {"rules": [{"tool": "run_shell", "action": "allow"}]},
    )
    monkeypatch.setenv(
        "CORLINMAN_AGENT_PERMISSIONS",
        json.dumps([{"tool": "run_shell", "action": "deny"}]),
    )
    gate = build_permission_gate(
        data_dir=tmp_path / "dd", project_dir=tmp_path / "proj"
    )
    assert gate.decide("run_shell") == DENY


def test_mode_and_strict_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_settings(
        tmp_path / "dd" / "settings.json",
        {"rules": [], "mode": "plan", "strict": True},
    )
    gate = build_permission_gate(
        data_dir=tmp_path / "dd", project_dir=tmp_path / "proj"
    )
    assert gate.mode.value == "plan"
    # File-driven plan mode denies a mutating tool with no explicit rule.
    assert gate.decide("write_file") == DENY

    # Env mode overrides the file's.
    monkeypatch.setenv("CORLINMAN_AGENT_PERMISSION_MODE", "bypass")
    gate2 = build_permission_gate(
        data_dir=tmp_path / "dd", project_dir=tmp_path / "proj"
    )
    assert gate2.mode.value == "bypass"
    assert gate2.decide("write_file") == ALLOW


def test_project_mode_overrides_user_mode(tmp_path: Path) -> None:
    _write_settings(
        tmp_path / "dd" / "settings.json", {"rules": [], "mode": "plan"}
    )
    _write_settings(
        tmp_path / "proj" / ".corlinman" / "settings.local.json",
        {"rules": [], "mode": "acceptEdits"},
    )
    gate = build_permission_gate(
        data_dir=tmp_path / "dd", project_dir=tmp_path / "proj"
    )
    assert gate.mode.value == "acceptEdits"


def test_broken_settings_file_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "dd" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    gate = build_permission_gate(
        data_dir=tmp_path / "dd", project_dir=tmp_path / "proj"
    )
    assert gate.decide("run_shell") == ALLOW  # degrades to stock allow-all


def test_non_dict_permissions_block_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "dd" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"permissions": ["nope"]}), encoding="utf-8")
    gate = build_permission_gate(
        data_dir=tmp_path / "dd", project_dir=tmp_path / "proj"
    )
    assert gate.decide("run_shell") == ALLOW


# ---------------------------------------------------------------------------
# Durable "always" persistence
# ---------------------------------------------------------------------------


def test_persist_allow_rule_writes_and_dedups(tmp_path: Path) -> None:
    dd = tmp_path / "dd"
    path = persist_allow_rule("run_shell", data_dir=dd)
    assert path == dd / "settings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["permissions"]["rules"] == [
        {"tool": "run_shell", "action": "allow"}
    ]
    # Idempotent — a second grant doesn't duplicate the rule.
    persist_allow_rule("run_shell", data_dir=dd)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data["permissions"]["rules"]) == 1

    # And the layered builder actually honours the persisted grant.
    gate = build_permission_gate(data_dir=dd, project_dir=tmp_path / "proj")
    assert gate.decide("run_shell") == ALLOW


def test_persist_allow_rule_preserves_existing_settings(tmp_path: Path) -> None:
    dd = tmp_path / "dd"
    _write_settings(
        dd / "settings.json",
        {"rules": [{"tool": "web_search", "action": "deny"}], "mode": "plan"},
    )
    persist_allow_rule("run_shell", data_dir=dd)
    data = json.loads((dd / "settings.json").read_text(encoding="utf-8"))
    rules = data["permissions"]["rules"]
    assert {"tool": "web_search", "action": "deny"} in rules
    assert {"tool": "run_shell", "action": "allow"} in rules
    assert data["permissions"]["mode"] == "plan"


def test_persist_allow_rule_survives_corrupt_file(tmp_path: Path) -> None:
    dd = tmp_path / "dd"
    dd.mkdir(parents=True)
    (dd / "settings.json").write_text("{broken", encoding="utf-8")
    path = persist_allow_rule("run_shell", data_dir=dd)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["permissions"]["rules"] == [
        {"tool": "run_shell", "action": "allow"}
    ]
