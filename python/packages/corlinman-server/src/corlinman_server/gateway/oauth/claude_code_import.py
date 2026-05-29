"""Read-only adapter for the Claude Code CLI credentials file.

When an operator already has Claude Code installed and signed in, their
``~/.claude/.credentials.json`` already holds a refreshable OAuth bundle
under the ``claudeAiOauth`` key. This module reads that file and emits
an :class:`OAuthCredential` view so the corlinman gateway can consume
the same subscription quota without minting a fresh PKCE login.

Hard rules:

* **Never write** to ``~/.claude/.credentials.json``. The Claude Code
  CLI owns that file; mutating it would silently change the operator's
  Claude Code state and could break the CLI's own refresh logic.
* **Never raise** when the file is missing — that's the common case
  for first-run operators and the endpoint surfaces it as 404, not 500.
* **Do raise** on malformed JSON — a corrupt file usually means the
  operator's keychain is in a weird state and they should investigate
  rather than silently get a no-op import.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from corlinman_server.gateway.oauth.storage import OAuthCredential

CLAUDE_CODE_CREDENTIALS_DEFAULT_PATH: Path = Path.home() / ".claude" / ".credentials.json"


class ClaudeCodeCredentialsMalformed(Exception):
    """``~/.claude/.credentials.json`` exists but is not parseable."""


def read_claude_code_credentials(
    path: Path | None = None,
) -> OAuthCredential | None:
    """Read the Claude Code CLI credentials file.

    Returns ``None`` when the file does not exist or its ``claudeAiOauth``
    block is missing / lacks an ``accessToken``. Raises
    :class:`ClaudeCodeCredentialsMalformed` when the file exists but isn't
    valid JSON or doesn't have an object root — symptomatic of disk
    corruption / partial writes.

    The returned ``OAuthCredential`` carries ``provider="anthropic"``
    because Claude Code's tokens speak the Anthropic OAuth shape and the
    corlinman gateway routes them to the Anthropic provider.
    """
    target = Path(path) if path is not None else CLAUDE_CODE_CREDENTIALS_DEFAULT_PATH
    if not target.exists():
        return None

    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ClaudeCodeCredentialsMalformed(f"cannot read {target}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ClaudeCodeCredentialsMalformed(
            f"{target} is not valid JSON: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise ClaudeCodeCredentialsMalformed(
            f"{target} root is not a JSON object"
        )

    oauth_block = data.get("claudeAiOauth")
    if not isinstance(oauth_block, dict):
        return None

    access_token = oauth_block.get("accessToken")
    if not isinstance(access_token, str) or not access_token:
        return None

    refresh_token_raw = oauth_block.get("refreshToken")
    refresh_token = refresh_token_raw if isinstance(refresh_token_raw, str) and refresh_token_raw else None

    expires_at_raw = oauth_block.get("expiresAt")
    expires_at_ms = expires_at_raw if isinstance(expires_at_raw, int) and expires_at_raw > 0 else None

    scopes_raw = oauth_block.get("scopes")
    if isinstance(scopes_raw, list):
        scope = " ".join(str(s) for s in scopes_raw if isinstance(s, str))
    elif isinstance(scopes_raw, str):
        scope = scopes_raw
    else:
        scope = None

    return OAuthCredential(
        provider="anthropic",
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at_ms=expires_at_ms,
        scope=scope or None,
        obtained_at_ms=int(time.time() * 1000),
    )


__all__ = [
    "CLAUDE_CODE_CREDENTIALS_DEFAULT_PATH",
    "ClaudeCodeCredentialsMalformed",
    "read_claude_code_credentials",
]
