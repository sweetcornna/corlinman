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

from corlinman_server.gateway.routes_admin_b.config import (
    REDACTED_SENTINEL,
    _has_redacted,
    _merge_secrets_from,
    _redact,
)


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
    assert merged["channels"]["qq"]["napcat_access_token"] == base["channels"]["qq"]["napcat_access_token"]
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
