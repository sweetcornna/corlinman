"""Repro + regression for SEC-07.

``GET /admin/config`` advertises a redacted snapshot, but ``_redact`` only
rewrote ``api_key.value`` / ``password_hash`` / ``secret_key``. Channel
secrets (``channels.telegram.bot_token``, ``channels.slack.app_token``,
``channels.qq.napcat_access_token`` …) and OAuth tokens
(``refresh_token`` / ``access_token``) were emitted verbatim and survived
the POST round-trip.

Acceptance: those leaf secrets come back as the REDACTED sentinel, and a
POST that echoes the sentinel back is re-merged from the live snapshot
(never written as ``None`` / never tripping the redacted-in-payload guard).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from corlinman_server.gateway.routes_admin_b.config_admin import config as config_routes
from corlinman_server.gateway.routes_admin_b.config_admin.config import (
    REDACTED_SENTINEL,
    _has_redacted,
    _merge_secrets_from,
    _redact,
    _rewrite_py_config,
)
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from fastapi import FastAPI

from ._admin_auth import authenticated_test_client, configure_admin_auth


def _sample_cfg() -> dict:
    return {
        "server": {"port": 8080},
        "channels": {
            "telegram": {
                "enabled": True,
                "bot_token": "123456:AAH-cleartext-telegram-secret",
            },
            "slack": {
                "enabled": True,
                "app_token": "xapp-1-cleartext-slack-app-secret",
                "bot_token": "xoxb-cleartext-slack-bot-secret",
            },
            "qq": {
                "enabled": True,
                "napcat_access_token": "napcat-cleartext-secret",
                # env-ref form must stay readable (parity with api_key.env)
                "access_token": {"env": "QQ_ACCESS_TOKEN"},
            },
            "wechat_official": {
                "token": "wechat-cleartext-verify-token",
                "app_secret": "wechat-cleartext-app-secret",
            },
        },
        "oauth": {
            "refresh_token": "rt-cleartext",
            "access_token": "at-cleartext",
            "client_secret": "cs-cleartext",
        },
        # env-ref api_key already handled; keep to prove no regression
        "api_key": {"value": "should-be-redacted", "env": "X"},
    }


def test_redact_leaks_channel_and_napcat_tokens_FAILS_BEFORE() -> None:
    """The core SEC-07 repro: every inline channel/oauth secret must be
    redacted, while env-ref forms stay readable."""
    snap = _sample_cfg()
    red = _redact(snap)

    ch = red["channels"]
    # --- the leaks the audit calls out ---
    assert ch["telegram"]["bot_token"] == REDACTED_SENTINEL
    assert ch["slack"]["app_token"] == REDACTED_SENTINEL
    assert ch["slack"]["bot_token"] == REDACTED_SENTINEL
    assert ch["qq"]["napcat_access_token"] == REDACTED_SENTINEL
    assert ch["wechat_official"]["token"] == REDACTED_SENTINEL
    assert ch["wechat_official"]["app_secret"] == REDACTED_SENTINEL

    # --- oauth leaf tokens ---
    assert red["oauth"]["refresh_token"] == REDACTED_SENTINEL
    assert red["oauth"]["access_token"] == REDACTED_SENTINEL
    assert red["oauth"]["client_secret"] == REDACTED_SENTINEL

    # --- existing behaviour preserved ---
    assert red["api_key"]["value"] == REDACTED_SENTINEL
    # env-ref forms stay readable
    assert ch["qq"]["access_token"] == {"env": "QQ_ACCESS_TOKEN"}
    # non-secret fields untouched
    assert red["server"]["port"] == 8080
    assert ch["telegram"]["enabled"] is True

    # The redacted snapshot must not still contain any cleartext secret.
    assert "cleartext" not in str(red)


def test_redact_secretref_value_form() -> None:
    """A ``napcat_access_token = { value = ".." }`` SecretRef must redact
    the inline value but keep an ``env`` reference readable, and round-trip
    cleanly through the POST merge."""
    base = {
        "channels": {
            "qq": {
                "napcat_access_token": {"value": "inline-secret", "env": "NAPCAT_TOKEN"},
            }
        }
    }
    red = _redact(base)
    sec = red["channels"]["qq"]["napcat_access_token"]
    assert sec["value"] == REDACTED_SENTINEL
    assert sec["env"] == "NAPCAT_TOKEN"
    assert "inline-secret" not in str(red)

    merged = _merge_secrets_from(red, base)
    assert merged["channels"]["qq"]["napcat_access_token"]["value"] == "inline-secret"
    assert not _has_redacted(merged)


def test_post_roundtrip_remerges_redacted_channel_secrets() -> None:
    """POST of the redacted snapshot must restore secrets from the live
    base, and the resulting merge must not still contain the sentinel."""
    base = _sample_cfg()
    redacted = _redact(base)

    merged = _merge_secrets_from(redacted, base)

    # Live values are restored verbatim.
    assert merged["channels"]["telegram"]["bot_token"] == base["channels"]["telegram"]["bot_token"]
    assert (
        merged["channels"]["qq"]["napcat_access_token"]
        == base["channels"]["qq"]["napcat_access_token"]
    )
    assert merged["channels"]["slack"]["app_token"] == base["channels"]["slack"]["app_token"]
    assert merged["oauth"]["refresh_token"] == base["oauth"]["refresh_token"]

    # No sentinel survives the round-trip → POST guard would NOT 400.
    assert not _has_redacted(merged)


def test_redacted_secret_with_no_base_is_not_written_as_none() -> None:
    """If the POST echoes the sentinel for a key that does not exist in the
    live base (operator added a fresh secret then re-submitted the GET'd
    snapshot), the merge must DROP it rather than write a literal ``None``
    or leave the sentinel."""
    new = {"channels": {"telegram": {"bot_token": REDACTED_SENTINEL}}}
    base: dict = {"channels": {"telegram": {}}}  # no live value

    merged = _merge_secrets_from(new, base)

    tg = merged["channels"]["telegram"]
    # The key must not be present as None and must not be the sentinel.
    assert tg.get("bot_token") is None
    assert "bot_token" not in tg or tg["bot_token"] is not None
    assert not _has_redacted(merged)


@pytest.mark.asyncio
async def test_full_config_rewrite_reconciles_qq_before_sidecar(
    tmp_path: Path,
) -> None:
    calls: list[tuple[dict, dict, Path]] = []

    class _Registry:
        async def reconcile_and_write_sidecar(self, channels, *, config, path) -> None:
            calls.append((channels, config, Path(path)))

    cfg = {
        "channels": {
            "qq": {
                "default_instance": "bot-a",
                "instances": {"bot-a": {"enabled": False}},
            }
        }
    }
    state = AdminState(py_config_path=tmp_path / "py-config.json")
    runtime_state = SimpleNamespace(qq_runtime_registry=_Registry())
    state.extras["app_state"] = SimpleNamespace(corlinman_state=runtime_state)

    await _rewrite_py_config(state, cfg)

    assert calls == [(cfg["channels"], cfg, state.py_config_path)]


def test_post_config_rolls_back_disk_and_snapshot_when_reconcile_fails(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "corlinman.toml"
    old_cfg = {
        "channels": {
            "qq": {
                "default_instance": "bot-a",
                "instances": {"bot-a": {"enabled": True}},
            }
        }
    }
    old_toml = config_routes._toml_dumps(old_cfg)
    config_path.write_text(old_toml, encoding="utf-8")
    live = {"value": old_cfg}

    class _Registry:
        async def reconcile_and_write_sidecar(self, channels, *, config, path) -> None:
            if config["channels"]["qq"]["instances"]["bot-a"]["enabled"] is False:
                raise RuntimeError("manager unavailable")

    state = AdminState(
        config_path=config_path,
        py_config_path=tmp_path / "py-config.json",
        config_loader=lambda: live["value"],
    )
    state.extras["config_swap_fn"] = lambda cfg: live.update(value=cfg)
    state.extras["app_state"] = SimpleNamespace(
        corlinman_state=SimpleNamespace(qq_runtime_registry=_Registry())
    )
    configure_admin_auth(state)
    set_admin_state(state)
    app = FastAPI()
    app.include_router(config_routes.router())
    try:
        client = authenticated_test_client(app)
        new_cfg = {
            "channels": {
                "qq": {
                    "default_instance": "bot-a",
                    "instances": {"bot-a": {"enabled": False}},
                }
            }
        }

        with pytest.raises(RuntimeError, match="manager unavailable"):
            client.post(
                "/admin/config",
                json={"toml": config_routes._toml_dumps(new_cfg)},
            )

        assert live["value"] == old_cfg
        assert config_routes._toml_loads(config_path.read_text()) == old_cfg
    finally:
        set_admin_state(None)
