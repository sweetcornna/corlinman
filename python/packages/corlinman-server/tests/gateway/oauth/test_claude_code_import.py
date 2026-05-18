"""Tests for ``corlinman_server.gateway.oauth.claude_code_import``.

Coverage:

* A valid ``~/.claude/.credentials.json`` shape parses into an
  :class:`OAuthCredential` carrying ``provider="anthropic"``.
* Missing file → returns ``None`` (the operator simply doesn't have
  Claude Code installed; the import endpoint surfaces that as 404).
* Malformed JSON → raises ``ClaudeCodeCredentialsMalformed`` (the
  operator's keychain is in a weird state and they should investigate).
* Missing ``claudeAiOauth`` block → returns ``None``.
* ``scopes`` list joins with spaces; string scope passes through.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corlinman_server.gateway.oauth.claude_code_import import (
    ClaudeCodeCredentialsMalformed,
    read_claude_code_credentials,
)


def _write(path: Path, body: object) -> None:
    path.write_text(json.dumps(body), encoding="utf-8")


def test_missing_file_returns_none(tmp_path: Path) -> None:
    target = tmp_path / ".credentials.json"
    assert not target.exists()
    assert read_claude_code_credentials(target) is None


def test_valid_credentials_parse(tmp_path: Path) -> None:
    target = tmp_path / ".credentials.json"
    _write(
        target,
        {
            "claudeAiOauth": {
                "accessToken": "sk-ant-oat01-abc",
                "refreshToken": "rt-xyz",
                "expiresAt": 1_900_000_000_000,
                "scopes": ["user:inference", "user:profile"],
            }
        },
    )
    cred = read_claude_code_credentials(target)
    assert cred is not None
    assert cred.provider == "anthropic"
    assert cred.access_token == "sk-ant-oat01-abc"
    assert cred.refresh_token == "rt-xyz"
    assert cred.expires_at_ms == 1_900_000_000_000
    assert cred.scope == "user:inference user:profile"


def test_string_scope_passes_through(tmp_path: Path) -> None:
    target = tmp_path / ".credentials.json"
    _write(
        target,
        {"claudeAiOauth": {"accessToken": "a", "scopes": "user:inference"}},
    )
    cred = read_claude_code_credentials(target)
    assert cred is not None
    assert cred.scope == "user:inference"


def test_missing_oauth_block_returns_none(tmp_path: Path) -> None:
    target = tmp_path / ".credentials.json"
    _write(target, {"someOtherField": "x"})
    assert read_claude_code_credentials(target) is None


def test_missing_access_token_returns_none(tmp_path: Path) -> None:
    target = tmp_path / ".credentials.json"
    _write(target, {"claudeAiOauth": {"refreshToken": "rt"}})
    assert read_claude_code_credentials(target) is None


def test_malformed_json_raises(tmp_path: Path) -> None:
    target = tmp_path / ".credentials.json"
    target.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ClaudeCodeCredentialsMalformed, match="not valid JSON"):
        read_claude_code_credentials(target)


def test_non_object_root_raises(tmp_path: Path) -> None:
    target = tmp_path / ".credentials.json"
    target.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ClaudeCodeCredentialsMalformed, match="not a JSON object"):
        read_claude_code_credentials(target)


def test_zero_expires_at_yields_none(tmp_path: Path) -> None:
    target = tmp_path / ".credentials.json"
    _write(target, {"claudeAiOauth": {"accessToken": "a", "expiresAt": 0}})
    cred = read_claude_code_credentials(target)
    assert cred is not None
    assert cred.expires_at_ms is None
