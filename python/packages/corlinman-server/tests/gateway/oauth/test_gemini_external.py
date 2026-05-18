"""Tests for ``corlinman_server.gateway.oauth.gemini_external``.

Coverage mirrors :mod:`test_codex_external`:

* Missing file → ``None``.
* Synthetic well-formed file → ``detected=True`` with ``expiry_date``
  (Google CLI canonical, ms) honoured and id_token email surfaced.
* Falls back to ``expires_at_ms`` when ``expiry_date`` is absent.
* Empty / missing ``access_token`` → ``detected=False``.
* Malformed JSON → ``detected=False`` without raising.
* ``GEMINI_HOME`` env var honoured.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from corlinman_server.gateway.oauth import gemini_external


def _jwt(payload: dict[str, Any]) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode("ascii")
    payload_b = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode("ascii")
    sig = base64.urlsafe_b64encode(b"sig").rstrip(b"=").decode("ascii")
    return f"{header}.{payload_b}.{sig}"


def _write(path: Path, body: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")


def test_missing_file_returns_none(tmp_path: Path) -> None:
    target = tmp_path / "oauth_creds.json"
    assert not target.exists()
    assert gemini_external.read_gemini_status(target) is None


def test_valid_file_detected(tmp_path: Path) -> None:
    target = tmp_path / "oauth_creds.json"
    _write(
        target,
        {
            "access_token": "ya29.opaque",
            "refresh_token": "1//rt",
            "scope": "https://www.googleapis.com/auth/cloud-platform openid email",
            "token_type": "Bearer",
            "id_token": _jwt({"sub": "google-uid-1", "email": "operator@example.com"}),
            "expiry_date": 1_900_000_000_000,  # ms
        },
    )
    status = gemini_external.read_gemini_status(target)
    assert status is not None
    assert status.detected is True
    assert status.expires_at_ms == 1_900_000_000_000
    assert status.account_id == "operator@example.com"


def test_expires_at_ms_fallback(tmp_path: Path) -> None:
    """When ``expiry_date`` is missing, ``expires_at_ms`` is honoured."""
    target = tmp_path / "oauth_creds.json"
    _write(
        target,
        {
            "access_token": "ya29.x",
            "expires_at_ms": 1_800_000_000_000,
        },
    )
    status = gemini_external.read_gemini_status(target)
    assert status is not None
    assert status.detected is True
    assert status.expires_at_ms == 1_800_000_000_000


def test_no_id_token_no_account(tmp_path: Path) -> None:
    target = tmp_path / "oauth_creds.json"
    _write(target, {"access_token": "ya29.x"})
    status = gemini_external.read_gemini_status(target)
    assert status is not None
    assert status.detected is True
    assert status.account_id is None
    assert status.expires_at_ms is None


def test_empty_access_token_returns_not_detected(tmp_path: Path) -> None:
    target = tmp_path / "oauth_creds.json"
    _write(target, {"access_token": "", "refresh_token": "1//rt"})
    status = gemini_external.read_gemini_status(target)
    assert status is not None
    assert status.detected is False


def test_malformed_json_returns_not_detected_without_raising(tmp_path: Path) -> None:
    target = tmp_path / "oauth_creds.json"
    target.write_text("{ not json", encoding="utf-8")
    status = gemini_external.read_gemini_status(target)
    assert status is not None
    assert status.detected is False


def test_root_not_object_returns_not_detected(tmp_path: Path) -> None:
    target = tmp_path / "oauth_creds.json"
    target.write_text('"just-a-string"', encoding="utf-8")
    status = gemini_external.read_gemini_status(target)
    assert status is not None
    assert status.detected is False


def test_gemini_home_env_overrides_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_HOME", str(tmp_path))
    resolved = gemini_external._gemini_auth_path()
    assert resolved == tmp_path / "oauth_creds.json"


def test_gemini_home_empty_falls_back_to_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_HOME", "   ")
    resolved = gemini_external._gemini_auth_path()
    assert resolved == Path.home() / ".gemini" / "oauth_creds.json"


def test_does_not_log_token_bytes_on_malformed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    target = tmp_path / "oauth_creds.json"
    secret = "GEMINISECRETTOKEN9999"
    target.write_text(
        '{"access_token": "' + secret + '" BROKEN}', encoding="utf-8"
    )
    caplog.set_level("WARNING")
    status = gemini_external.read_gemini_status(target)
    assert status is not None
    assert status.detected is False
    for record in caplog.records:
        assert secret not in record.getMessage()
