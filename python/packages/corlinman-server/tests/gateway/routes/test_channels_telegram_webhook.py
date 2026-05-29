"""Route-level tests for ``gateway/routes/channels.py`` (TEST-007).

The Telegram *webhook* route — ``POST /v1/channels/telegram/webhook``
(plus its legacy ``/channels/telegram/webhook`` alias) — had no route
coverage. The existing ``test_admin_channels_telegram.py`` only covers
the unrelated ``/admin/channels/telegram/*`` admin surface, and no R6
test exercises ``_verify_secret`` or the inbound ``_handle`` flow.

What we assert against the real router (no mocked handler):

* secret mismatch / missing header → 401 ``unauthorized`` (the
  ``X-Telegram-Bot-Api-Secret-Token`` constant-time gate).
* empty-config (``secret_token=""``) disables the check → 200 ``ok``
  with or without a header.
* valid secret → the body is decoded into a real
  ``corlinman_channels.telegram.Update`` and handed to the real
  ``process_update`` → 200 ``{"ok": true}``.
* a malformed JSON body (after a passing secret) → 400 ``invalid_update``.

A message-less ``{"update_id": N}`` update is a clean no-op inside
``process_update`` (verified: returns ``None``), so the happy path needs
no live Telegram HTTP client.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("corlinman_channels.telegram_webhook")

from corlinman_server.gateway.routes.channels import (  # noqa: E402
    SECRET_HEADER,
    TelegramWebhookState,
    router,
)
from fastapi.testclient import TestClient  # noqa: E402


class _NoopTelegramHttp:
    """Stand-in for ``corlinman_channels.TelegramHttp``.

    A message-less update never touches the HTTP client (no media to
    download, no reply to send), so this placeholder is enough for the
    happy path. It is NOT a mock of the route handler — the real
    ``process_update`` runs against it.
    """


def _state(secret_token: str) -> TelegramWebhookState:
    return TelegramWebhookState(
        secret_token=secret_token,
        bot_id=42,
        bot_username="cornbot",
        data_dir=Path(tempfile.mkdtemp()),
        http=_NoopTelegramHttp(),
        hooks=None,
    )


def _client(secret_token: str) -> TestClient:
    app = fastapi.FastAPI()
    app.include_router(router(_state(secret_token)))
    return TestClient(app)


def _update() -> dict[str, Any]:
    """A minimal, message-less Telegram Update — a clean no-op."""
    return {"update_id": 1}


# ---------------------------------------------------------------------------
# secret gate
# ---------------------------------------------------------------------------


def test_secret_mismatch_returns_401() -> None:
    with _client("s3cr3t") as client:
        resp = client.post(
            "/v1/channels/telegram/webhook",
            json=_update(),
            headers={SECRET_HEADER: "wrong-secret"},
        )
    assert resp.status_code == 401, resp.text
    body = resp.json()
    assert body["error"] == "unauthorized"
    assert SECRET_HEADER in body["message"]


def test_missing_secret_header_returns_401_when_configured() -> None:
    """No secret header at all, with a secret configured → 401 (the
    constant-time compare treats ``None`` as the empty string)."""
    with _client("s3cr3t") as client:
        resp = client.post("/v1/channels/telegram/webhook", json=_update())
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"] == "unauthorized"


# ---------------------------------------------------------------------------
# empty-config behaviour (secret check disabled)
# ---------------------------------------------------------------------------


def test_empty_secret_disables_check_accepts_without_header() -> None:
    """Empty ``secret_token`` disables the gate (local-dev tunnels that
    strip the header) → 200 ``ok`` even with no header at all."""
    with _client("") as client:
        resp = client.post("/v1/channels/telegram/webhook", json=_update())
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}


def test_empty_secret_disables_check_accepts_any_header() -> None:
    with _client("") as client:
        resp = client.post(
            "/v1/channels/telegram/webhook",
            json=_update(),
            headers={SECRET_HEADER: "anything-goes"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}


# ---------------------------------------------------------------------------
# valid secret → real Update decode + process_update → 200
# ---------------------------------------------------------------------------


def test_valid_secret_processes_update_returns_200_ok() -> None:
    with _client("s3cr3t") as client:
        resp = client.post(
            "/v1/channels/telegram/webhook",
            json=_update(),
            headers={SECRET_HEADER: "s3cr3t"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}


def test_legacy_alias_path_also_processes_update() -> None:
    """The legacy ``/channels/telegram/webhook`` alias shares the same
    handler — existing webhook registrations keep working through the port."""
    with _client("s3cr3t") as client:
        resp = client.post(
            "/channels/telegram/webhook",
            json=_update(),
            headers={SECRET_HEADER: "s3cr3t"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}


# ---------------------------------------------------------------------------
# malformed body (after a passing secret) → 400 invalid_update
# ---------------------------------------------------------------------------


def test_valid_secret_but_malformed_json_returns_400() -> None:
    with _client("s3cr3t") as client:
        resp = client.post(
            "/v1/channels/telegram/webhook",
            content=b"this-is-not-json",
            headers={
                SECRET_HEADER: "s3cr3t",
                "content-type": "application/json",
            },
        )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "invalid_update"
