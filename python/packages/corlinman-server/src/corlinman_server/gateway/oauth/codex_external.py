"""Read-only adapter for the OpenAI **Codex CLI** auth file.

The official Codex CLI (``codex`` binary, OpenAI-owned) writes its
ChatGPT-subscription tokens to ``~/.codex/auth.json``. Operators who
have already signed in with ``codex login`` already have a valid token
bundle on disk; we surface its presence + expiry to the dashboard so the
operator can see "Codex is connected" without re-authenticating.

Hard rules:

* **Never write** to ``~/.codex/auth.json``. The Codex CLI owns that
  file; mutating it would silently change the operator's Codex CLI
  state. The corresponding HTTP endpoint is read-only (``GET``).
* **Never raise** when the file is missing or malformed — the dashboard
  surfaces both as ``detected: false``. Malformed JSON emits a warning
  log (no token bytes) so the operator can investigate, but does not
  bubble a 500.
* **Never log** ``access_token`` or ``refresh_token`` from the file.

Path resolution mirrors hermes
(``hermes_cli/auth.py::_import_codex_cli_tokens`` lines 2827-2858):

    codex_home = os.environ["CODEX_HOME"].strip() or "~/.codex"
    auth_path  = codex_home / "auth.json"

The file shape is the OpenAI canonical
``{"tokens": {"access_token": ..., "refresh_token": ..., ...},
"OPENAI_API_KEY": ..., "last_refresh": "..."}``. We only need the
``access_token`` for detection and the (optional) ``access_token`` JWT
``exp`` claim for the expiry hint — we do **not** verify the JWT
signature; the value is informational and the upstream Codex CLI is
authoritative.
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
# Path resolution — verbatim from hermes_cli/auth.py:2827-2836
# ---------------------------------------------------------------------------


def _codex_auth_path() -> Path:
    """Resolve the Codex CLI auth file path.

    Mirrors hermes ``_import_codex_cli_tokens``: honour the
    ``CODEX_HOME`` env var if set (and non-empty after strip), else fall
    back to ``~/.codex``.
    """
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if not codex_home:
        codex_home = str(Path.home() / ".codex")
    return Path(codex_home).expanduser() / "auth.json"


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CodexStatus:
    """Snapshot of the Codex CLI auth file's presence + expiry hint.

    ``detected`` is True iff a parseable file exists and carries a
    non-empty ``access_token`` under the canonical ``tokens`` block.
    ``account_id`` is best-effort — the official Codex CLI doesn't
    consistently expose one; we use the ``sub`` claim from the
    ``id_token`` JWT when present, ``None`` otherwise. ``expires_at_ms``
    likewise comes from the ``access_token`` JWT's ``exp`` claim (decoded
    without signature verification — Codex CLI controls the file, the
    field is informational only).
    """

    detected: bool
    account_id: str | None = None
    expires_at_ms: int | None = None


# ---------------------------------------------------------------------------
# Internal: JWT payload peek (no signature verification — read-only hint)
# ---------------------------------------------------------------------------


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    """Best-effort decode of the payload segment of a JWT.

    Returns ``None`` for anything that doesn't look like a 3-segment JWT
    with a base64url middle segment. We do **not** validate the
    signature — the value is purely a display hint and the Codex CLI is
    the source of truth for token validity.
    """
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    payload_b64 = token.split(".", 2)[1]
    # base64url decode with padding restored
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


def read_codex_status(path: Path | None = None) -> CodexStatus | None:
    """Read ``~/.codex/auth.json`` and return its detection status.

    Returns ``None`` only when the file does not exist (the
    file-not-found case is distinguished from "found but unusable" so
    callers can decide whether to surface a "not installed" hint vs a
    "broken state, please re-login" hint — the HTTP endpoint flattens
    both to ``detected: false``).

    Malformed JSON or unexpected shapes log a single ``warning`` (with
    the path, **never** token bytes) and return a ``CodexStatus`` with
    ``detected=False``.
    """
    target = Path(path) if path is not None else _codex_auth_path()
    if not target.is_file():
        return None

    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        # Permission denied / IO error → treat as "not detected" and warn.
        logger.warning("codex_auth.read_failed", path=str(target), error=str(exc))
        return CodexStatus(detected=False)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("codex_auth.malformed_json", path=str(target), error=str(exc))
        return CodexStatus(detected=False)

    if not isinstance(data, dict):
        logger.warning("codex_auth.malformed_shape", path=str(target), reason="root_not_object")
        return CodexStatus(detected=False)

    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        # File exists but has no tokens block — the operator may have
        # half-completed `codex login` and bailed. Surface as
        # not-detected; no log noise (this is a known "in progress"
        # state).
        return CodexStatus(detected=False)

    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return CodexStatus(detected=False)

    # Best-effort expiry hint — decode the access_token JWT payload's
    # ``exp`` claim if present.
    expires_at_ms: int | None = None
    payload = _decode_jwt_payload(access_token)
    if payload is not None:
        exp = payload.get("exp")
        if isinstance(exp, (int, float)) and exp > 0:
            expires_at_ms = int(exp * 1000)

    # Best-effort account hint — id_token's ``sub`` claim.
    account_id: str | None = None
    id_token = tokens.get("id_token")
    if isinstance(id_token, str) and id_token:
        id_payload = _decode_jwt_payload(id_token)
        if id_payload is not None:
            # Prefer ``email`` over ``sub`` — display-friendly and matches
            # the Gemini external CLI helper for consistency.
            candidate = id_payload.get("email") or id_payload.get("sub")
            if isinstance(candidate, str) and candidate:
                account_id = candidate

    return CodexStatus(
        detected=True,
        account_id=account_id,
        expires_at_ms=expires_at_ms,
    )


__all__ = [
    "CodexStatus",
    "read_codex_status",
]
