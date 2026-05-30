"""Signed, stateless share tokens for the public **agent status card**.

A status token is a read-only, time-limited *capability* scoping access to
exactly ONE conversation's status + work trajectory. The agent hands it to a
chat user as a clickable channel link (``<public_url>/status/<token>``) so
the user can watch the agent's current step + timeline **without an admin
login**.

Wire format (no DB — the token IS the capability)::

    <b64url(session_key|exp_unix)>.<b64url(hmac_sha256(body, key))>

* ``session_key`` — the conversation the token authorizes (and only that one).
* ``exp_unix`` — hard expiry; :func:`verify_status_token` rejects stale tokens.
* HMAC-SHA256 over the body with a per-deployment signing key, compared in
  constant time. Tampering with the session_key / expiry invalidates the sig.

The signing key is resolved once per process via :func:`resolve_signing_key`
(env override → a persisted ``<DATA_DIR>/status_signing.key`` generated on
first use) so links survive gateway restarts without any config.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

__all__ = [
    "DEFAULT_TTL_SECONDS",
    "StatusTokenError",
    "make_status_token",
    "resolve_signing_key",
    "verify_status_token",
]

#: Default link lifetime — 24h. Long enough that a user can revisit the card
#: for the rest of the day, short enough that a leaked link self-expires.
DEFAULT_TTL_SECONDS: int = 24 * 60 * 60

#: Env override for the signing key (raw string; any length, hashed into 32B).
_SIGNING_KEY_ENV: str = "CORLINMAN_STATUS_SIGNING_KEY"

#: Filename of the persisted per-deployment key under the data dir.
_KEY_FILENAME: str = "status_signing.key"


class StatusTokenError(Exception):
    """Malformed token / signing failure. ``verify_status_token`` never
    raises (it returns ``None``); this is for the ``make`` path + key
    resolution surprises."""


def _b64e(raw: bytes) -> str:
    """URL-safe base64 without padding (compact, path-safe)."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    """Inverse of :func:`_b64e`; re-pads before decoding."""
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(body: str, signing_key: bytes) -> str:
    digest = hmac.new(signing_key, body.encode("utf-8"), hashlib.sha256).digest()
    return _b64e(digest)


def make_status_token(
    session_key: str,
    signing_key: bytes,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: int | None = None,
) -> str:
    """Mint a signed status token for ``session_key``.

    ``ttl_seconds`` <= 0 is treated as the default (never mint an
    already-expired token by accident). ``now`` is injectable for tests.
    """
    if not session_key:
        raise StatusTokenError("session_key must be non-empty")
    ttl = ttl_seconds if ttl_seconds > 0 else DEFAULT_TTL_SECONDS
    exp = (int(now) if now is not None else int(time.time())) + ttl
    # ``|`` separates the two fields; session_key is b64-wrapped as a whole so
    # a literal ``|`` inside a key can't confuse the split.
    body = f"{_b64e(session_key.encode('utf-8'))}|{exp}"
    return f"{body}.{_sign(body, signing_key)}"


def verify_status_token(
    token: str,
    signing_key: bytes,
    *,
    now: int | None = None,
) -> str | None:
    """Return the ``session_key`` a valid, unexpired token authorizes, else
    ``None``. Never raises — any malformed / tampered / expired token is a
    clean ``None`` so route handlers can 403 uniformly."""
    if not token or not isinstance(token, str):
        return None
    body, _, sig = token.partition(".")
    if not sig:
        return None
    expected = _sign(body, signing_key)
    # Constant-time compare to avoid leaking the signature byte-by-byte.
    if not hmac.compare_digest(sig, expected):
        return None
    key_b64, _, exp_str = body.partition("|")
    if not exp_str or not exp_str.isdigit():
        return None
    if int(exp_str) < (int(now) if now is not None else int(time.time())):
        return None  # expired
    try:
        return _b64d(key_b64).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def resolve_signing_key(data_dir: Path | None) -> bytes:
    """Resolve the per-deployment signing key (stable across restarts).

    Precedence: ``$CORLINMAN_STATUS_SIGNING_KEY`` (hashed to 32 bytes) → a
    persisted ``<data_dir>/status_signing.key`` (generated + chmod 600 on
    first use) → an in-memory random key when no data dir is available
    (tokens won't survive a restart, but the feature still works for the
    process lifetime — fine for tests / degraded boots).
    """
    env = os.environ.get(_SIGNING_KEY_ENV)
    if env and env.strip():
        return hashlib.sha256(env.strip().encode("utf-8")).digest()
    if data_dir is not None:
        path = Path(data_dir) / _KEY_FILENAME
        try:
            if path.is_file():
                raw = path.read_bytes()
                if len(raw) >= 16:
                    return raw
            generated = secrets.token_bytes(32)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write then tighten perms; best-effort on platforms without chmod.
            path.write_bytes(generated)
            try:
                path.chmod(0o600)
            except OSError:
                pass
            return generated
        except OSError:
            # Unwritable data dir — fall through to the ephemeral key.
            pass
    return secrets.token_bytes(32)
