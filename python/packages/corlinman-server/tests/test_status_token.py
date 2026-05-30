"""Tests for the agent-status-card share token (``gateway/status_token.py``)."""

from __future__ import annotations

from pathlib import Path

from corlinman_server.gateway.status_token import (
    DEFAULT_TTL_SECONDS,
    make_status_token,
    resolve_signing_key,
    verify_status_token,
)

_KEY = b"k" * 32


def test_roundtrip_including_pipe_in_session_key() -> None:
    # session_key is b64-wrapped, so a literal '|' inside it is safe.
    token = make_status_token("sess:abc|weird/key", _KEY, now=1000)
    assert verify_status_token(token, _KEY, now=1000) == "sess:abc|weird/key"


def test_expiry_rejected() -> None:
    token = make_status_token("s", _KEY, ttl_seconds=100, now=1000)
    assert verify_status_token(token, _KEY, now=1099) == "s"
    assert verify_status_token(token, _KEY, now=1101) is None


def test_wrong_key_rejected() -> None:
    token = make_status_token("s", _KEY, now=1000)
    assert verify_status_token(token, b"x" * 32, now=1000) is None


def test_tamper_rejected() -> None:
    token = make_status_token("s", _KEY, now=1000)
    sig = token.partition(".")[2]
    # Swap the body (different session) but keep the old signature.
    forged = make_status_token("evil", _KEY, now=1000).partition(".")[0] + "." + sig
    assert verify_status_token(forged, _KEY, now=1000) is None


def test_garbage_returns_none() -> None:
    for bad in ("", "no-dot", "a.b.c.d", "....", "x" * 5):
        assert verify_status_token(bad, _KEY) is None


def test_default_ttl_applied_for_nonpositive() -> None:
    token = make_status_token("s", _KEY, ttl_seconds=0, now=0)
    assert verify_status_token(token, _KEY, now=DEFAULT_TTL_SECONDS - 1) == "s"
    assert verify_status_token(token, _KEY, now=DEFAULT_TTL_SECONDS + 1) is None


def test_resolve_signing_key_persists(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CORLINMAN_STATUS_SIGNING_KEY", raising=False)
    k1 = resolve_signing_key(tmp_path)
    k2 = resolve_signing_key(tmp_path)
    assert k1 == k2  # stable across calls (survives restart)
    assert (tmp_path / "status_signing.key").is_file()
    # A token minted with the persisted key verifies on the next resolve.
    token = make_status_token("s", k1, now=1000)
    assert verify_status_token(token, resolve_signing_key(tmp_path), now=1000) == "s"


def test_resolve_signing_key_env_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CORLINMAN_STATUS_SIGNING_KEY", "operator-secret")
    # Env wins and is deterministic regardless of data dir.
    assert resolve_signing_key(tmp_path) == resolve_signing_key(None)
    # No key file written when the env override is used.
    assert not (tmp_path / "status_signing.key").exists()
