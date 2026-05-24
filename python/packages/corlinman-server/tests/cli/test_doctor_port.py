"""Tests for the ``port_bindable`` doctor check.

Covers:
* free port  → ``ok``
* occupied port → ``warn`` (unknown holder) or ``ok`` (corlinman holder)
* port resolution from TOML / env / default
"""

from __future__ import annotations

import json
import socket
from contextlib import closing
from pathlib import Path

import pytest
from click.testing import CliRunner

from corlinman_server.cli.doctor import (
    _check_port_bindable,
    _resolve_port,
)
from corlinman_server.cli.main import cli


def _free_port() -> int:
    """Ask the kernel for a free port, then immediately release it.

    Race-y in principle but fine in practice — the window between this
    helper returning and the check rebinding is microseconds.
    """
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _config_with_port(tmp_path: Path, port: int) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(f"[server]\nport = {port}\n", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# _resolve_port
# ---------------------------------------------------------------------------


class TestResolvePort:
    def test_default_port(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CORLINMAN_PORT", raising=False)
        monkeypatch.delenv("PORT", raising=False)
        assert _resolve_port(tmp_path) == 6005

    def test_from_config_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CORLINMAN_PORT", raising=False)
        monkeypatch.delenv("PORT", raising=False)
        _config_with_port(tmp_path, 7777)
        assert _resolve_port(tmp_path) == 7777

    def test_corlinman_port_env_overrides_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CORLINMAN_PORT", "8888")
        monkeypatch.delenv("PORT", raising=False)
        # No config file → env wins
        assert _resolve_port(tmp_path) == 8888

    def test_bind_form_parsed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CORLINMAN_PORT", raising=False)
        monkeypatch.delenv("PORT", raising=False)
        (tmp_path / "config.toml").write_text(
            '[server]\nbind = "0.0.0.0:9090"\n', encoding="utf-8"
        )
        assert _resolve_port(tmp_path) == 9090


# ---------------------------------------------------------------------------
# _check_port_bindable
# ---------------------------------------------------------------------------


class TestCheckPortBindable:
    def test_ok_when_port_free(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        port = _free_port()
        monkeypatch.delenv("CORLINMAN_PORT", raising=False)
        monkeypatch.delenv("PORT", raising=False)
        _config_with_port(tmp_path, port)
        report = _check_port_bindable(tmp_path)
        assert report.name == "port_bindable"
        assert report.status == "ok"
        assert str(port) in report.message
        assert "available" in report.message

    def test_warn_when_port_occupied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bind a real socket to occupy a port, then run the check."""
        monkeypatch.delenv("CORLINMAN_PORT", raising=False)
        monkeypatch.delenv("PORT", raising=False)
        # Hold a port for the duration of this test.
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            occupied_port = holder.getsockname()[1]
            _config_with_port(tmp_path, occupied_port)
            report = _check_port_bindable(tmp_path)
            assert report.name == "port_bindable"
            assert report.status == "warn"
            assert str(occupied_port) in report.message
            assert report.hint is not None

    def test_appears_in_doctor_json(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli, ["doctor", "--json", "--data-dir", str(tmp_path)]
        )
        assert result.exit_code in (0, 1), result.output
        payload = json.loads(result.output)
        names = {item["name"] for item in payload}
        assert "port_bindable" in names

    def test_module_filter(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        port = _free_port()
        monkeypatch.delenv("CORLINMAN_PORT", raising=False)
        monkeypatch.delenv("PORT", raising=False)
        _config_with_port(tmp_path, port)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "doctor",
                "--json",
                "--module",
                "port_bindable",
                "--data-dir",
                str(tmp_path),
            ],
        )
        assert result.exit_code in (0, 1), result.output
        payload = json.loads(result.output)
        assert len(payload) == 1
        assert payload[0]["name"] == "port_bindable"
