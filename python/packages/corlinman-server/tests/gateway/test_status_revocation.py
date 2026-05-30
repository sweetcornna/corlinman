"""Tests for status-token revocation (#34) — the per-session epoch store and
its fold into ``make_status_token`` / ``verify_status_token``.

Backward compatibility is the whole point: a token minted today (no epoch /
epoch 0) must keep verifying while the session's stored epoch is 0, and only
stop once the session is explicitly revoked.
"""

from __future__ import annotations

import json
from pathlib import Path

from corlinman_server.gateway.status_revocation import (
    current_epoch,
    revoke_session,
)
from corlinman_server.gateway.status_token import (
    make_status_token,
    verify_status_token,
)

_KEY = b"k" * 32
_SESSION = "tenant::sess-1"


# ---------------------------------------------------------------------------
# Epoch store round-trip
# ---------------------------------------------------------------------------


def test_current_epoch_defaults_to_zero(tmp_path: Path) -> None:
    # No file at all -> 0.
    assert current_epoch(tmp_path, _SESSION) == 0
    # data_dir None -> 0.
    assert current_epoch(None, _SESSION) == 0
    # Empty session_key -> 0.
    assert current_epoch(tmp_path, "") == 0


def test_revoke_increments_and_persists(tmp_path: Path) -> None:
    assert current_epoch(tmp_path, _SESSION) == 0
    assert revoke_session(tmp_path, _SESSION) == 1
    assert current_epoch(tmp_path, _SESSION) == 1
    # Persisted across "processes" — a fresh read sees it.
    assert current_epoch(tmp_path, _SESSION) == 1
    assert revoke_session(tmp_path, _SESSION) == 2
    assert current_epoch(tmp_path, _SESSION) == 2


def test_revoke_is_per_session(tmp_path: Path) -> None:
    revoke_session(tmp_path, _SESSION)
    revoke_session(tmp_path, _SESSION)
    other = "tenant::sess-2"
    # Bumping sess-1 twice leaves sess-2 untouched.
    assert current_epoch(tmp_path, _SESSION) == 2
    assert current_epoch(tmp_path, other) == 0
    assert revoke_session(tmp_path, other) == 1
    assert current_epoch(tmp_path, _SESSION) == 2
    assert current_epoch(tmp_path, other) == 1


def test_revoke_writes_atomically_valid_json(tmp_path: Path) -> None:
    revoke_session(tmp_path, _SESSION)
    path = tmp_path / "status_epochs.json"
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {_SESSION: 1}
    # No stray temp files left behind.
    leftover = [p.name for p in tmp_path.iterdir() if p.name.startswith(".status_epochs")]
    assert leftover == []


def test_revoke_none_data_dir_is_noop(tmp_path: Path) -> None:
    assert revoke_session(None, _SESSION) == 0
    assert revoke_session(tmp_path, "") == 0


def test_corrupt_epoch_file_reads_as_zero(tmp_path: Path) -> None:
    (tmp_path / "status_epochs.json").write_text("{ not json", encoding="utf-8")
    assert current_epoch(tmp_path, _SESSION) == 0
    # And a revoke recovers cleanly (starts from 0 -> 1).
    assert revoke_session(tmp_path, _SESSION) == 1
    assert current_epoch(tmp_path, _SESSION) == 1


# ---------------------------------------------------------------------------
# Token fold + revocation gate
# ---------------------------------------------------------------------------


def test_token_at_epoch_zero_verifies_at_epoch_zero() -> None:
    token = make_status_token(_SESSION, _KEY, now=1000, epoch=0)
    assert verify_status_token(token, _KEY, now=1000, current_epoch=0) == _SESSION


def test_token_minted_before_revoke_is_rejected_after() -> None:
    # Link handed out before the operator revoked.
    token = make_status_token(_SESSION, _KEY, now=1000, epoch=0)
    # After revoke, the session's current epoch is 1 — the epoch-0 token fails.
    assert verify_status_token(token, _KEY, now=1000, current_epoch=1) is None


def test_token_minted_at_current_epoch_still_verifies() -> None:
    # A link minted after the revoke carries the live epoch and keeps working.
    token = make_status_token(_SESSION, _KEY, now=1000, epoch=1)
    assert verify_status_token(token, _KEY, now=1000, current_epoch=1) == _SESSION
    # A token ahead of the current epoch (e.g. a stale current_epoch read)
    # is still accepted — only *behind* is rejected.
    assert verify_status_token(token, _KEY, now=1000, current_epoch=0) == _SESSION


def test_default_verify_current_epoch_is_zero() -> None:
    # Existing callers that pass no current_epoch get the backward-compatible
    # "nothing revoked" behaviour.
    token = make_status_token(_SESSION, _KEY, now=1000)
    assert verify_status_token(token, _KEY, now=1000) == _SESSION


def test_legacy_two_field_body_still_verifies() -> None:
    """A pre-#34 token carries only ``key|exp`` (no epoch). Hand-build one with
    the legacy body shape and confirm verify still accepts it at epoch 0 and
    rejects it once the session is revoked."""
    import base64
    import hashlib
    import hmac

    def _b64e(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    exp = 2_000
    legacy_body = f"{_b64e(_SESSION.encode('utf-8'))}|{exp}"  # 2 fields, no epoch
    sig = _b64e(
        hmac.new(_KEY, legacy_body.encode("utf-8"), hashlib.sha256).digest()
    )
    legacy_token = f"{legacy_body}.{sig}"

    # Legacy token verifies while nothing is revoked.
    assert verify_status_token(legacy_token, _KEY, now=1000) == _SESSION
    assert (
        verify_status_token(legacy_token, _KEY, now=1000, current_epoch=0)
        == _SESSION
    )
    # And is rejected once the session is revoked (epoch reads as 0 < 1).
    assert (
        verify_status_token(legacy_token, _KEY, now=1000, current_epoch=1) is None
    )


def test_revocation_end_to_end(tmp_path: Path) -> None:
    """Full loop through the real store: mint at the live epoch, verify, revoke,
    old link dies, a re-mint at the bumped epoch works."""
    epoch0 = current_epoch(tmp_path, _SESSION)
    assert epoch0 == 0
    link = make_status_token(_SESSION, _KEY, now=1000, epoch=epoch0)
    assert (
        verify_status_token(
            link, _KEY, now=1000, current_epoch=current_epoch(tmp_path, _SESSION)
        )
        == _SESSION
    )

    new_epoch = revoke_session(tmp_path, _SESSION)
    assert new_epoch == 1
    # Old link no longer verifies against the bumped epoch.
    assert (
        verify_status_token(
            link, _KEY, now=1000, current_epoch=current_epoch(tmp_path, _SESSION)
        )
        is None
    )
    # Re-minted link carries the new epoch and verifies again.
    relink = make_status_token(
        _SESSION, _KEY, now=1000, epoch=current_epoch(tmp_path, _SESSION)
    )
    assert (
        verify_status_token(
            relink, _KEY, now=1000, current_epoch=current_epoch(tmp_path, _SESSION)
        )
        == _SESSION
    )
