"""Tests for ``corlinman_server.gateway.oauth.codex_external``.

Coverage:

* Missing ``~/.codex/auth.json`` → ``read_codex_status`` returns
  ``None`` (file-not-found is distinct from "found-but-unusable" so
  the HTTP layer can pick a sensible default).
* A synthetic well-formed auth file → ``detected=True`` with
  ``expires_at_ms`` extracted from the access_token JWT's ``exp`` claim
  and ``account_id`` from the id_token's ``sub`` / ``email``.
* Missing ``tokens`` block / empty ``access_token`` → ``detected=False``
  with no log noise (known half-installed state).
* Malformed JSON → returns ``detected=False`` and **does not raise**
  (we don't want a corrupt file to brick the status endpoint).
* ``CODEX_HOME`` env var honoured by ``_codex_auth_path``.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

import pytest

from corlinman_server.gateway.oauth import codex_external


def _jwt(payload: dict[str, Any]) -> str:
    """Build a fake JWT (header.payload.signature) for tests."""
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode("ascii")
    payload_b = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode("ascii")
    sig = base64.urlsafe_b64encode(b"sig").rstrip(b"=").decode("ascii")
    return f"{header}.{payload_b}.{sig}"


def _write(path: Path, body: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")


def test_missing_file_returns_none(tmp_path: Path) -> None:
    target = tmp_path / "auth.json"
    assert not target.exists()
    assert codex_external.read_codex_status(target) is None


def test_valid_file_detected_with_expiry(tmp_path: Path) -> None:
    target = tmp_path / "auth.json"
    exp_seconds = 1_900_000_000  # well into the future
    _write(
        target,
        {
            "tokens": {
                "access_token": _jwt({"exp": exp_seconds, "sub": "user-123"}),
                "refresh_token": "rt-abc",
                "id_token": _jwt({"sub": "user-123", "email": "operator@example.com"}),
            },
            "OPENAI_API_KEY": "sk-not-used",
            "last_refresh": "2026-05-18T00:00:00Z",
        },
    )
    status = codex_external.read_codex_status(target)
    assert status is not None
    assert status.detected is True
    assert status.expires_at_ms == exp_seconds * 1000
    # id_token's email beats sub when both present
    assert status.account_id == "operator@example.com"


def test_no_id_token_falls_back_to_none_account(tmp_path: Path) -> None:
    target = tmp_path / "auth.json"
    _write(target, {"tokens": {"access_token": "opaque-not-a-jwt"}})
    status = codex_external.read_codex_status(target)
    assert status is not None
    assert status.detected is True
    assert status.account_id is None
    assert status.expires_at_ms is None  # non-JWT token → no exp hint


def test_missing_tokens_block_returns_not_detected_quietly(tmp_path: Path) -> None:
    target = tmp_path / "auth.json"
    _write(target, {"OPENAI_API_KEY": "sk-only"})
    status = codex_external.read_codex_status(target)
    assert status is not None
    assert status.detected is False


def test_empty_access_token_returns_not_detected(tmp_path: Path) -> None:
    target = tmp_path / "auth.json"
    _write(target, {"tokens": {"access_token": "", "refresh_token": "rt"}})
    status = codex_external.read_codex_status(target)
    assert status is not None
    assert status.detected is False


def test_malformed_json_returns_not_detected_without_raising(tmp_path: Path) -> None:
    target = tmp_path / "auth.json"
    target.write_text("{ not json", encoding="utf-8")
    status = codex_external.read_codex_status(target)
    assert status is not None
    assert status.detected is False  # warn-and-continue, do not raise


def test_root_not_object_returns_not_detected(tmp_path: Path) -> None:
    target = tmp_path / "auth.json"
    target.write_text("[1, 2, 3]", encoding="utf-8")
    status = codex_external.read_codex_status(target)
    assert status is not None
    assert status.detected is False


def test_codex_home_env_overrides_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    resolved = codex_external._codex_auth_path()
    assert resolved == tmp_path / "auth.json"


def test_codex_home_empty_falls_back_to_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", "   ")
    resolved = codex_external._codex_auth_path()
    assert resolved == Path.home() / ".codex" / "auth.json"


def test_does_not_log_token_bytes_on_malformed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Even when warning about malformed JSON, the token must not leak."""
    target = tmp_path / "auth.json"
    secret_token_marker = "SECRETTOKENMARKER12345"
    # Build a file that's *almost* JSON but contains the secret as
    # garbage text; the parser will fail and warn — the token-shaped
    # text should NOT appear in any log record.
    target.write_text(
        '{"tokens": {"access_token": "' + secret_token_marker + '" BROKEN}}',
        encoding="utf-8",
    )
    caplog.set_level("WARNING")
    status = codex_external.read_codex_status(target)
    assert status is not None
    assert status.detected is False
    # Spot-check: secret must not appear in any captured log line.
    for record in caplog.records:
        assert secret_token_marker not in record.getMessage()
