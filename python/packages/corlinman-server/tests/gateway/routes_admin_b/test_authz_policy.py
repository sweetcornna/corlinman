"""``/admin/authz/policy`` — the W3-4 policy editor backend.

Round-trips the structured ``[permissions]`` editor through the existing
config-admin channel: a PUT must land in ``config.toml`` (whole-section
replace, unset keys absent) and publish the mutation to live readers;
invalid policies must 400 without touching the file.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from corlinman_server.gateway.routes_admin_b.config_admin import authz_policy
from corlinman_server.gateway.routes_admin_b.state import AdminState, set_admin_state
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._admin_auth import authenticated_test_client, configure_admin_auth


@pytest.fixture()
def harness(tmp_path: Path) -> Iterator[tuple[dict[str, Any], TestClient, Path]]:
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    snapshot: dict[str, Any] = {}

    def _loader() -> dict[str, Any]:
        return dict(snapshot)

    state = AdminState(config_loader=_loader, config_path=config_path)
    configure_admin_auth(state)
    set_admin_state(state)
    app = FastAPI()
    app.include_router(authz_policy.router())
    try:
        yield snapshot, authenticated_test_client(app), config_path
    finally:
        set_admin_state(None)


_POLICY = {
    "mode": "default",
    "strict": True,
    "rules": [
        {
            "tool": "run_shell(rm:*)",
            "action": "deny",
            "note": "no destructive shell",
        },
        {
            "tool": "mcp:github/*",
            "action": "ask",
            "memory": "session",
            "scope": {"surface": "qq|telegram", "user": "admin*"},
        },
    ],
}


def test_get_renders_current_section(
    harness: tuple[dict[str, Any], TestClient, Path],
) -> None:
    snapshot, client, _path = harness
    snapshot["permissions"] = {
        "mode": "plan",
        "rules": [{"tool": "*", "action": "ask", "match": {"user": "admin*"}}],
    }
    resp = client.get("/admin/authz/policy")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "plan"
    assert body["strict"] is None  # unset stays unset, not false
    # The deprecated ``match`` alias is surfaced as ``scope``.
    assert body["rules"][0]["scope"]["user"] == "admin*"


def test_put_round_trips_into_config_toml(
    harness: tuple[dict[str, Any], TestClient, Path],
) -> None:
    _snapshot, client, config_path = harness
    resp = client.put("/admin/authz/policy", json=_POLICY)
    assert resp.status_code == 200

    written = tomllib.loads(config_path.read_text(encoding="utf-8"))
    section = written["permissions"]
    assert section["mode"] == "default"
    assert section["strict"] is True
    # Unset keys must be ABSENT (None is a payload in the sidecar contract).
    assert "default_action" not in section
    assert "last_match_wins" not in section
    assert [r["tool"] for r in section["rules"]] == [
        "run_shell(rm:*)",
        "mcp:github/*",
    ]
    assert section["rules"][1]["scope"] == {
        "surface": "qq|telegram",
        "user": "admin*",
    }

    # The PUT response mirrors what was written (the GET view in prod
    # reads the swapped snapshot; the test harness has no swap_fn wired).
    body = resp.json()
    assert body["strict"] is True
    assert len(body["rules"]) == 2
    assert body["rules"][1]["memory"] == "session"


def test_put_invalid_action_400s_without_writing(
    harness: tuple[dict[str, Any], TestClient, Path],
) -> None:
    _snapshot, client, config_path = harness
    bad = {"rules": [{"tool": "run_shell", "action": "blocken"}]}
    resp = client.put("/admin/authz/policy", json=bad)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_policy"
    assert "permissions" not in tomllib.loads(
        config_path.read_text(encoding="utf-8")
    )


def test_put_empty_policy_removes_the_section(
    harness: tuple[dict[str, Any], TestClient, Path],
) -> None:
    snapshot, client, config_path = harness
    snapshot["permissions"] = {"mode": "plan"}
    resp = client.put("/admin/authz/policy", json={"rules": []})
    assert resp.status_code == 200
    assert "permissions" not in tomllib.loads(
        config_path.read_text(encoding="utf-8")
    )
