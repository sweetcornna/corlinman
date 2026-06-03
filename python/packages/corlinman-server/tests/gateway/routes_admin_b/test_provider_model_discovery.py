"""Tests for ``/admin/providers/{name}/test`` and
``/admin/providers/{name}/models`` endpoints, plus the codex credential
status endpoint ``/admin/credentials/codex/status``.

All network calls are mocked — tests remain fully offline.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from corlinman_server.gateway.routes_admin_b.config_admin import credentials, providers
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._admin_auth import (
    authenticated_test_client,
    configure_admin_auth,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_snapshot(cfg: dict[str, Any]) -> dict[str, Any]:
    return dict(cfg)


@pytest.fixture()
def temp_config_path(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text("", encoding="utf-8")
    return p


@pytest.fixture()
def providers_state(temp_config_path: Path) -> Iterator[tuple[AdminState, dict[str, Any]]]:
    snapshot: dict[str, Any] = {}

    def _loader() -> dict[str, Any]:
        return dict(snapshot)

    state = AdminState(config_loader=_loader, config_path=temp_config_path)
    configure_admin_auth(state)
    state.extras["snapshot"] = snapshot
    set_admin_state(state)
    try:
        yield state, snapshot
    finally:
        set_admin_state(None)


@pytest.fixture()
def providers_client(providers_state: tuple[AdminState, dict[str, Any]]) -> TestClient:
    _state, _ = providers_state
    app = FastAPI()
    app.include_router(providers.router())
    return authenticated_test_client(app)


@pytest.fixture()
def credentials_client(providers_state: tuple[AdminState, dict[str, Any]]) -> TestClient:
    _state, _ = providers_state
    app = FastAPI()
    app.include_router(credentials.router())
    return authenticated_test_client(app)


# ---------------------------------------------------------------------------
# Helper: build a minimal httpx-like response mock
# ---------------------------------------------------------------------------


def _mock_httpx_response(*, status_code: int = 200, json_body: Any = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    return resp


# ---------------------------------------------------------------------------
# Tests: POST /admin/providers/{name}/test
# ---------------------------------------------------------------------------


class TestProviderTest:
    def test_provider_not_found_returns_error(
        self,
        providers_client: TestClient,
        providers_state: tuple[AdminState, dict[str, Any]],
    ) -> None:
        _, snapshot = providers_state
        snapshot.clear()
        snapshot.update({"providers": {}})

        resp = providers_client.post("/admin/providers/nonexistent/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "not_found" in (body.get("error") or "")

    def test_incompatible_kind_returns_error(
        self,
        providers_client: TestClient,
        providers_state: tuple[AdminState, dict[str, Any]],
    ) -> None:
        """anthropic has no free upstream probe; W1.1 surfaces as ok with note.

        Previously the route propagated the legacy helper's
        "kind does not support /v1/models probe" error verbatim. After
        W1.1 the test endpoint upgrades that case to ``ok=True`` with a
        ``note`` flag (config-shape is valid, just not live-verified)
        and a ``models_count`` from the hardcoded catalog so the UI
        button shows green without burning a real chat call.
        """
        _, snapshot = providers_state
        snapshot.clear()
        snapshot.update({
            "providers": {
                "myanthropic": {"kind": "anthropic", "api_key": "sk-ant-xxx", "enabled": True}
            }
        })

        resp = providers_client.post("/admin/providers/myanthropic/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "note" in body
        assert body["models_count"] >= 1

    @pytest.mark.asyncio
    async def test_openai_provider_success(
        self,
        providers_state: tuple[AdminState, dict[str, Any]],
    ) -> None:
        _, snapshot = providers_state
        snapshot.clear()
        snapshot.update({
            "providers": {
                "myopenai": {
                    "kind": "openai",
                    "api_key": "sk-test",
                    "base_url": "https://api.openai.com",
                    "enabled": True,
                }
            }
        })

        mock_resp = _mock_httpx_response(
            status_code=200,
            json_body={"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]},
        )

        # Patch AsyncClient.get to return mock_resp
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        from corlinman_server.gateway.routes_admin_b.config_admin.providers import (
            _query_provider_models,
        )

        with patch(
            "corlinman_server.gateway.routes_admin_b.config_admin.providers._httpx",
            create=True,
        ):
            # Re-test via the underlying helper directly, mocking httpx
            cfg = {
                "providers": {
                    "myopenai": {
                        "kind": "openai",
                        "api_key": "sk-test",
                        "base_url": "https://api.openai.com",
                        "enabled": True,
                    }
                }
            }
            with patch(
                "httpx.AsyncClient",
                return_value=mock_client,
            ):
                result = await _query_provider_models("myopenai", cfg)

        assert result["ok"] is True
        assert "gpt-4o" in result["models"]
        assert result["error"] is None

    def test_codex_provider_no_cred_returns_error(
        self,
        providers_client: TestClient,
        providers_state: tuple[AdminState, dict[str, Any]],
    ) -> None:
        """Codex is OpenAI-shape so W1.1 routes through the helper.

        With no credential file the helper returns ``ok=False`` carrying
        ``codex_auth_not_found``; the route forwards it after redacting.
        """
        _, snapshot = providers_state
        snapshot.clear()
        snapshot.update({"providers": {}})

        with patch(
            "corlinman_providers._codex_oauth.load_codex_credential",
            return_value=None,
        ):
            resp = providers_client.post("/admin/providers/codex/test")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "codex_auth_not_found" in (body.get("error") or "")


# ---------------------------------------------------------------------------
# Tests: GET /admin/providers/{name}/models
# ---------------------------------------------------------------------------


class TestProviderModels:
    def test_returns_models_and_error_keys(
        self,
        providers_client: TestClient,
        providers_state: tuple[AdminState, dict[str, Any]],
    ) -> None:
        _, snapshot = providers_state
        snapshot.clear()
        snapshot.update({"providers": {}})

        resp = providers_client.get("/admin/providers/nonexistent/models")
        assert resp.status_code == 200
        body = resp.json()
        assert "models" in body
        assert "error" in body
        assert isinstance(body["models"], list)

    @pytest.mark.asyncio
    async def test_models_returned_on_success(
        self,
        providers_state: tuple[AdminState, dict[str, Any]],
    ) -> None:
        _, _snapshot = providers_state
        cfg = {
            "providers": {
                "myprovider": {
                    "kind": "openai_compatible",
                    "api_key": "sk-xyz",
                    "base_url": "https://my.api",
                    "enabled": True,
                }
            }
        }

        mock_resp = _mock_httpx_response(
            status_code=200,
            json_body={"data": [{"id": "model-a"}, {"id": "model-b"}]},
        )
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        from corlinman_server.gateway.routes_admin_b.config_admin.providers import (
            _query_provider_models,
        )

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await _query_provider_models("myprovider", cfg)

        assert result["ok"] is True
        assert "model-a" in result["models"]
        assert "model-b" in result["models"]


# ---------------------------------------------------------------------------
# Tests: GET /admin/credentials/codex/status
# ---------------------------------------------------------------------------


class TestCodexCredentialStatus:
    def test_no_file_returns_not_detected(
        self,
        credentials_client: TestClient,
    ) -> None:
        with patch(
            "corlinman_server.gateway.oauth.codex_external.read_codex_status",
            return_value=None,
        ):
            resp = credentials_client.get("/admin/credentials/codex/status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["detected"] is False
        assert body["account"] is None
        assert body["expires_at_ms"] is None
        assert body["expired"] is None

    def test_detected_not_expired(
        self,
        credentials_client: TestClient,
    ) -> None:
        import time

        future_ms = int(time.time() * 1000) + 3_600_000  # 1 hour from now

        from corlinman_server.gateway.oauth.codex_external import CodexStatus

        status = CodexStatus(
            detected=True,
            account_id="user@example.com",
            expires_at_ms=future_ms,
        )
        with patch(
            "corlinman_server.gateway.oauth.codex_external.read_codex_status",
            return_value=status,
        ):
            resp = credentials_client.get("/admin/credentials/codex/status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["detected"] is True
        assert body["account"] == "user@example.com"
        assert body["expires_at_ms"] == future_ms
        assert body["expired"] is False

    def test_detected_and_expired(
        self,
        credentials_client: TestClient,
    ) -> None:
        import time

        past_ms = int(time.time() * 1000) - 1_000  # 1 second ago

        from corlinman_server.gateway.oauth.codex_external import CodexStatus

        status = CodexStatus(
            detected=True,
            account_id="user@example.com",
            expires_at_ms=past_ms,
        )
        with patch(
            "corlinman_server.gateway.oauth.codex_external.read_codex_status",
            return_value=status,
        ):
            resp = credentials_client.get("/admin/credentials/codex/status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["detected"] is True
        assert body["expired"] is True

    def test_detected_no_expiry(
        self,
        credentials_client: TestClient,
    ) -> None:
        from corlinman_server.gateway.oauth.codex_external import CodexStatus

        status = CodexStatus(detected=True, account_id=None, expires_at_ms=None)
        with patch(
            "corlinman_server.gateway.oauth.codex_external.read_codex_status",
            return_value=status,
        ):
            resp = credentials_client.get("/admin/credentials/codex/status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["detected"] is True
        assert body["expired"] is False
        assert body["expires_at_ms"] is None
