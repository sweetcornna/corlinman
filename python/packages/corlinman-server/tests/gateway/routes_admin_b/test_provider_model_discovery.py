"""Tests for ``/admin/providers/{name}/test`` and
``/admin/providers/{name}/models`` endpoints, plus the codex credential
status endpoint ``/admin/credentials/codex/status``.

All network calls are mocked — tests remain fully offline.
"""

from __future__ import annotations

import base64
import json
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


def _codex_jwt_with_account(account_id: str) -> str:
    payload = {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
        }
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    return f"header.{encoded.decode('ascii')}.signature"


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
        """anthropic without a resolvable key falls back to the built-in catalog.

        A configured API key now enables native live model discovery. Without
        one, the endpoint keeps the previous green "configured" behavior and
        reports the hardcoded catalog size.
        """
        _, snapshot = providers_state
        snapshot.clear()
        snapshot.update({
            "providers": {
                "myanthropic": {"kind": "anthropic", "enabled": True}
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

        from corlinman_server.gateway.routes_admin_b.config_admin._providers_lib import (
            _query_provider_models,
        )

        with patch(
            "corlinman_server.gateway.routes_admin_b.config_admin._providers_lib._httpx",
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

    @pytest.mark.asyncio
    async def test_anthropic_provider_uses_native_models_api(
        self,
        providers_state: tuple[AdminState, dict[str, Any]],
    ) -> None:
        _, _snapshot = providers_state
        cfg = {
            "providers": {
                "claude": {
                    "kind": "anthropic",
                    "api_key": "sk-ant-test",
                    "enabled": True,
                }
            }
        }
        mock_resp = _mock_httpx_response(
            status_code=200,
            json_body={"data": [{"id": "claude-fable-5"}, {"id": "claude-opus-6"}]},
        )
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        from corlinman_server.gateway.routes_admin_b.config_admin._providers_lib import (
            _query_provider_models,
        )

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await _query_provider_models("claude", cfg)

        assert result["ok"] is True
        assert result["models"] == ["claude-fable-5", "claude-opus-6"]
        mock_client.get.assert_awaited_once()
        assert mock_client.get.await_args.args[0] == "https://api.anthropic.com/v1/models"
        headers = mock_client.get.await_args.kwargs["headers"]
        assert headers["x-api-key"] == "sk-ant-test"
        assert headers["anthropic-version"] == "2023-06-01"

    @pytest.mark.asyncio
    async def test_google_provider_uses_native_models_api(
        self,
        providers_state: tuple[AdminState, dict[str, Any]],
    ) -> None:
        _, _snapshot = providers_state
        cfg = {
            "providers": {
                "gemini": {
                    "kind": "google",
                    "api_key": "google-key",
                    "enabled": True,
                }
            }
        }
        mock_resp = _mock_httpx_response(
            status_code=200,
            json_body={
                "models": [
                    {
                        "name": "models/gemini-4.0-pro-preview",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/text-embedding-004",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                ]
            },
        )
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        from corlinman_server.gateway.routes_admin_b.config_admin._providers_lib import (
            _query_provider_models,
        )

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await _query_provider_models("gemini", cfg)

        assert result["ok"] is True
        assert result["models"] == ["gemini-4.0-pro-preview"]
        mock_client.get.assert_awaited_once()
        assert (
            mock_client.get.await_args.args[0]
            == "https://generativelanguage.googleapis.com/v1beta/models"
        )
        assert mock_client.get.await_args.kwargs["params"] == {"key": "google-key"}

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

    @pytest.mark.asyncio
    async def test_codex_provider_probe_uses_chatgpt_codex_backend(self) -> None:
        """Codex OAuth tokens are ChatGPT subscription tokens, not API keys."""
        from corlinman_providers._codex_oauth import CodexOAuthCredential
        from corlinman_server.gateway.routes_admin_b.config_admin._providers_lib import (
            _query_provider_models,
        )

        access_token = _codex_jwt_with_account("acct_test_123")
        mock_resp = _mock_httpx_response(
            status_code=200,
            json_body={
                "models": [
                    {"slug": "gpt-5.4-mini"},
                    {"slug": "gpt-5.5"},
                    {"name": "ignored"},
                ]
            },
        )
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch(
            "corlinman_providers._codex_oauth.load_codex_credential",
            return_value=CodexOAuthCredential(
                access_token=access_token,
                refresh_token="refresh-token",
                expires_at_ms=None,
            ),
        ), patch("httpx.AsyncClient", return_value=mock_client):
            result = await _query_provider_models("codex", {"providers": {}})

        assert result["ok"] is True
        assert result["models"] == ["gpt-5.4-mini", "gpt-5.5"]
        mock_client.get.assert_awaited_once()
        url = mock_client.get.await_args.args[0]
        headers = mock_client.get.await_args.kwargs["headers"]
        assert (
            url
            == "https://chatgpt.com/backend-api/codex/models?client_version=1.0.0"
        )
        assert headers["Authorization"] == f"Bearer {access_token}"
        assert headers["originator"] == "codex_cli_rs"
        assert headers["User-Agent"].startswith("codex_cli_rs/")
        assert headers["ChatGPT-Account-ID"] == "acct_test_123"

    @pytest.mark.asyncio
    async def test_codex_provider_probe_refreshes_expired_credential(self) -> None:
        from corlinman_providers._codex_oauth import CodexOAuthCredential
        from corlinman_server.gateway.routes_admin_b.config_admin._providers_lib import (
            _query_provider_models,
        )

        old_token = _codex_jwt_with_account("acct_old")
        fresh_token = _codex_jwt_with_account("acct_fresh")
        mock_resp = _mock_httpx_response(
            status_code=200,
            json_body={"models": [{"slug": "gpt-5.5"}]},
        )
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        refresh = AsyncMock(
            return_value=CodexOAuthCredential(
                access_token=fresh_token,
                refresh_token="new-refresh",
                expires_at_ms=None,
            )
        )

        with patch(
            "corlinman_providers._codex_oauth.load_codex_credential",
            return_value=CodexOAuthCredential(
                access_token=old_token,
                refresh_token="old-refresh",
                expires_at_ms=1,
            ),
        ), patch(
            "corlinman_providers._codex_oauth.refresh_codex_token",
            new=refresh,
        ), patch(
            "corlinman_providers._codex_oauth.persist_codex_credential",
            return_value=True,
        ) as persist, patch("httpx.AsyncClient", return_value=mock_client):
            result = await _query_provider_models("codex", {"providers": {}})

        assert result["ok"] is True
        refresh.assert_awaited_once_with(refresh_token="old-refresh")
        persist.assert_called_once()
        headers = mock_client.get.await_args.kwargs["headers"]
        assert headers["Authorization"] == f"Bearer {fresh_token}"
        assert headers["ChatGPT-Account-ID"] == "acct_fresh"


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

        from corlinman_server.gateway.routes_admin_b.config_admin._providers_lib import (
            _query_provider_models,
        )

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await _query_provider_models("myprovider", cfg)

        assert result["ok"] is True
        assert "model-a" in result["models"]
        assert "model-b" in result["models"]

    @pytest.mark.asyncio
    async def test_base_url_ending_with_v1_uses_single_v1_segment(
        self,
        providers_state: tuple[AdminState, dict[str, Any]],
    ) -> None:
        """OpenAI-compatible relays commonly expose ``.../v1`` as base_url.

        The probe must request ``.../v1/models`` rather than duplicating the
        version segment into ``.../v1/v1/models``.
        """
        _, _snapshot = providers_state
        cfg = {
            "providers": {
                "relay": {
                    "kind": "openai_compatible",
                    "api_key": "sk-test",
                    "base_url": "https://relay.example/api/v1",
                    "enabled": True,
                }
            }
        }

        mock_resp = _mock_httpx_response(
            status_code=200,
            json_body={"data": [{"id": "relay-model-a"}]},
        )
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        from corlinman_server.gateway.routes_admin_b.config_admin._providers_lib import (
            _query_provider_models,
        )

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await _query_provider_models("relay", cfg)

        assert result["ok"] is True
        mock_client.get.assert_awaited_once()
        assert mock_client.get.await_args.args[0] == "https://relay.example/api/v1/models"

    @pytest.mark.asyncio
    async def test_glm_default_v4_base_url_uses_models_under_v4(
        self,
        providers_state: tuple[AdminState, dict[str, Any]],
    ) -> None:
        """GLM exposes an OpenAI-compatible v4 root, not a v1 root."""
        _, _snapshot = providers_state
        cfg = {
            "providers": {
                "zhipu": {
                    "kind": "glm",
                    "api_key": "sk-test",
                    "enabled": True,
                }
            }
        }

        mock_resp = _mock_httpx_response(
            status_code=200,
            json_body={"data": [{"id": "glm-4-flash"}]},
        )
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        from corlinman_server.gateway.routes_admin_b.config_admin._providers_lib import (
            _query_provider_models,
        )

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await _query_provider_models("zhipu", cfg)

        assert result["ok"] is True
        assert result["models"] == ["glm-4-flash"]
        mock_client.get.assert_awaited_once()
        assert (
            mock_client.get.await_args.args[0]
            == "https://open.bigmodel.cn/api/paas/v4/models"
        )


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
