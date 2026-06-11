from __future__ import annotations

from typing import Any

import pytest
from corlinman_server.gateway.routes_admin_b import _napcat_lib as nc
from corlinman_server.gateway.routes_admin_b import napcat
from corlinman_server.gateway.routes_admin_b.state import AdminState, set_admin_state
from fastapi import FastAPI

from ._admin_auth import authenticated_test_client, configure_admin_auth


class _HealthyProbeClient:
    def __init__(self, base_url: str, access_token: str | None) -> None:
        self.base_url = base_url
        self.access_token = access_token

    async def __aenter__(self) -> _HealthyProbeClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get_credential(self) -> str | None:
        return "CRED-OK" if self.access_token else None

    async def _fetch_qrcode(self) -> str:
        return "https://qq.example/qr"

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        del body
        if path == nc.OB11_CONFIG_GET_PATH:
            return {"network": {"websocketServers": []}}
        raise AssertionError(f"unexpected path {path}")


class _UnreachableProbeClient(_HealthyProbeClient):
    async def get_credential(self) -> str | None:
        raise nc.NapcatError("napcat_unreachable", "connect failed", status=503)

    async def _fetch_qrcode(self) -> str:
        raise nc.NapcatError("napcat_unreachable", "connect failed", status=503)

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        del path, body
        raise nc.NapcatError("napcat_unreachable", "connect failed", status=503)


@pytest.mark.asyncio
async def test_diagnostics_classifies_default_url_as_managed_missing_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CORLINMAN_NAPCAT_URL", raising=False)
    monkeypatch.delenv("NAPCAT_WEBUI_TOKEN", raising=False)
    monkeypatch.delenv("NAPCAT_WEBUI_SECRET_KEY", raising=False)
    monkeypatch.delenv("WEBUI_TOKEN", raising=False)

    out = await nc._probe_napcat_diagnostics(
        {"channels": {"qq": {}}},
        client_factory=_HealthyProbeClient,
    )

    assert out.mode == "managed"
    assert out.url == nc.DEFAULT_NAPCAT_URL
    assert out.url_source == "default"
    assert out.managed is True
    assert out.auth_configured is False
    assert out.credential == "missing_token"
    assert "napcat_webui_token_missing" in out.issues
    assert "set_napcat_webui_token" in out.actions


@pytest.mark.asyncio
async def test_diagnostics_classifies_explicit_url_as_external_and_healthy() -> None:
    out = await nc._probe_napcat_diagnostics(
        {
            "channels": {
                "qq": {
                    "napcat_url": "http://user-napcat:6099",
                    "napcat_access_token": "tok",
                }
            }
        },
        client_factory=_HealthyProbeClient,
    )

    assert out.mode == "external"
    assert out.url == "http://user-napcat:6099"
    assert out.url_source == "config"
    assert out.managed is False
    assert out.auth_configured is True
    assert out.credential == "ok"
    assert out.qrcode_api == "ok"
    assert out.onebot_config_api == "ok"
    assert out.issues == []


@pytest.mark.asyncio
async def test_diagnostics_reports_external_unreachable() -> None:
    out = await nc._probe_napcat_diagnostics(
        {
            "channels": {
                "qq": {
                    "napcat_url": "http://user-napcat:6099",
                    "napcat_access_token": "tok",
                }
            }
        },
        client_factory=_UnreachableProbeClient,
    )

    assert out.mode == "external"
    assert out.credential == "failed"
    assert out.qrcode_api == "unreachable"
    assert out.onebot_config_api == "unreachable"
    assert "napcat_unreachable" in out.issues
    assert "check_external_napcat_url" in out.actions


def test_napcat_diagnostics_route_returns_probe_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_probe(cfg: dict[str, Any]) -> nc.NapcatDiagnosticsOut:
        calls.append(cfg)
        return nc.NapcatDiagnosticsOut(
            mode="external",
            url="http://user-napcat:6099",
            url_source="config",
            managed=False,
            auth_configured=True,
            credential="ok",
            qrcode_api="ok",
            onebot_config_api="ok",
            issues=[],
            actions=[],
        )

    monkeypatch.setattr(
        napcat,
        "config_snapshot",
        lambda: {
            "channels": {
                "qq": {
                    "napcat_url": "http://user-napcat:6099",
                    "napcat_access_token": "tok",
                }
            }
        },
    )
    monkeypatch.setattr(napcat, "_probe_napcat_diagnostics", fake_probe)

    state = configure_admin_auth(AdminState(data_dir=tmp_path))
    set_admin_state(state)
    try:
        app = FastAPI()
        app.include_router(napcat.router())
        client = authenticated_test_client(app)
        resp = client.get("/admin/channels/qq/napcat/diagnostics")
    finally:
        set_admin_state(None)

    assert resp.status_code == 200, resp.text
    assert resp.json()["mode"] == "external"
    assert calls == [
        {
            "channels": {
                "qq": {
                    "napcat_url": "http://user-napcat:6099",
                    "napcat_access_token": "tok",
                }
            }
        }
    ]
