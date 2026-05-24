"""Tests for the ``must_change_password`` doctor check.

Verifies both arms:
* ``warn`` when the seeded ``admin/root`` flag is still set
* ``ok``  when the operator has rotated credentials
* ``warn`` when the config / admin block is absent (degraded, not failed)
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from corlinman_server.cli.doctor import _check_must_change_password
from corlinman_server.cli.main import cli


_DEFAULT_FLAG_TOML = """\
[admin]
username = "admin"
password_hash = "$argon2id$default"
must_change_password = true
"""

_ROTATED_TOML = """\
[admin]
username = "admin"
password_hash = "$argon2id$rotated"
must_change_password = false
"""

_NO_FLAG_TOML = """\
[admin]
username = "admin"
password_hash = "$argon2id$nokey"
"""


class TestCheckMustChangePassword:
    def test_warn_when_default_flag_active(self, tmp_path: Path) -> None:
        (tmp_path / "config.toml").write_text(_DEFAULT_FLAG_TOML, encoding="utf-8")
        report = _check_must_change_password(tmp_path)
        assert report.name == "must_change_password"
        assert report.status == "warn"
        assert "default password" in report.message
        assert report.hint and "corlinman init" in report.hint

    def test_ok_when_flag_explicitly_false(self, tmp_path: Path) -> None:
        (tmp_path / "config.toml").write_text(_ROTATED_TOML, encoding="utf-8")
        report = _check_must_change_password(tmp_path)
        assert report.status == "ok"
        assert "custom" in report.message

    def test_ok_when_flag_absent(self, tmp_path: Path) -> None:
        """No ``must_change_password`` key → treated as false (already rotated)."""
        (tmp_path / "config.toml").write_text(_NO_FLAG_TOML, encoding="utf-8")
        report = _check_must_change_password(tmp_path)
        assert report.status == "ok"

    def test_warn_when_config_missing(self, tmp_path: Path) -> None:
        report = _check_must_change_password(tmp_path)
        assert report.status == "warn"
        assert "config.toml absent" in report.message

    def test_warn_when_admin_block_missing(self, tmp_path: Path) -> None:
        (tmp_path / "config.toml").write_text(
            '[server]\nport = 6005\n', encoding="utf-8"
        )
        report = _check_must_change_password(tmp_path)
        assert report.status == "warn"
        assert "[admin]" in report.message or "admin" in report.message

    def test_warn_on_malformed_toml(self, tmp_path: Path) -> None:
        """Malformed TOML → warn (not fail) so doctor stays best-effort."""
        (tmp_path / "config.toml").write_text(
            "not valid toml !!! = [", encoding="utf-8"
        )
        report = _check_must_change_password(tmp_path)
        assert report.status == "warn"
        assert "parse" in report.message

    def test_appears_in_doctor_json(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli, ["doctor", "--json", "--data-dir", str(tmp_path)]
        )
        assert result.exit_code in (0, 1), result.output
        payload = json.loads(result.output)
        names = {item["name"] for item in payload}
        assert "must_change_password" in names

    def test_module_filter(self, tmp_path: Path) -> None:
        (tmp_path / "config.toml").write_text(_DEFAULT_FLAG_TOML, encoding="utf-8")
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "doctor",
                "--json",
                "--module",
                "must_change_password",
                "--data-dir",
                str(tmp_path),
            ],
        )
        assert result.exit_code in (0, 1), result.output
        payload = json.loads(result.output)
        assert len(payload) == 1
        assert payload[0]["name"] == "must_change_password"
        assert payload[0]["status"] == "warn"
