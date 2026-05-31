"""Gap-fill (lane-providers) Claude Code macOS-Keychain import tests.

Claude Code >= 2.1.114 stores its OAuth bundle in the macOS login Keychain
under the ``Claude Code-credentials`` generic-password service instead of
``~/.claude/.credentials.json``. When the on-disk file is absent (and the
DEFAULT path was used) the importer now shells out to
``security find-generic-password -s "Claude Code-credentials" -w`` and parses
the returned JSON blob.

These tests mock ``subprocess.run`` so no real Keychain access happens, and
force the default-path branch by pointing the default at a nonexistent file.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import corlinman_server.gateway.oauth.claude_code_import as cci
from corlinman_server.gateway.oauth.claude_code_import import (
    read_claude_code_credentials,
)


def _force_default_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the default credentials path at a guaranteed-absent file and
    force the darwin code path so the Keychain branch is reachable on CI."""
    monkeypatch.setattr(
        cci, "CLAUDE_CODE_CREDENTIALS_DEFAULT_PATH", tmp_path / "nope.json"
    )
    monkeypatch.setattr(cci.sys, "platform", "darwin")


def test_keychain_fallback_invoked_when_file_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _force_default_missing(monkeypatch, tmp_path)

    blob = json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "sk-ant-oat01-keychain",
                "refreshToken": "rt-keychain",
                "expiresAt": 1_900_000_000_000,
                "scopes": ["user:inference"],
            }
        }
    )

    recorded: dict[str, Any] = {}

    def _fake_run(cmd: list[str], **kwargs: Any) -> Any:
        recorded["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout=blob + "\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    cred = read_claude_code_credentials()
    assert cred is not None
    assert cred.provider == "anthropic"
    assert cred.access_token == "sk-ant-oat01-keychain"
    assert cred.refresh_token == "rt-keychain"
    assert cred.scope == "user:inference"

    # The exact security CLI invocation, including the service name.
    assert recorded["cmd"][:2] == ["security", "find-generic-password"]
    assert "Claude Code-credentials" in recorded["cmd"]
    assert "-w" in recorded["cmd"]


def test_keychain_miss_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _force_default_missing(monkeypatch, tmp_path)

    def _fake_run(cmd: list[str], **kwargs: Any) -> Any:
        # Non-zero exit = item not in the Keychain.
        return SimpleNamespace(returncode=44, stdout="", stderr="not found")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert read_claude_code_credentials() is None


def test_keychain_skipped_on_non_darwin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        cci, "CLAUDE_CODE_CREDENTIALS_DEFAULT_PATH", tmp_path / "nope.json"
    )
    monkeypatch.setattr(cci.sys, "platform", "linux")

    called = {"n": 0}

    def _fake_run(cmd: list[str], **kwargs: Any) -> Any:  # pragma: no cover
        called["n"] += 1
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert read_claude_code_credentials() is None
    assert called["n"] == 0


def test_keychain_security_binary_missing_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _force_default_missing(monkeypatch, tmp_path)

    def _fake_run(cmd: list[str], **kwargs: Any) -> Any:
        raise FileNotFoundError("security not found")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert read_claude_code_credentials() is None


def test_keychain_timeout_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _force_default_missing(monkeypatch, tmp_path)

    def _fake_run(cmd: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd, 10)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert read_claude_code_credentials() is None


def test_explicit_path_never_reaches_keychain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit ``path=`` to a missing file returns None WITHOUT touching
    the Keychain — explicit path means 'read exactly this file'."""
    monkeypatch.setattr(cci.sys, "platform", "darwin")
    called = {"n": 0}

    def _fake_run(cmd: list[str], **kwargs: Any) -> Any:  # pragma: no cover
        called["n"] += 1
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert read_claude_code_credentials(tmp_path / "absent.json") is None
    assert called["n"] == 0
