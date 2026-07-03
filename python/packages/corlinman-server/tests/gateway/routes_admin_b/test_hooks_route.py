"""``GET /admin/hooks`` — declarative + discovered + shell layers (Dim 9)."""

from __future__ import annotations

from pathlib import Path

from corlinman_hooks import HookRunner
from corlinman_server.gateway.routes_admin_b.infra import hooks as hooks_route
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    require_admin,
    set_admin_state,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(tmp_path: Path, runner: HookRunner | None) -> TestClient:
    extras = {} if runner is None else {"hook_runner": runner}
    state = AdminState(data_dir=tmp_path, extras=extras)
    set_admin_state(state)
    application = FastAPI()
    application.include_router(hooks_route.router())
    application.dependency_overrides[require_admin] = lambda: None
    return TestClient(application)


def test_admin_hooks_reports_all_layers(tmp_path: Path) -> None:
    runner = HookRunner(
        {
            "hooks": {
                "pre_tool": "legacy.sh",
                "declarative": {
                    "PreToolUse": [
                        {"matcher": "run_shell", "hooks": [{"kind": "command", "command": "true"}]}
                    ],
                    "BadEvent": [{"hooks": [{"kind": "command", "command": "true"}]}],
                },
            }
        }
    )
    try:
        client = _client(tmp_path, runner)
        body = client.get("/admin/hooks").json()
        assert body["status"] == "ok"
        assert body["registered"] == {"pre_tool": "legacy.sh"}
        assert body["declarative"] == [
            {
                "event": "pre_tool",
                "matcher": "run_shell",
                "if": None,
                "kinds": ["command"],
                "async": [False],
            }
        ]
        assert any("BadEvent" in w for w in body["warnings"])
        assert "pre_tool" in body["live_events"]
        assert "user_prompt_submit" in body["supported_events"]
    finally:
        set_admin_state(None)


def test_admin_hooks_without_runner(tmp_path: Path) -> None:
    try:
        client = _client(tmp_path, None)
        body = client.get("/admin/hooks").json()
        assert body["status"] == "hook_runner_unavailable"
        assert body["registered"] == {}
        assert body["supported_events"] == []
        assert body["live_events"]  # wiring facts are runner-independent
    finally:
        set_admin_state(None)
