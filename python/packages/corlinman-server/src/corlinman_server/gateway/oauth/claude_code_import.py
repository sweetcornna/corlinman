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
import subprocess
import sys
import time
from pathlib import Path

from corlinman_server.gateway.oauth.storage import OAuthCredential

CLAUDE_CODE_CREDENTIALS_DEFAULT_PATH: Path = Path.home() / ".claude" / ".credentials.json"

# Claude Code >= 2.1.114 stores its OAuth bundle in the macOS login Keychain
# under this generic-password service name instead of the on-disk JSON file.
# We read it with the system ``security`` CLI (no third-party dependency).
_KEYCHAIN_SERVICE_NAME = "Claude Code-credentials"


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
    explicit_path = path is not None
    target = path if path is not None else CLAUDE_CODE_CREDENTIALS_DEFAULT_PATH
    if not target.exists():
        # File absent. Newer Claude Code (>= 2.1.114) keeps the bundle in the
        # macOS login Keychain instead of the JSON file — try that fallback
        # before reporting "not found". Non-darwin platforms skip straight to
        # ``None`` (the historic behaviour) since there is no Keychain. An
        # EXPLICIT ``path=`` means "read exactly this file" (tests / callers
        # pointing at a specific bundle), so we never reach for the Keychain
        # in that case — only the default-path lookup falls back.
        if explicit_path:
            return None
        raw = _read_keychain_credentials()
        if raw is None:
            return None
        source = f"{_KEYCHAIN_SERVICE_NAME} (macOS Keychain)"
    else:
        try:
            raw = target.read_text(encoding="utf-8")
        except OSError as exc:
            raise ClaudeCodeCredentialsMalformed(f"cannot read {target}: {exc}") from exc
        source = str(target)

    return _parse_claude_code_credentials(raw, source=source)


def _parse_claude_code_credentials(raw: str, *, source: str) -> OAuthCredential | None:
    """Parse a Claude Code credentials JSON blob into an ``OAuthCredential``.

    Shared by the on-disk file path and the macOS Keychain path — both store
    the identical ``{"claudeAiOauth": {...}}`` shape. Raises
    :class:`ClaudeCodeCredentialsMalformed` on non-JSON / non-object roots
    (``source`` names the origin in the error for operator triage). Returns
    ``None`` when the ``claudeAiOauth`` block is missing / lacks an
    ``accessToken``.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ClaudeCodeCredentialsMalformed(
            f"{source} is not valid JSON: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise ClaudeCodeCredentialsMalformed(f"{source} root is not a JSON object")

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


def _read_keychain_credentials() -> str | None:
    """Read the Claude Code credentials blob from the macOS login Keychain.

    Shells out to ``security find-generic-password -s "Claude Code-credentials"
    -w`` (the ``-w`` flag prints only the stored password — the credentials
    JSON — to stdout). Returns the raw JSON string, or ``None`` when:

    * the platform is not macOS (no Keychain),
    * the ``security`` binary is missing,
    * the item is absent (``security`` exits non-zero), or
    * the call times out.

    The secret is NEVER logged. We never raise — a Keychain miss is the
    common first-run case and must surface as "not found" (404), not 500.
    """
    if sys.platform != "darwin":
        return None
    try:
        proc = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                _KEYCHAIN_SERVICE_NAME,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # ``security`` missing / unexecutable / timed out — treat as absent.
        return None
    if proc.returncode != 0:
        # Non-zero exit means the item is not in the Keychain (or access was
        # denied). Either way there's nothing to import — do NOT echo stderr,
        # which may reference the secret's storage path.
        return None
    out = proc.stdout.strip()
    return out or None


__all__ = [
    "CLAUDE_CODE_CREDENTIALS_DEFAULT_PATH",
    "ClaudeCodeCredentialsMalformed",
    "read_claude_code_credentials",
]
