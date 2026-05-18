"""On-disk persistence for OAuth credentials.

Storage layout mirrors hermes's ``~/.hermes/.anthropic_oauth.json`` but
keyed per-provider so the same code can hold Codex / Gemini / xAI tokens
later (those land in W-A3). The file is written with mode ``0o600`` so a
shared-home operator deployment doesn't accidentally expose tokens to
other UIDs on the host.

Data dir is *always* passed in by the caller — there is no implicit
fallback to ``~/.corlinman``. The gateway bootstrapper holds the
authoritative ``data_dir`` on :class:`AdminState`; the router threads it
through to these helpers. Tests pass ``tmp_path``.

The :class:`OAuthCredential` dataclass is frozen and its ``__repr__``
redacts both ``access_token`` and ``refresh_token`` (first 6 chars +
ellipsis) so a stray ``logger.info(cred)`` can't leak the secret.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

# Defensive slug regex — only [a-z0-9_-], length 1-32. We never accept a
# provider id from an untrusted HTTP body without first validating against
# this set, so the JSON filename can never be path-traversed.
_PROVIDER_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def _validate_provider(provider: str) -> str:
    if not isinstance(provider, str) or not _PROVIDER_SLUG_RE.match(provider):
        raise ValueError(f"invalid provider slug: {provider!r}")
    return provider


def _redact(secret: str | None) -> str:
    """Return ``<first6>…`` for a secret, ``<unset>`` when None / empty.

    Keeping the first 6 chars is enough to distinguish two tokens (the
    leading characters are issuer-deterministic) without exposing the
    bearer value. We intentionally omit any trailing characters because
    most OAuth tokens are signed JWTs whose final segments carry
    decodable claims.
    """
    if not secret:
        return "<unset>"
    if len(secret) <= 6:
        return "<redacted>"
    return f"{secret[:6]}…"


@dataclass(frozen=True)
class OAuthCredential:
    """One provider's OAuth credential bundle.

    Field names follow the hermes wire shape (``access_token`` /
    ``refresh_token`` / ``expires_at_ms``) so the JSON file is
    cross-readable with a hermes install at the same path.

    The ``scope`` field is optional — Anthropic returns ``"user:inference
    user:profile org:create_api_key"`` on a fresh login; Claude Code's
    ``~/.claude/.credentials.json`` stores ``scopes`` as a list which we
    join with a space to fit this single string slot.

    ``obtained_at_ms`` is the wall-clock time we received the token,
    handy for diagnostics ("token is 3h old, refresh now or later?").
    """

    provider: str
    access_token: str
    refresh_token: str | None
    expires_at_ms: int | None
    scope: str | None
    obtained_at_ms: int

    def __repr__(self) -> str:  # pragma: no cover — exercised by test
        # Redact both tokens. Everything else is safe to surface.
        return (
            "OAuthCredential("
            f"provider={self.provider!r}, "
            f"access_token={_redact(self.access_token)!r}, "
            f"refresh_token={_redact(self.refresh_token)!r}, "
            f"expires_at_ms={self.expires_at_ms!r}, "
            f"scope={self.scope!r}, "
            f"obtained_at_ms={self.obtained_at_ms!r}"
            ")"
        )

    # Convenience constructors / helpers --------------------------------

    @classmethod
    def new(
        cls,
        *,
        provider: str,
        access_token: str,
        refresh_token: str | None,
        expires_at_ms: int | None,
        scope: str | None = None,
        obtained_at_ms: int | None = None,
    ) -> OAuthCredential:
        """Build a credential, stamping ``obtained_at_ms`` to now."""
        _validate_provider(provider)
        return cls(
            provider=provider,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at_ms=expires_at_ms,
            scope=scope,
            obtained_at_ms=(
                obtained_at_ms if obtained_at_ms is not None else int(time.time() * 1000)
            ),
        )

    def is_expired(self, *, skew_seconds: int = 0) -> bool:
        """Return True when the access token is past ``expires_at_ms``.

        ``skew_seconds`` is subtracted from the expiry, letting callers
        treat a token as "about to expire" so they can pro-actively
        refresh before a request lands.
        """
        if self.expires_at_ms is None:
            return False
        now_ms = int(time.time() * 1000)
        return now_ms >= (self.expires_at_ms - skew_seconds * 1000)

    def expires_in_seconds(self) -> int | None:
        if self.expires_at_ms is None:
            return None
        return max(0, (self.expires_at_ms - int(time.time() * 1000)) // 1000)

    def with_refreshed(
        self,
        *,
        access_token: str,
        refresh_token: str | None,
        expires_at_ms: int | None,
    ) -> OAuthCredential:
        """Return a new credential carrying the refreshed token fields."""
        return replace(
            self,
            access_token=access_token,
            refresh_token=refresh_token if refresh_token else self.refresh_token,
            expires_at_ms=expires_at_ms,
            obtained_at_ms=int(time.time() * 1000),
        )


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _oauth_dir(data_dir: Path) -> Path:
    return Path(data_dir) / ".oauth"


def _credential_path(data_dir: Path, provider: str) -> Path:
    provider = _validate_provider(provider)
    return _oauth_dir(data_dir) / f"{provider}.json"


def load_credential(data_dir: Path, provider: str) -> OAuthCredential | None:
    """Read a stored credential or return ``None`` when absent / malformed.

    Malformed files are treated as "no credential" rather than raised so
    a corrupted JSON on disk doesn't brick the credential-resolution
    chain — the operator can simply re-run OAuth login to overwrite it.
    """
    path = _credential_path(data_dir, provider)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    access_token = raw.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return None
    obtained_at = raw.get("obtained_at_ms")
    if not isinstance(obtained_at, int):
        obtained_at = int(time.time() * 1000)
    return OAuthCredential(
        provider=str(raw.get("provider") or provider),
        access_token=access_token,
        refresh_token=raw.get("refresh_token") or None,
        expires_at_ms=raw.get("expires_at_ms") if isinstance(raw.get("expires_at_ms"), int) else None,
        scope=raw.get("scope") or None,
        obtained_at_ms=obtained_at,
    )


def save_credential(data_dir: Path, credential: OAuthCredential) -> Path:
    """Persist ``credential`` to disk with ``0o600`` mode.

    Uses a same-directory ``.tmp`` file + ``replace`` so a crash mid-write
    can't leave the JSON file half-overwritten. The directory itself is
    created with ``0o700`` for the same reason the file is ``0o600``.
    """
    path = _credential_path(data_dir, credential.provider)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    # Best-effort tighten on the directory — ignore if the filesystem
    # doesn't honour chmod (Windows, some FUSE mounts).
    try:
        os.chmod(parent, stat.S_IRWXU)  # 0o700
    except OSError:  # pragma: no cover — platform-specific
        pass

    payload: dict[str, Any] = asdict(credential)
    tmp = path.with_suffix(".tmp")
    # Write through a file descriptor so we can set the mode atomically.
    fd = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        stat.S_IRUSR | stat.S_IWUSR,  # 0o600
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover
        pass
    return path


def delete_credential(data_dir: Path, provider: str) -> bool:
    """Remove the credential file. Returns True iff a file was removed.

    Missing-file is not an error — the endpoint contract is "make sure
    this provider is disconnected" which is satisfied by both "file
    didn't exist" and "file removed".
    """
    path = _credential_path(data_dir, provider)
    if not path.exists():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


__all__ = [
    "OAuthCredential",
    "delete_credential",
    "load_credential",
    "save_credential",
]
