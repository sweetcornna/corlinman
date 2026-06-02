"""Tests for ``PUT /admin/channels/{channel}/config`` — the per-channel
editable secrets / base_url / id-whitelist / keyword-filter / flag writer.

Covers the round-trip contract:

* a field written through the PUT shows up in ``state.channels_config`` and
  is handed to the wired ``channels_writer``;
* secrets honour the redaction-merge contract (``***REDACTED***`` keeps the
  live value, a fresh value overwrites it) and are never echoed back;
* unknown channels 404 and unknown fields 400;
* numeric id whitelists are coerced + validated.

Mirrors the FastAPI TestClient pattern in ``test_admin_channels_uniform.py``.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")

from corlinman_server.gateway.routes_admin_a import (
    AdminState,
    build_router,
    set_admin_state,
)
from corlinman_server.gateway.routes_admin_a._session_store import (
    AdminSessionStore,
)
from corlinman_server.gateway.routes_admin_a.auth import hash_password
from fastapi import FastAPI
from fastapi.testclient import TestClient

REDACTED = "***REDACTED***"


def _basic_auth_header() -> str:
    token = base64.b64encode(b"admin:rootroot").decode("ascii")
    return f"Basic {token}"


class _RecordingWriter:
    """Captures every ``channels_writer(cfg)`` call so the test can assert
    the persisted snapshot. Synchronous — the route awaits when needed."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, cfg: dict[str, Any]) -> None:
        # Deep-ish copy of the per-channel sections so later mutations of
        # the live dict don't retroactively change what we recorded.
        self.calls.append({k: dict(v) for k, v in cfg.items()})


@pytest.fixture()
def writer() -> _RecordingWriter:
    return _RecordingWriter()


@pytest.fixture()
def base_state(tmp_path: Path, writer: _RecordingWriter) -> Iterator[AdminState]:
    channels_config: dict[str, Any] = {
        "qq": {
            "enabled": True,
            "ws_url": "ws://127.0.0.1:3001",
            "access_token": "live-napcat-token",
            "self_ids": [10001],
        },
        "telegram": {
            "enabled": True,
            "bot_token": "111:live-bot-token",
            "allowed_chat_ids": [42],
            "keyword_filter": ["hi"],
            "require_mention_in_groups": False,
        },
        "discord": {
            "enabled": True,
            "bot_token": "live-discord-token",
            "allowed_channel_ids": ["111"],
        },
        "feishu": {
            "enabled": True,
            "app_id": "cli_public",
            "app_secret": "live-feishu-secret",
        },
    }
    state = AdminState(
        data_dir=tmp_path,
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
        channels_config=channels_config,
        channels_writer=writer,
    )
    set_admin_state(state)
    try:
        yield state
    finally:
        set_admin_state(None)


@pytest.fixture()
def client(base_state: AdminState) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(build_router())
    with TestClient(app, headers={"Authorization": _basic_auth_header()}) as c:
        yield c


# ---------------------------------------------------------------------------
# Round-trip: base_url + filter + numeric id whitelist
# ---------------------------------------------------------------------------


def test_round_trip_url_filter_ids(
    client: TestClient, base_state: AdminState, writer: _RecordingWriter
) -> None:
    resp = client.put(
        "/admin/channels/telegram/config",
        json={
            "urls": {"base_url": "https://tg.proxy.cn"},
            "filters": {"keyword_filter": ["hello", "hey"]},
            "ids": {"allowed_chat_ids": ["7", "8"]},
            "flags": {"require_mention_in_groups": True},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert set(body["wrote"]) == {
        "base_url",
        "keyword_filter",
        "allowed_chat_ids",
        "require_mention_in_groups",
    }
    # Persisted into the live config dict.
    tg = base_state.channels_config["telegram"]
    assert tg["base_url"] == "https://tg.proxy.cn"
    assert tg["keyword_filter"] == ["hello", "hey"]
    # Numeric ids coerced from strings to int.
    assert tg["allowed_chat_ids"] == [7, 8]
    assert tg["require_mention_in_groups"] is True
    # Handed to the writer exactly once.
    assert len(writer.calls) == 1
    assert writer.calls[0]["telegram"]["base_url"] == "https://tg.proxy.cn"
    # Status-style projection surfaces the new base_url (non-secret).
    assert body["config_keys"]["base_url"] == "https://tg.proxy.cn"


# ---------------------------------------------------------------------------
# Secrets: redaction-merge contract
# ---------------------------------------------------------------------------


def test_secret_redacted_keeps_live_value(
    client: TestClient, base_state: AdminState
) -> None:
    """A ``***REDACTED***`` echo must NOT clobber the live secret."""
    resp = client.put(
        "/admin/channels/telegram/config",
        json={"secrets": {"bot_token": REDACTED}, "flags": {}},
    )
    assert resp.status_code == 200, resp.text
    assert base_state.channels_config["telegram"]["bot_token"] == "111:live-bot-token"
    # Secret value is never echoed; only its name appears in ``wrote``.
    assert "bot_token" in resp.json()["wrote"]
    assert REDACTED not in resp.text
    assert "111:live-bot-token" not in resp.text


def test_secret_fresh_value_overwrites(
    client: TestClient, base_state: AdminState
) -> None:
    resp = client.put(
        "/admin/channels/telegram/config",
        json={"secrets": {"bot_token": "222:rotated"}},
    )
    assert resp.status_code == 200, resp.text
    assert base_state.channels_config["telegram"]["bot_token"] == "222:rotated"
    # config_keys never carries the secret.
    assert "bot_token" not in resp.json()["config_keys"]


def test_secret_redacted_with_no_live_value_is_400(
    client: TestClient, base_state: AdminState
) -> None:
    # discord has no app-level secret beyond bot_token; redact a key that
    # has no live value behind it on a freshly-stubbed channel.
    base_state.channels_config["slack"] = {"enabled": False}
    resp = client.put(
        "/admin/channels/slack/config",
        json={"secrets": {"app_token": REDACTED}},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "redacted_sentinel_in_payload"


# ---------------------------------------------------------------------------
# app_id is a public (non-secret) field; app_secret stays a secret
# ---------------------------------------------------------------------------


def test_feishu_app_id_public_secret_separate(
    client: TestClient, base_state: AdminState
) -> None:
    resp = client.put(
        "/admin/channels/feishu/config",
        json={
            "urls": {"app_id": "cli_rotated", "api_base": "https://open.larksuite.com"},
            "secrets": {"app_secret": "fresh-secret"},
            "ids": {"allowed_chat_ids": ["oc_9"]},
        },
    )
    assert resp.status_code == 200, resp.text
    fs = base_state.channels_config["feishu"]
    assert fs["app_id"] == "cli_rotated"
    assert fs["api_base"] == "https://open.larksuite.com"
    assert fs["app_secret"] == "fresh-secret"
    assert fs["allowed_chat_ids"] == ["oc_9"]
    ck = resp.json()["config_keys"]
    # Public id + base surfaced, secret never.
    assert ck["app_id"] == "cli_rotated"
    assert ck["api_base"] == "https://open.larksuite.com"
    assert "app_secret" not in ck


# ---------------------------------------------------------------------------
# Validation + error envelopes
# ---------------------------------------------------------------------------


def test_unknown_channel_is_404(client: TestClient) -> None:
    resp = client.put("/admin/channels/nope/config", json={"flags": {}})
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "unknown_channel"


def test_unknown_field_is_400(client: TestClient) -> None:
    resp = client.put(
        "/admin/channels/discord/config",
        json={"flags": {"not_a_flag": True}},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "unknown_field"


def test_non_numeric_id_is_400(client: TestClient) -> None:
    resp = client.put(
        "/admin/channels/telegram/config",
        json={"ids": {"allowed_chat_ids": ["not-a-number"]}},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_id"


def test_empty_keyword_is_400(client: TestClient) -> None:
    resp = client.put(
        "/admin/channels/discord/config",
        json={"filters": {"keyword_filter": ["ok", "  "]}},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_keyword"


def test_missing_writer_is_503(base_state: AdminState) -> None:
    base_state.channels_writer = None
    app = FastAPI()
    app.include_router(build_router())
    with TestClient(app, headers={"Authorization": _basic_auth_header()}) as c:
        resp = c.put(
            "/admin/channels/telegram/config",
            json={"flags": {"require_mention_in_groups": True}},
        )
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "channels_writer_missing"


def test_auto_stub_missing_section(
    client: TestClient, base_state: AdminState, writer: _RecordingWriter
) -> None:
    """Writing to a channel with no [channels.X] section yet stubs it so the
    setup wizard can pre-fill config before flipping enabled."""
    assert "slack" not in base_state.channels_config
    resp = client.put(
        "/admin/channels/slack/config",
        json={
            "secrets": {"app_token": "xapp-1", "bot_token": "xoxb-1"},
            "urls": {"api_base": "https://slack.example"},
        },
    )
    assert resp.status_code == 200, resp.text
    section = base_state.channels_config["slack"]
    assert section["app_token"] == "xapp-1"
    assert section["api_base"] == "https://slack.example"
    # The stub did NOT silently enable the channel.
    assert section.get("enabled", False) is False
    assert writer.calls and "slack" in writer.calls[-1]


def test_qq_secret_and_ws_url_round_trip(
    client: TestClient, base_state: AdminState
) -> None:
    resp = client.put(
        "/admin/channels/qq/config",
        json={
            "secrets": {"access_token": "rotated-napcat"},
            "urls": {"ws_url": "ws://10.0.0.5:3001"},
            "ids": {"self_ids": ["20002"]},
        },
    )
    assert resp.status_code == 200, resp.text
    qq = base_state.channels_config["qq"]
    assert qq["access_token"] == "rotated-napcat"
    assert qq["ws_url"] == "ws://10.0.0.5:3001"
    assert qq["self_ids"] == [20002]
    # access_token (secret) never echoed; ws_url surfaced.
    ck = resp.json()["config_keys"]
    assert ck["ws_url"] == "ws://10.0.0.5:3001"
    assert "access_token" not in ck


def test_requires_auth(base_state: AdminState) -> None:
    app = FastAPI()
    app.include_router(build_router())
    with TestClient(app) as anonymous:
        resp = anonymous.put(
            "/admin/channels/telegram/config",
            json={"flags": {"require_mention_in_groups": True}},
        )
    assert resp.status_code == 401
