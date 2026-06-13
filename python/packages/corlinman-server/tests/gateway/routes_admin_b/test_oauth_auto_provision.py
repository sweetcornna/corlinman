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
from fastapi.responses import JSONResponse
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


@pytest.mark.asyncio
async def test_oauth_provisioning_skips_when_config_path_unset() -> None:
    state = AdminState(config_loader=lambda: {}, config_path=None)

    with patch(
        "corlinman_server.gateway.routes_admin_b.oauth._query_anthropic_oauth_models",
        new=AsyncMock(),
    ) as query, patch(
        "corlinman_server.gateway.routes_admin_b.oauth._write_config_atomic"
    ) as write_config:
        err = await oauth_routes._provision_oauth_models(
            state,
            provider="anthropic",
            kind="anthropic",
            access_token="oauth-access-token",
        )

    assert err is None
    query.assert_not_awaited()
    write_config.assert_not_called()


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
        "oauth_provisioned": True,
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
        "oauth_provisioned": True,
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
    assert on_disk["providers"]["codex"] == {
        "kind": "codex",
        "enabled": True,
        "oauth_provisioned": True,
    }
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


def test_oauth_provisioning_uses_non_conflicting_alias_when_provider_alias_is_owned(
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
                    },
                    "anthropic": {
                        "provider": "legacy-anthropic",
                        "model": "claude-3-5-sonnet-latest",
                        "params": {},
                    },
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
    assert on_disk["models"]["default"] == "anthropic-claude-opus-4-8"
    assert on_disk["models"]["aliases"]["claude-opus-4-8"]["provider"] == "relay"
    assert on_disk["models"]["aliases"]["anthropic"]["provider"] == "legacy-anthropic"
    assert on_disk["models"]["aliases"]["anthropic-claude-opus-4-8"] == {
        "provider": "anthropic",
        "model": "claude-opus-4-8",
        "params": {},
    }


def test_anthropic_disconnect_disables_provisioned_slot_and_clears_default(
    oauth_state_client: tuple[AdminState, TestClient, Path],
) -> None:
    state, client, config_path = oauth_state_client
    snapshot: dict[str, Any] = state.extras["snapshot"]
    snapshot.update(
        {
            # Marked slot: this flow provisioned it, so disconnect may clean up.
            "providers": {
                "anthropic": {
                    "kind": "anthropic",
                    "enabled": True,
                    "oauth_provisioned": True,
                }
            },
            "models": {
                "default": "claude-opus-4-8",
                "aliases": {
                    "claude-opus-4-8": {
                        "provider": "anthropic",
                        "model": "claude-opus-4-8",
                        "params": {},
                    },
                    # A user-created alias that happens to point at the same
                    # provider must survive a transient disconnect.
                    "fast": {
                        "provider": "anthropic",
                        "model": "claude-opus-4-8",
                        "params": {"max_tokens": 256},
                    },
                    "relay": {
                        "provider": "relay",
                        "model": "claude-opus-4-8",
                        "params": {},
                    },
                },
            },
        }
    )

    resp = client.delete("/admin/oauth/anthropic")

    assert resp.status_code == 204, resp.text
    on_disk = _on_disk(config_path)
    # The provisioned slot is disabled and the dangling default cleared, but no
    # alias is removed (they revive on reconnect).
    assert on_disk["providers"]["anthropic"]["enabled"] is False
    assert "default" not in on_disk["models"]
    assert on_disk["models"]["aliases"]["claude-opus-4-8"]["provider"] == "anthropic"
    assert on_disk["models"]["aliases"]["fast"]["params"] == {"max_tokens": 256}
    assert on_disk["models"]["aliases"]["relay"]["provider"] == "relay"


def test_disconnect_leaves_unmarked_env_backed_provider_untouched(
    oauth_state_client: tuple[AdminState, TestClient, Path],
) -> None:
    state, client, config_path = oauth_state_client
    snapshot: dict[str, Any] = state.extras["snapshot"]
    # Operator-configured Anthropic slot with no api_key (authenticates via the
    # adapter's env-var fallback) and no provisioning marker. Disconnecting OAuth
    # must not disable it or clear its still-valid default.
    snapshot.update(
        {
            "providers": {"anthropic": {"kind": "anthropic", "enabled": True}},
            "models": {
                "default": "claude-opus-4-8",
                "aliases": {
                    "claude-opus-4-8": {
                        "provider": "anthropic",
                        "model": "claude-opus-4-8",
                        "params": {},
                    },
                },
            },
        }
    )
    oauth_routes._write_config_atomic(config_path, dict(snapshot))

    resp = client.delete("/admin/oauth/anthropic")

    assert resp.status_code == 204, resp.text
    on_disk = _on_disk(config_path)
    assert on_disk["providers"]["anthropic"]["enabled"] is True
    assert on_disk["models"]["default"] == "claude-opus-4-8"
    assert on_disk["models"]["aliases"]["claude-opus-4-8"]["provider"] == "anthropic"


def test_anthropic_disconnect_preserves_api_key_backed_provider(
    oauth_state_client: tuple[AdminState, TestClient, Path],
) -> None:
    state, client, config_path = oauth_state_client
    snapshot: dict[str, Any] = state.extras["snapshot"]
    snapshot.update(
        {
            "providers": {
                "anthropic": {
                    "kind": "anthropic",
                    "enabled": True,
                    "api_key": "sk-configured",
                }
            },
            "models": {
                "default": "claude-opus-4-8",
                "aliases": {
                    "claude-opus-4-8": {
                        "provider": "anthropic",
                        "model": "claude-opus-4-8",
                        "params": {},
                    },
                },
            },
        }
    )
    # Seed disk to match the loader snapshot so the assertions below verify the
    # on-disk TOML survives the disconnect — a no-op cleanup writes nothing, so
    # without this seed the file would stay empty and these reads would KeyError.
    oauth_routes._write_config_atomic(config_path, dict(snapshot))

    resp = client.delete("/admin/oauth/anthropic")

    assert resp.status_code == 204, resp.text
    on_disk = _on_disk(config_path)
    assert on_disk["providers"]["anthropic"]["enabled"] is True
    assert on_disk["models"]["default"] == "claude-opus-4-8"
    assert on_disk["models"]["aliases"]["claude-opus-4-8"]["provider"] == "anthropic"


def test_codex_disconnect_disables_provisioned_slot_and_clears_default(
    oauth_state_client: tuple[AdminState, TestClient, Path],
) -> None:
    state, client, config_path = oauth_state_client
    snapshot: dict[str, Any] = state.extras["snapshot"]
    snapshot.update(
        {
            "providers": {
                "codex": {
                    "kind": "codex",
                    "enabled": True,
                    "oauth_provisioned": True,
                }
            },
            "models": {
                "default": "gpt-5.5",
                "aliases": {
                    "gpt-5.5": {
                        "provider": "codex",
                        "model": "gpt-5.5",
                        "params": {},
                    },
                    "openai": {
                        "provider": "openai",
                        "model": "gpt-4o",
                        "params": {},
                    },
                },
            },
        }
    )

    with patch("corlinman_server.gateway.oauth.codex_pkce.delete_auth_json"):
        resp = client.delete("/admin/oauth/codex")

    assert resp.status_code == 204, resp.text
    on_disk = _on_disk(config_path)
    # Provisioned slot disabled, dangling default cleared, aliases preserved.
    assert on_disk["providers"]["codex"]["enabled"] is False
    assert "default" not in on_disk["models"]
    assert on_disk["models"]["aliases"]["gpt-5.5"]["provider"] == "codex"
    assert on_disk["models"]["aliases"]["openai"]["provider"] == "openai"


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


def test_oauth_provisioning_preserves_existing_shorthand_alias(
    oauth_state_client: tuple[AdminState, TestClient, Path],
) -> None:
    state, client, config_path = oauth_state_client
    snapshot: dict[str, Any] = state.extras["snapshot"]
    # A providerless shorthand alias (e.g. bulk-created for the legacy path)
    # whose name collides with a discovered model id must not be rerouted to the
    # OAuth provider; only genuinely-new model ids get fresh aliases.
    snapshot.update(
        {
            "models": {
                "aliases": {
                    "claude-opus-4-8": "claude-opus-4-8",
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
        new=AsyncMock(return_value=["claude-opus-4-8", "claude-sonnet-4-6"]),
    ):
        resp = client.post("/admin/oauth/claude-code/import")

    assert resp.status_code == 200, resp.text
    on_disk = _on_disk(config_path)
    aliases = on_disk["models"]["aliases"]
    # The user's shorthand is untouched...
    assert aliases["claude-opus-4-8"] == "claude-opus-4-8"
    # ...while the genuinely-new model gets a fresh provider-backed alias.
    assert aliases["claude-sonnet-4-6"]["provider"] == "anthropic"
    assert on_disk["models"]["default"] == "claude-sonnet-4-6"


def test_oauth_provisioning_preserves_provider_named_shorthand_alias(
    oauth_state_client: tuple[AdminState, TestClient, Path],
) -> None:
    state, client, config_path = oauth_state_client
    snapshot: dict[str, Any] = state.extras["snapshot"]
    # Every discovered id already collides AND the provider name itself is taken
    # by a providerless shorthand. The fallback must mint a suffixed alias rather
    # than overwrite the user's `anthropic` shorthand.
    snapshot.update(
        {
            "models": {
                "aliases": {
                    "claude-opus-4-8": {
                        "provider": "relay",
                        "model": "claude-opus-4-8",
                        "params": {},
                    },
                    "anthropic": "claude-opus-4-8",
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
    aliases = on_disk["models"]["aliases"]
    # The user's shorthand and the relay alias are untouched...
    assert aliases["anthropic"] == "claude-opus-4-8"
    assert aliases["claude-opus-4-8"]["provider"] == "relay"
    # ...and a suffixed alias carries the OAuth provider + becomes the default.
    assert aliases["anthropic-claude-opus-4-8"]["provider"] == "anthropic"
    assert on_disk["models"]["default"] == "anthropic-claude-opus-4-8"


@pytest.mark.asyncio
async def test_oauth_provisioning_respects_manual_slot_of_different_kind(
    oauth_state_client: tuple[AdminState, TestClient, Path],
) -> None:
    state, _client, config_path = oauth_state_client
    snapshot: dict[str, Any] = state.extras["snapshot"]
    # Operator manually pointed `[providers.codex]` at a relay of a different
    # kind. A Codex OAuth login must not repurpose that slot to the codex adapter
    # (manual config wins, cf. _auto_inject_codex).
    snapshot.update(
        {
            "providers": {
                "codex": {
                    "kind": "openai_compatible",
                    "enabled": True,
                    "base_url": "https://relay.internal/v1",
                }
            },
        }
    )
    # Seed disk so the assertions read real on-disk TOML even if provisioning
    # leaves the config untouched (manual-config-wins early return).
    oauth_routes._write_config_atomic(config_path, dict(snapshot))

    with patch(
        "corlinman_server.gateway.routes_admin_b.oauth._query_codex_oauth_models",
        new=AsyncMock(return_value=["gpt-5.5"]),
    ):
        err = await oauth_routes._provision_oauth_models(
            state,
            provider="codex",
            kind="codex",
            access_token="codex-access-token",
        )

    assert err is None
    on_disk = _on_disk(config_path)
    # The manual slot's kind/base_url are preserved and no codex aliases minted.
    assert on_disk["providers"]["codex"]["kind"] == "openai_compatible"
    assert on_disk["providers"]["codex"]["base_url"] == "https://relay.internal/v1"
    assert "gpt-5.5" not in (on_disk.get("models", {}).get("aliases") or {})


def test_anthropic_disconnect_keeps_default_naming_unrelated_alias(
    oauth_state_client: tuple[AdminState, TestClient, Path],
) -> None:
    state, client, config_path = oauth_state_client
    snapshot: dict[str, Any] = state.extras["snapshot"]
    # The Anthropic slot IS provisioned (marked), so disconnect disables it and
    # reaches the default-clearing branch. But the default "anthropic" resolves
    # to an alias owned by another provider, so it is not dangling and is kept.
    snapshot.update(
        {
            "providers": {
                "anthropic": {
                    "kind": "anthropic",
                    "enabled": True,
                    "oauth_provisioned": True,
                }
            },
            "models": {
                "default": "anthropic",
                "aliases": {
                    "anthropic": {
                        "provider": "relay",
                        "model": "claude-opus-4-8",
                        "params": {},
                    },
                },
            },
        }
    )
    oauth_routes._write_config_atomic(config_path, dict(snapshot))

    resp = client.delete("/admin/oauth/anthropic")

    assert resp.status_code == 204, resp.text
    on_disk = _on_disk(config_path)
    assert on_disk["providers"]["anthropic"]["enabled"] is False
    assert on_disk["models"]["default"] == "anthropic"
    assert on_disk["models"]["aliases"]["anthropic"]["provider"] == "relay"


@pytest.mark.asyncio
async def test_oauth_provisioning_does_not_shadow_raw_default_model(
    oauth_state_client: tuple[AdminState, TestClient, Path],
) -> None:
    state, _client, config_path = oauth_state_client
    snapshot: dict[str, Any] = state.extras["snapshot"]
    # The operator's default is a raw model id with no alias entry, resolving
    # through their existing setup. A Codex login that discovers the same id must
    # not mint an alias for it (resolve() checks aliases first, which would
    # silently reroute the default to the OAuth provider).
    snapshot.update({"models": {"default": "gpt-5.5"}})
    oauth_routes._write_config_atomic(config_path, dict(snapshot))

    with patch(
        "corlinman_server.gateway.routes_admin_b.oauth._query_codex_oauth_models",
        new=AsyncMock(return_value=["gpt-5.5"]),
    ):
        err = await oauth_routes._provision_oauth_models(
            state,
            provider="codex",
            kind="codex",
            access_token="codex-access-token",
        )

    assert err is None
    on_disk = _on_disk(config_path)
    aliases = on_disk["models"].get("aliases") or {}
    # The raw default is preserved and NOT shadowed by a "gpt-5.5" alias...
    assert on_disk["models"]["default"] == "gpt-5.5"
    assert "gpt-5.5" not in aliases
    # ...but the provider is still bindable via its provider-named alias.
    assert aliases["codex"]["provider"] == "codex"


def test_disconnect_spares_provisioned_slot_adopted_via_api_key(
    oauth_state_client: tuple[AdminState, TestClient, Path],
) -> None:
    state, client, config_path = oauth_state_client
    snapshot: dict[str, Any] = state.extras["snapshot"]
    # Slot was provisioned (marked) but the operator later adopted it by adding
    # an api_key via /admin/credentials. The marker lingers, but disconnect must
    # treat the now manually-credentialed slot as off-limits.
    snapshot.update(
        {
            "providers": {
                "anthropic": {
                    "kind": "anthropic",
                    "enabled": True,
                    "oauth_provisioned": True,
                    "api_key": "sk-adopted",
                }
            },
            "models": {
                "default": "claude-opus-4-8",
                "aliases": {
                    "claude-opus-4-8": {
                        "provider": "anthropic",
                        "model": "claude-opus-4-8",
                        "params": {},
                    },
                },
            },
        }
    )
    oauth_routes._write_config_atomic(config_path, dict(snapshot))

    resp = client.delete("/admin/oauth/anthropic")

    assert resp.status_code == 204, resp.text
    on_disk = _on_disk(config_path)
    assert on_disk["providers"]["anthropic"]["enabled"] is True
    assert on_disk["models"]["default"] == "claude-opus-4-8"


def test_codex_disconnect_keeps_token_when_config_cleanup_fails(
    oauth_state_client: tuple[AdminState, TestClient, Path],
) -> None:
    state, client, _config_path = oauth_state_client
    snapshot: dict[str, Any] = state.extras["snapshot"]
    snapshot.update(
        {
            "providers": {
                "codex": {
                    "kind": "codex",
                    "enabled": True,
                    "oauth_provisioned": True,
                }
            },
        }
    )

    # Simulate the config write failing during cleanup. The token deletion must
    # NOT run, so we never strand an enabled slot pointed at a deleted token.
    cleanup_error = JSONResponse(status_code=500, content={"error": "write_failed"})
    with patch(
        "corlinman_server.gateway.routes_admin_b.oauth._cleanup_oauth_provider_config",
        new=AsyncMock(return_value=cleanup_error),
    ), patch(
        "corlinman_server.gateway.oauth.codex_pkce.delete_auth_json"
    ) as delete_token:
        resp = client.delete("/admin/oauth/codex")

    assert resp.status_code == 500, resp.text
    delete_token.assert_not_called()
