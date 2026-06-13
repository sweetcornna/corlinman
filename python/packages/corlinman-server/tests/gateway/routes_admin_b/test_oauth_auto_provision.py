"""OAuth completion should make subscription-backed providers chat-ready.

The login/import endpoints persist OAuth tokens today; this file pins the
second half of the UX contract: after a successful OAuth flow, the gateway also
writes a provider slot plus model aliases discovered from the upstream account.
That way the model list is immediately useful without a manual trip through
Providers or Models.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from corlinman_server.gateway.oauth import sessions
from corlinman_server.gateway.oauth.storage import OAuthCredential
from corlinman_server.gateway.routes_admin_b import oauth as oauth_routes
from corlinman_server.gateway.routes_admin_b.state import AdminState, set_admin_state
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._admin_auth import authenticated_test_client, configure_admin_auth


@pytest.fixture()
def oauth_state_client(tmp_path: Path) -> Iterator[tuple[AdminState, TestClient, Path]]:
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    snapshot: dict[str, Any] = {}

    def _loader() -> dict[str, Any]:
        return dict(snapshot)

    def _swap(next_cfg: dict[str, Any]) -> None:
        snapshot.clear()
        snapshot.update(next_cfg)

    state = AdminState(config_loader=_loader, config_path=config_path)
    state.data_dir = tmp_path  # type: ignore[attr-defined]
    state.extras["snapshot"] = snapshot
    state.extras["config_swap_fn"] = _swap
    configure_admin_auth(state)
    set_admin_state(state)
    sessions._reset_for_tests()

    app = FastAPI()
    app.include_router(oauth_routes.router())
    try:
        yield state, authenticated_test_client(app), config_path
    finally:
        set_admin_state(None)
        sessions._reset_for_tests()


def _on_disk(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    return tomllib.loads(raw)


def _mock_httpx_client(resp: MagicMock) -> AsyncMock:
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=resp)
    return client


_FAKE_TOKENS = {
    "access_token": "oauth-access-token",
    "refresh_token": "oauth-refresh-token",
    "expires_at_ms": 9_999_999_999_999,
    "scope": "scope",
}


def test_anthropic_model_discovery_sends_anthropic_api_version() -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"data": [{"id": "claude-opus-4-8"}]}
    http_client = _mock_httpx_client(resp)

    with patch("httpx.AsyncClient", return_value=http_client) as async_client_cls:
        models = __import__("asyncio").run(
            oauth_routes._query_anthropic_oauth_models("oauth-access-token")
        )

    assert models == ["claude-opus-4-8"]
    async_client_cls.assert_called_once_with(timeout=10.0)
    headers = http_client.get.await_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer oauth-access-token"
    assert headers["anthropic-beta"] == "oauth-2025-04-20"
    assert headers["anthropic-version"] == "2023-06-01"


def test_anthropic_pkce_submit_discovers_models_and_configures_aliases(
    oauth_state_client: tuple[AdminState, TestClient, Path],
) -> None:
    _state, client, config_path = oauth_state_client
    start = client.post("/admin/oauth/anthropic/start")
    assert start.status_code == 200, start.text
    session_id = start.json()["session_id"]

    with patch(
        "corlinman_server.gateway.oauth.anthropic_pkce.exchange_code",
        new=AsyncMock(return_value=_FAKE_TOKENS),
    ), patch(
        "corlinman_server.gateway.routes_admin_b.oauth._query_anthropic_oauth_models",
        new=AsyncMock(return_value=["claude-sonnet-4-6", "claude-opus-4-8"]),
    ):
        resp = client.post(
            "/admin/oauth/anthropic/submit",
            json={"session_id": session_id, "code": "CODE#STATE"},
        )

    assert resp.status_code == 200, resp.text
    on_disk = _on_disk(config_path)
    assert on_disk["providers"]["anthropic"] == {
        "kind": "anthropic",
        "enabled": True,
    }
    assert on_disk["models"]["default"] == "claude-opus-4-8"
    assert on_disk["models"]["aliases"]["claude-opus-4-8"] == {
        "provider": "anthropic",
        "model": "claude-opus-4-8",
        "params": {},
    }
    assert on_disk["models"]["aliases"]["claude-sonnet-4-6"] == {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "params": {},
    }


def test_claude_code_import_discovers_models_and_configures_anthropic(
    oauth_state_client: tuple[AdminState, TestClient, Path],
) -> None:
    _state, client, config_path = oauth_state_client
    credential = OAuthCredential.new(
        provider="anthropic",
        access_token="claude-code-access-token",
        refresh_token="claude-code-refresh-token",
        expires_at_ms=9_999_999_999_999,
        scope="user:inference",
        obtained_at_ms=1_000,
    )

    with patch(
        "corlinman_server.gateway.oauth.claude_code_import.read_claude_code_credentials",
        return_value=credential,
    ), patch(
        "corlinman_server.gateway.routes_admin_b.oauth._query_anthropic_oauth_models",
        new=AsyncMock(return_value=["claude-haiku-4-5", "claude-fable-5"]),
    ):
        resp = client.post("/admin/oauth/claude-code/import")

    assert resp.status_code == 200, resp.text
    on_disk = _on_disk(config_path)
    assert on_disk["providers"]["anthropic"] == {
        "kind": "anthropic",
        "enabled": True,
    }
    assert on_disk["models"]["default"] == "claude-fable-5"
    assert on_disk["models"]["aliases"]["claude-fable-5"]["provider"] == "anthropic"


def test_codex_pkce_submit_discovers_models_and_configures_aliases(
    oauth_state_client: tuple[AdminState, TestClient, Path],
) -> None:
    _state, client, config_path = oauth_state_client
    start = client.post("/admin/oauth/codex/start")
    assert start.status_code == 200, start.text
    body = start.json()
    record = sessions.get_session(body["session_id"])
    assert record is not None

    with patch(
        "corlinman_server.gateway.oauth.codex_pkce.exchange_code",
        new=AsyncMock(return_value=_FAKE_TOKENS),
    ), patch("corlinman_server.gateway.oauth.codex_pkce.write_auth_json"), patch(
        "corlinman_server.gateway.routes_admin_b.oauth._query_codex_oauth_models",
        new=AsyncMock(return_value=["gpt-4o", "gpt-5.5"]),
    ):
        resp = client.post(
            "/admin/oauth/codex/submit",
            json={
                "session_id": body["session_id"],
                "code": "thecode",
                "state": record["state"],
            },
        )

    assert resp.status_code == 200, resp.text
    on_disk = _on_disk(config_path)
    assert on_disk["providers"]["codex"] == {"kind": "codex", "enabled": True}
    assert on_disk["models"]["default"] == "gpt-5.5"
    assert on_disk["models"]["aliases"]["gpt-5.5"] == {
        "provider": "codex",
        "model": "gpt-5.5",
        "params": {},
    }
    assert on_disk["models"]["aliases"]["gpt-4o"]["provider"] == "codex"


def test_oauth_provisioning_creates_provider_named_alias_when_model_ids_conflict(
    oauth_state_client: tuple[AdminState, TestClient, Path],
) -> None:
    state, client, config_path = oauth_state_client
    snapshot: dict[str, Any] = state.extras["snapshot"]
    snapshot.update(
        {
            "models": {
                "aliases": {
                    "claude-opus-4-8": {
                        "provider": "relay",
                        "model": "claude-opus-4-8",
                        "params": {},
                    }
                },
            }
        }
    )
    credential = OAuthCredential.new(
        provider="anthropic",
        access_token="claude-code-access-token",
        refresh_token="claude-code-refresh-token",
        expires_at_ms=9_999_999_999_999,
        scope="user:inference",
        obtained_at_ms=1_000,
    )

    with patch(
        "corlinman_server.gateway.oauth.claude_code_import.read_claude_code_credentials",
        return_value=credential,
    ), patch(
        "corlinman_server.gateway.routes_admin_b.oauth._query_anthropic_oauth_models",
        new=AsyncMock(return_value=["claude-opus-4-8"]),
    ):
        resp = client.post("/admin/oauth/claude-code/import")

    assert resp.status_code == 200, resp.text
    on_disk = _on_disk(config_path)
    assert on_disk["models"]["default"] == "anthropic"
    assert on_disk["models"]["aliases"]["claude-opus-4-8"]["provider"] == "relay"
    assert on_disk["models"]["aliases"]["anthropic"] == {
        "provider": "anthropic",
        "model": "claude-opus-4-8",
        "params": {},
    }


def test_oauth_provisioning_preserves_existing_default(
    oauth_state_client: tuple[AdminState, TestClient, Path],
) -> None:
    state, client, config_path = oauth_state_client
    snapshot: dict[str, Any] = state.extras["snapshot"]
    snapshot.update(
        {
            "models": {
                "default": "operator-pick",
                "aliases": {
                    "operator-pick": {
                        "provider": "openai",
                        "model": "gpt-4o-mini",
                        "params": {},
                    }
                },
            }
        }
    )

    credential = OAuthCredential.new(
        provider="anthropic",
        access_token="claude-code-access-token",
        refresh_token="claude-code-refresh-token",
        expires_at_ms=9_999_999_999_999,
        scope="user:inference",
        obtained_at_ms=1_000,
    )

    with patch(
        "corlinman_server.gateway.oauth.claude_code_import.read_claude_code_credentials",
        return_value=credential,
    ), patch(
        "corlinman_server.gateway.routes_admin_b.oauth._query_anthropic_oauth_models",
        new=AsyncMock(return_value=["claude-opus-4-8"]),
    ):
        resp = client.post("/admin/oauth/claude-code/import")

    assert resp.status_code == 200, resp.text
    on_disk = _on_disk(config_path)
    assert on_disk["models"]["default"] == "operator-pick"
    assert on_disk["models"]["aliases"]["operator-pick"]["provider"] == "openai"
    assert on_disk["models"]["aliases"]["claude-opus-4-8"]["provider"] == "anthropic"
