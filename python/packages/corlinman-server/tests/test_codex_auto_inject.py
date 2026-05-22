"""Tests for Codex auto-detection in ``gateway.providers.bootstrap``.

Verifies that ``_auto_inject_codex`` mutates ``state.config`` correctly
when ``~/.codex/auth.json`` is detected, and that it is a no-op when
the provider is already configured or the file is absent.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from corlinman_server.gateway.providers import _auto_inject_codex


def _state(config: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(config=config if config is not None else {})


def _detected_status(account_id: str = "test@example.com") -> Any:
    return SimpleNamespace(detected=True, account_id=account_id)


def _not_detected_status() -> Any:
    return SimpleNamespace(detected=False, account_id=None)


class TestAutoInjectCodex:
    def test_injects_when_detected_and_not_configured(self) -> None:
        state = _state({"providers": {}})
        with patch(
            "corlinman_server.gateway.oauth.codex_external.read_codex_status",
            return_value=_detected_status(),
        ):
            _auto_inject_codex(state)

        assert "codex" in state.config["providers"]
        assert state.config["providers"]["codex"]["kind"] == "codex"
        assert state.config["models"]["default"] == "o4-mini"

    def test_no_op_when_codex_already_configured(self) -> None:
        state = _state({"providers": {"codex": {"kind": "codex", "api_key": "manual"}}})
        with patch(
            "corlinman_server.gateway.oauth.codex_external.read_codex_status",
            return_value=_detected_status(),
        ):
            _auto_inject_codex(state)

        # Should not overwrite the manual entry.
        assert state.config["providers"]["codex"]["api_key"] == "manual"
        # Should not inject models.default.
        assert "models" not in state.config

    def test_no_op_when_not_detected(self) -> None:
        state = _state({"providers": {}})
        with patch(
            "corlinman_server.gateway.oauth.codex_external.read_codex_status",
            return_value=_not_detected_status(),
        ):
            _auto_inject_codex(state)

        assert "codex" not in state.config.get("providers", {})

    def test_no_op_when_file_absent(self) -> None:
        state = _state({"providers": {}})
        with patch(
            "corlinman_server.gateway.oauth.codex_external.read_codex_status",
            return_value=None,
        ):
            _auto_inject_codex(state)

        assert "codex" not in state.config.get("providers", {})

    def test_does_not_overwrite_existing_models_default(self) -> None:
        state = _state({
            "providers": {},
            "models": {"default": "claude-sonnet-4-5"},
        })
        with patch(
            "corlinman_server.gateway.oauth.codex_external.read_codex_status",
            return_value=_detected_status(),
        ):
            _auto_inject_codex(state)

        assert state.config["models"]["default"] == "claude-sonnet-4-5"

    def test_creates_providers_dict_when_absent(self) -> None:
        state = _state({})  # No "providers" key
        with patch(
            "corlinman_server.gateway.oauth.codex_external.read_codex_status",
            return_value=_detected_status(),
        ):
            _auto_inject_codex(state)

        assert "codex" in state.config["providers"]

    def test_no_op_when_config_is_none(self) -> None:
        state = SimpleNamespace(config=None)
        with patch(
            "corlinman_server.gateway.oauth.codex_external.read_codex_status",
            return_value=_detected_status(),
        ):
            _auto_inject_codex(state)  # must not raise

        assert state.config is None

    def test_no_op_when_config_is_not_dict(self) -> None:
        state = SimpleNamespace(config="not-a-dict")
        with patch(
            "corlinman_server.gateway.oauth.codex_external.read_codex_status",
            return_value=_detected_status(),
        ):
            _auto_inject_codex(state)  # must not raise

    def test_alias_added_for_o4_mini(self) -> None:
        state = _state({"providers": {}})
        with patch(
            "corlinman_server.gateway.oauth.codex_external.read_codex_status",
            return_value=_detected_status(),
        ):
            _auto_inject_codex(state)

        aliases = state.config["models"]["aliases"]
        assert "o4-mini" in aliases
        assert aliases["o4-mini"]["provider"] == "codex"
        assert aliases["o4-mini"]["model"] == "o4-mini"
