"""Tests for ``corlinman_server.gateway.oauth.storage``.

Coverage:

* ``OAuthCredential`` JSON round-trip via ``save_credential`` +
  ``load_credential`` preserves every field.
* The file on disk has mode ``0o600`` (operator-only read/write).
* ``__repr__`` redacts both ``access_token`` and ``refresh_token`` so a
  stray ``logger.info(cred)`` cannot leak tokens.
* Malformed JSON loads as ``None`` instead of raising — operators
  recovering from a corrupted file should just be able to re-OAuth.
* ``delete_credential`` removes the file and returns True; on a missing
  file it returns False without raising.
* Provider slug validation rejects path-traversal attempts.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from corlinman_server.gateway.oauth.storage import (
    OAuthCredential,
    delete_credential,
    load_credential,
    save_credential,
)


def _make_cred() -> OAuthCredential:
    return OAuthCredential.new(
        provider="anthropic",
        access_token="sk-ant-oauth-abcdef-1234567890",
        refresh_token="rt-zyxwvu-0987654321",
        expires_at_ms=1_800_000_000_000,
        scope="user:inference",
        obtained_at_ms=1_700_000_000_000,
    )


def test_round_trip_preserves_every_field(tmp_path: Path) -> None:
    cred = _make_cred()
    path = save_credential(tmp_path, cred)
    assert path.exists()
    loaded = load_credential(tmp_path, "anthropic")
    assert loaded is not None
    assert loaded.provider == "anthropic"
    assert loaded.access_token == cred.access_token
    assert loaded.refresh_token == cred.refresh_token
    assert loaded.expires_at_ms == cred.expires_at_ms
    assert loaded.scope == cred.scope
    assert loaded.obtained_at_ms == cred.obtained_at_ms


def test_file_mode_is_0600(tmp_path: Path) -> None:
    cred = _make_cred()
    path = save_credential(tmp_path, cred)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_repr_redacts_tokens() -> None:
    cred = _make_cred()
    out = repr(cred)
    # The full secrets must not appear anywhere in the repr.
    assert "sk-ant-oauth-abcdef-1234567890" not in out
    assert "rt-zyxwvu-0987654321" not in out
    # We surface a short prefix so two creds are distinguishable at a
    # glance, but only the first 6 chars.
    assert "sk-ant" in out
    assert "rt-zyx" in out
    # And the ellipsis marker.
    assert "…" in out


def test_repr_handles_none_refresh_token() -> None:
    cred = OAuthCredential.new(
        provider="anthropic",
        access_token="abcdefghij",
        refresh_token=None,
        expires_at_ms=None,
    )
    out = repr(cred)
    assert "<unset>" in out
    assert "abcdef" in out


def test_load_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert load_credential(tmp_path, "anthropic") is None


def test_load_returns_none_for_malformed_json(tmp_path: Path) -> None:
    oauth_dir = tmp_path / ".oauth"
    oauth_dir.mkdir()
    (oauth_dir / "anthropic.json").write_text("{ not json", encoding="utf-8")
    assert load_credential(tmp_path, "anthropic") is None


def test_load_returns_none_when_access_token_missing(tmp_path: Path) -> None:
    oauth_dir = tmp_path / ".oauth"
    oauth_dir.mkdir()
    (oauth_dir / "anthropic.json").write_text(
        json.dumps({"provider": "anthropic", "refresh_token": "rt"}),
        encoding="utf-8",
    )
    assert load_credential(tmp_path, "anthropic") is None


def test_delete_removes_file_and_reports_true(tmp_path: Path) -> None:
    save_credential(tmp_path, _make_cred())
    assert delete_credential(tmp_path, "anthropic") is True
    assert load_credential(tmp_path, "anthropic") is None


def test_delete_missing_returns_false(tmp_path: Path) -> None:
    assert delete_credential(tmp_path, "anthropic") is False


def test_invalid_provider_slug_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        load_credential(tmp_path, "../../etc/passwd")
    with pytest.raises(ValueError):
        load_credential(tmp_path, "Anthropic")  # uppercase rejected
    with pytest.raises(ValueError):
        load_credential(tmp_path, "x" * 33)  # too long


def test_is_expired_respects_skew() -> None:
    import time

    now_ms = int(time.time() * 1000)
    cred = OAuthCredential(
        provider="anthropic",
        access_token="t",
        refresh_token=None,
        expires_at_ms=now_ms + 60_000,  # expires in 60s
        scope=None,
        obtained_at_ms=now_ms,
    )
    assert cred.is_expired() is False
    assert cred.is_expired(skew_seconds=120) is True


def test_with_refreshed_replaces_token_fields() -> None:
    cred = _make_cred()
    refreshed = cred.with_refreshed(
        access_token="new-access-token",
        refresh_token="new-refresh-token",
        expires_at_ms=2_000_000_000_000,
    )
    assert refreshed.access_token == "new-access-token"
    assert refreshed.refresh_token == "new-refresh-token"
    assert refreshed.expires_at_ms == 2_000_000_000_000
    # obtained_at_ms is re-stamped to "now"
    assert refreshed.obtained_at_ms != cred.obtained_at_ms
