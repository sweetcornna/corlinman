"""Signed, stateless share tokens for the public **agent status card**.

A status token is a read-only, time-limited *capability* scoping access to
exactly ONE conversation's status + work trajectory. The agent hands it to a
chat user as a clickable channel link (``<public_url>/status/<token>``) so
the user can watch the agent's current step + timeline **without an admin
login**.

Wire format (no DB — the token IS the capability)::

    <b64url(session_key)>|<exp_unix>|<epoch>|<b64url(persona_id)>.<b64url(hmac_sha256(body, key))>

* ``session_key`` — the conversation the token authorizes (and only that one).
* ``exp_unix`` — hard expiry; :func:`verify_status_token` rejects stale tokens.
* ``epoch`` — the per-session **revocation epoch** (#34) baked in at mint time.
  A token whose epoch is behind the session's current epoch is rejected, so an
  operator can invalidate every outstanding link for a session by bumping its
  epoch (see :mod:`corlinman_server.gateway.status_revocation`).
* ``persona_id`` (F2) — the optional bound persona whose avatar the public
  status card renders. b64url-wrapped so a slug with reserved chars is safe;
  an empty field means "no persona bound" (the common case for a plain agent
  session). It rides inside the signed body so a recipient can't swap in
  another persona's avatar by editing the link.
* HMAC-SHA256 over the body with a per-deployment signing key, compared in
  constant time. Tampering with any field (session / expiry / epoch / persona)
  invalidates the sig.

Backward compatibility: tokens minted before #34 carry only the 2-field body
``<b64url(session_key)>|<exp_unix>`` (no epoch); tokens minted before F2 carry
the 3-field body ``…|<epoch>`` (no persona). :func:`verify_status_token` accepts
all three shapes — a missing epoch reads as ``0``, a missing persona as ``""`` —
so legacy links keep verifying until their session is explicitly revoked
(current epoch >= 1). :func:`verify_status_token_full` returns the bound persona
alongside the session for callers (the public status card) that need it.

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
    "verify_status_token_full",
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
    epoch: int = 0,
    persona_id: str | None = None,
) -> str:
    """Mint a signed status token for ``session_key``.

    ``ttl_seconds`` <= 0 is treated as the default (never mint an
    already-expired token by accident). ``now`` is injectable for tests.

    ``epoch`` is the session's current revocation epoch (#34); it is folded
    into the signed body so a later ``revoke_session`` (which bumps the stored
    epoch) leaves this token's epoch behind and :func:`verify_status_token`
    rejects it. Defaults to ``0`` — the same value a legacy (pre-#34) token
    carries — so callers that don't pass an epoch keep their old behaviour.

    ``persona_id`` (F2) is the optional bound persona whose avatar the public
    status card renders. It is folded into the signed body (b64url-wrapped) so
    it can't be swapped on a shared link. Defaults to ``None`` → an empty
    persona field, which is byte-identical to the pre-F2 3-field body so
    callers that don't pass a persona keep minting exactly the old token shape.
    """
    if not session_key:
        raise StatusTokenError("session_key must be non-empty")
    ttl = ttl_seconds if ttl_seconds > 0 else DEFAULT_TTL_SECONDS
    exp = (int(now) if now is not None else int(time.time())) + ttl
    epoch_val = int(epoch) if epoch and int(epoch) > 0 else 0
    # ``|`` separates the fields; session_key + persona_id are each b64-wrapped
    # so a literal ``|`` inside either can't confuse the split.
    body = f"{_b64e(session_key.encode('utf-8'))}|{exp}|{epoch_val}"
    if persona_id:
        # Only append the 4th field when a persona is actually bound — an
        # empty persona keeps the body byte-identical to the pre-F2 shape so
        # existing tokens / callers are wholly unaffected.
        body = f"{body}|{_b64e(persona_id.encode('utf-8'))}"
    return f"{body}.{_sign(body, signing_key)}"


def verify_status_token(
    token: str,
    signing_key: bytes,
    *,
    now: int | None = None,
    current_epoch: int = 0,
) -> str | None:
    """Return the ``session_key`` a valid, unexpired token authorizes, else
    ``None``. Never raises — any malformed / tampered / expired / revoked
    token is a clean ``None`` so route handlers can 403 uniformly.

    ``current_epoch`` is the session's live revocation epoch (#34). A token
    whose baked-in epoch is *behind* ``current_epoch`` is rejected — that is
    how :func:`corlinman_server.gateway.status_revocation.revoke_session`
    invalidates outstanding links. The 4-field body ``key|exp|epoch|persona``
    (F2), the 3-field body ``key|exp|epoch`` (#34), and the legacy 2-field
    body ``key|exp`` are all accepted; a legacy token's epoch reads as ``0``,
    so it is rejected only once the session has been revoked
    (``current_epoch >= 1``). The persona field is ignored here — callers that
    need it use :func:`verify_status_token_full`."""
    result = verify_status_token_full(
        token, signing_key, now=now, current_epoch=current_epoch
    )
    return result[0] if result is not None else None


def verify_status_token_full(
    token: str,
    signing_key: bytes,
    *,
    now: int | None = None,
    current_epoch: int = 0,
) -> tuple[str, str | None] | None:
    """Like :func:`verify_status_token` but also surface the bound persona.

    Returns ``(session_key, persona_id)`` for a valid token — ``persona_id``
    is ``None`` when the token carries no persona field (every pre-F2 token,
    plus F2 tokens minted for a plain agent session). Returns ``None`` for any
    malformed / tampered / expired / revoked token, exactly like the
    session-only variant. Never raises.
    """
    if not token or not isinstance(token, str):
        return None
    body, _, sig = token.partition(".")
    if not sig:
        return None
    expected = _sign(body, signing_key)
    # Constant-time compare to avoid leaking the signature byte-by-byte.
    if not hmac.compare_digest(sig, expected):
        return None
    # Body is ``key|exp`` (legacy), ``key|exp|epoch`` (#34), or
    # ``key|exp|epoch|persona`` (F2). Split on ``|``; the session_key and
    # persona_id are each b64-wrapped so neither contains a literal ``|``.
    parts = body.split("|")
    if len(parts) not in (2, 3, 4):
        return None
    key_b64, exp_str = parts[0], parts[1]
    epoch_str = parts[2] if len(parts) >= 3 else "0"
    persona_b64 = parts[3] if len(parts) == 4 else ""
    if not exp_str or not exp_str.isdigit():
        return None
    if not epoch_str or not epoch_str.isdigit():
        return None
    if int(exp_str) < (int(now) if now is not None else int(time.time())):
        return None  # expired
    if int(epoch_str) < int(current_epoch):
        return None  # revoked — token predates the session's current epoch
    try:
        session_key = _b64d(key_b64).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    persona_id: str | None = None
    if persona_b64:
        try:
            persona_id = _b64d(persona_b64).decode("utf-8") or None
        except (ValueError, UnicodeDecodeError):
            # A corrupt persona field shouldn't sink an otherwise-valid token —
            # the session capability is the security-critical part. Treat an
            # undecodable persona as "no persona bound".
            persona_id = None
    return (session_key, persona_id)


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
