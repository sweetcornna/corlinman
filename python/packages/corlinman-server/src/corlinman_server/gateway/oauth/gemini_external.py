"""Read-only adapter for the **Gemini CLI** OAuth credentials file.

The official Gemini CLI (``gemini`` binary, Google-owned) writes its
Google OAuth tokens to ``~/.gemini/oauth_creds.json``. Operators who
have already signed in with ``gemini auth`` already have a valid token
bundle on disk; we surface its presence + expiry to the dashboard so the
operator can see "Gemini is connected" without re-authenticating.

Hard rules:

* **Never write** to ``~/.gemini/oauth_creds.json``. The Gemini CLI
  owns that file; the corresponding HTTP endpoint is read-only (``GET``).
* **Never raise** when the file is missing or malformed — both flatten
  to ``detected: false``. Malformed JSON emits a warning log (no token
  bytes) so the operator can investigate.
* **Never log** ``access_token`` or ``refresh_token`` from the file.

Path resolution: we honour ``GEMINI_HOME`` (mirrors hermes's pattern
for ``CODEX_HOME``), else fall back to ``~/.gemini``. Hermes itself
keeps its own Hermes-managed copy at
``~/.hermes/auth/google_oauth.json``
(``hermes_cli/auth.py:1756`` notes: "Tokens live in
``~/.hermes/auth/google_oauth.json`` (managed by ``agent.google_oauth``)")
— we read the **official Gemini CLI** path because corlinman doesn't
manage Gemini logins itself this round.

Expected file shape (Gemini CLI / qwen-CLI-style):

    {
      "access_token": "ya29.…",
      "refresh_token": "1//…",
      "scope": "https://www.googleapis.com/auth/cloud-platform openid …",
      "token_type": "Bearer",
      "id_token": "eyJ…",
      "expiry_date": 1700000000000   // ms since epoch
    }

We accept both ``expiry_date`` (Google CLI canonical, milliseconds) and
``expires_at_ms`` (Hermes canonical) for forward-compatibility.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _gemini_auth_path() -> Path:
    """Resolve the Gemini CLI credentials file path.

    Honour ``GEMINI_HOME`` env var if set + non-empty (mirrors the
    ``CODEX_HOME`` pattern), else ``~/.gemini``. File name is
    ``oauth_creds.json`` to match the canonical Google/Qwen CLI
    convention (see hermes ``_qwen_cli_auth_path`` at
    ``hermes_cli/auth.py:1561`` which uses the same ``oauth_creds.json``
    filename for the sibling Qwen CLI).
    """
    gemini_home = os.environ.get("GEMINI_HOME", "").strip()
    if not gemini_home:
        gemini_home = str(Path.home() / ".gemini")
    return Path(gemini_home).expanduser() / "oauth_creds.json"


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GeminiStatus:
    """Snapshot of the Gemini CLI auth file's presence + expiry hint.

    ``detected`` — True iff a parseable file exists with a non-empty
    ``access_token``. ``account_id`` — best-effort ``email`` from the
    ``id_token`` JWT payload (no signature verification). ``expires_at_ms``
    — from the ``expiry_date`` (ms) or ``expires_at_ms`` field.
    """

    detected: bool
    account_id: str | None = None
    expires_at_ms: int | None = None


# ---------------------------------------------------------------------------
# JWT payload peek (no signature verification — read-only hint)
# ---------------------------------------------------------------------------


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    payload_b64 = token.split(".", 2)[1]
    pad = "=" * ((4 - len(payload_b64) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_b64 + pad)
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def read_gemini_status(path: Path | None = None) -> GeminiStatus | None:
    """Read ``~/.gemini/oauth_creds.json`` and return its detection status.

    Returns ``None`` only when the file is absent. Malformed JSON or
    shape issues warn (no token bytes) and return
    ``GeminiStatus(detected=False)``.
    """
    target = Path(path) if path is not None else _gemini_auth_path()
    if not target.is_file():
        return None

    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("gemini_auth.read_failed", path=str(target), error=str(exc))
        return GeminiStatus(detected=False)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("gemini_auth.malformed_json", path=str(target), error=str(exc))
        return GeminiStatus(detected=False)

    if not isinstance(data, dict):
        logger.warning("gemini_auth.malformed_shape", path=str(target), reason="root_not_object")
        return GeminiStatus(detected=False)

    access_token = data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return GeminiStatus(detected=False)

    # Expiry hint — try ``expiry_date`` first (Google CLI canonical, ms),
    # then ``expires_at_ms`` (Hermes canonical). Both are milliseconds.
    expires_at_ms: int | None = None
    raw_exp = data.get("expiry_date")
    if isinstance(raw_exp, (int, float)) and raw_exp > 0:
        expires_at_ms = int(raw_exp)
    else:
        raw_exp = data.get("expires_at_ms")
        if isinstance(raw_exp, (int, float)) and raw_exp > 0:
            expires_at_ms = int(raw_exp)

    # Account hint — email from id_token if present.
    account_id: str | None = None
    id_token = data.get("id_token")
    if isinstance(id_token, str) and id_token:
        id_payload = _decode_jwt_payload(id_token)
        if id_payload is not None:
            candidate = id_payload.get("email") or id_payload.get("sub")
            if isinstance(candidate, str) and candidate:
                account_id = candidate

    return GeminiStatus(
        detected=True,
        account_id=account_id,
        expires_at_ms=expires_at_ms,
    )


__all__ = [
    "GeminiStatus",
    "read_gemini_status",
]
