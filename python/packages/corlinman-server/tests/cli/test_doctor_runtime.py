"""Tests for the runtime-wiring doctor checks added in Parcel P12.

Covers :func:`_check_runtime_config`, :func:`_check_provider_registry`,
and :func:`_check_runtime_wiring` as well as their integration via the
``corlinman doctor`` CLI command.

Each check must:
* return the right status for the happy / degraded / failed paths;
* never raise (degrade gracefully on any unexpected error);
* be selectable via ``--module``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from corlinman_server.cli.doctor import (
    CheckReport,
    _check_provider_registry,
    _check_runtime_config,
    _check_runtime_wiring,
)
from corlinman_server.cli.main import cli


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_MINIMAL_TOML = """\
[server]
host = "0.0.0.0"
port = 8080

[admin]
user = "admin"
"""

_PROVIDER_TOML = """\
[server]
host = "0.0.0.0"
port = 8080

[admin]
user = "admin"

[providers.mock]
kind = "mock"
"""

_MALFORMED_TOML = "not valid toml !!!= [\n"


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return tmp_path  # return the data_dir, not the file


# ---------------------------------------------------------------------------
# _check_runtime_config
# ---------------------------------------------------------------------------


class TestCheckRuntimeConfig:
    def test_missing_file_is_warn(self, tmp_path: Path) -> None:
        report = _check_runtime_config(tmp_path)
        assert report.name == "runtime_config"
        assert report.status == "warn"
        assert "not present" in report.message

    def test_valid_toml_is_ok(self, tmp_path: Path) -> None:
        _write(tmp_path, "config.toml", _MINIMAL_TOML)
        report = _check_runtime_config(tmp_path)
        assert report.status == "ok"
        assert "server" in report.message  # sections listed

    def test_malformed_toml_is_fail(self, tmp_path: Path) -> None:
        _write(tmp_path, "config.toml", _MALFORMED_TOML)
        report = _check_runtime_config(tmp_path)
        assert report.status == "fail"
        assert "failed to load" in report.message

    def test_valid_toml_lists_sections(self, tmp_path: Path) -> None:
        _write(tmp_path, "config.toml", _PROVIDER_TOML)
        report = _check_runtime_config(tmp_path)
        assert report.status == "ok"
        assert "providers" in report.message

    def test_degrade_on_import_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulate a broken gateway.core.config import — must not raise."""
        _write(tmp_path, "config.toml", _MINIMAL_TOML)

        def _broken_load(*_args, **_kw):
            raise RuntimeError("simulated import error")

        import corlinman_server.gateway.core.config as _cfg_mod
        monkeypatch.setattr(_cfg_mod, "load_from_path", _broken_load)

        # Re-import the check to pick up the patch
        import corlinman_server.cli.doctor as _doc
        report = _doc._check_runtime_config(tmp_path)
        assert report.status == "fail"
        assert "simulated import error" in report.message


# ---------------------------------------------------------------------------
# _check_provider_registry
# ---------------------------------------------------------------------------


class TestCheckProviderRegistry:
    def test_missing_file_is_warn(self, tmp_path: Path) -> None:
        report = _check_provider_registry(tmp_path)
        assert report.name == "provider_registry"
        assert report.status == "warn"

    def test_no_providers_section_is_warn(self, tmp_path: Path) -> None:
        _write(tmp_path, "config.toml", _MINIMAL_TOML)
        report = _check_provider_registry(tmp_path)
        assert report.status == "warn"
        assert "0 specs" in report.message or "0 spec" in report.message

    def test_mock_provider_is_ok(self, tmp_path: Path) -> None:
        _write(tmp_path, "config.toml", _PROVIDER_TOML)
        report = _check_provider_registry(tmp_path)
        assert report.status == "ok"
        assert "1 spec" in report.message

    def test_malformed_toml_is_fail(self, tmp_path: Path) -> None:
        _write(tmp_path, "config.toml", _MALFORMED_TOML)
        report = _check_provider_registry(tmp_path)
        assert report.status == "fail"

    def test_degrade_gracefully_on_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(tmp_path, "config.toml", _PROVIDER_TOML)

        import corlinman_server.gateway.providers as _prov
        monkeypatch.setattr(
            _prov, "build_registry", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom"))
        )

        import corlinman_server.cli.doctor as _doc
        report = _doc._check_provider_registry(tmp_path)
        assert report.status == "fail"
        assert "boom" in report.message


# ---------------------------------------------------------------------------
# _check_runtime_wiring
# ---------------------------------------------------------------------------


class TestCheckRuntimeWiring:
    def test_missing_file_is_warn(self, tmp_path: Path) -> None:
        report = _check_runtime_wiring(tmp_path)
        assert report.name == "runtime_wiring"
        assert report.status == "warn"

    def test_no_providers_is_degraded(self, tmp_path: Path) -> None:
        """No providers → registry empty → ChatService None → degraded."""
        _write(tmp_path, "config.toml", _MINIMAL_TOML)
        report = _check_runtime_wiring(tmp_path)
        assert report.status == "warn"
        assert "degraded" in report.message

    def test_with_mock_provider_is_ok(self, tmp_path: Path) -> None:
        """Mock provider → registry built → ChatService wired → ok."""
        _write(tmp_path, "config.toml", _PROVIDER_TOML)
        report = _check_runtime_wiring(tmp_path)
        assert report.status == "ok"
        assert "ok" in report.message

    def test_malformed_toml_is_fail(self, tmp_path: Path) -> None:
        _write(tmp_path, "config.toml", _MALFORMED_TOML)
        report = _check_runtime_wiring(tmp_path)
        assert report.status == "fail"

    def test_degrade_gracefully_on_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(tmp_path, "config.toml", _PROVIDER_TOML)

        import corlinman_server.gateway.core.config as _cfg_mod
        monkeypatch.setattr(
            _cfg_mod, "load_from_path", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("cfg err"))
        )

        import corlinman_server.cli.doctor as _doc
        report = _doc._check_runtime_wiring(tmp_path)
        assert report.status == "fail"
        assert "cfg err" in report.message


# ---------------------------------------------------------------------------
# CheckReport.to_dict — shape contract (shared wire shape)
# ---------------------------------------------------------------------------


def test_check_report_to_dict_without_hint() -> None:
    r = CheckReport(name="x", status="ok", message="fine")
    d = r.to_dict()
    assert d == {"name": "x", "status": "ok", "message": "fine"}
    assert "hint" not in d


def test_check_report_to_dict_with_hint() -> None:
    r = CheckReport(name="x", status="warn", message="m", hint="do X")
    d = r.to_dict()
    assert d["hint"] == "do X"


# ---------------------------------------------------------------------------
# CLI integration — ``corlinman doctor --json``
# ---------------------------------------------------------------------------


class TestDoctorCLIRuntime:
    def test_new_checks_appear_in_json(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli, ["doctor", "--json", "--data-dir", str(tmp_path)]
        )
        assert result.exit_code in (0, 1), result.output
        payload = json.loads(result.output)
        names = {item["name"] for item in payload}
        assert "runtime_config" in names
        assert "provider_registry" in names
        assert "runtime_wiring" in names

    def test_module_filter_runtime_config(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["doctor", "--json", "--module", "runtime_config", "--data-dir", str(tmp_path)],
        )
        assert result.exit_code in (0, 1), result.output
        payload = json.loads(result.output)
        assert len(payload) == 1
        assert payload[0]["name"] == "runtime_config"

    def test_module_filter_provider_registry(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["doctor", "--json", "--module", "provider_registry", "--data-dir", str(tmp_path)],
        )
        assert result.exit_code in (0, 1), result.output
        payload = json.loads(result.output)
        assert len(payload) == 1
        assert payload[0]["name"] == "provider_registry"

    def test_module_filter_runtime_wiring(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["doctor", "--json", "--module", "runtime_wiring", "--data-dir", str(tmp_path)],
        )
        assert result.exit_code in (0, 1), result.output
        payload = json.loads(result.output)
        assert len(payload) == 1
        assert payload[0]["name"] == "runtime_wiring"

    def test_all_checks_have_valid_status(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli, ["doctor", "--json", "--data-dir", str(tmp_path)]
        )
        assert result.exit_code in (0, 1), result.output
        payload = json.loads(result.output)
        for item in payload:
            assert item["status"] in ("ok", "warn", "fail"), (
                f"Check {item['name']!r} has unexpected status {item['status']!r}"
            )

    def test_doctor_with_valid_config_and_provider(self, tmp_path: Path) -> None:
        """Happy path: a config with a mock provider → runtime_wiring = ok."""
        (tmp_path / "config.toml").write_text(_PROVIDER_TOML, encoding="utf-8")
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["doctor", "--json", "--module", "runtime_wiring", "--data-dir", str(tmp_path)],
        )
        assert result.exit_code in (0, 1), result.output
        # result.output may contain structlog debug lines; find the JSON array
        # by looking for the first line that starts with "[".
        lines = result.output.splitlines()
        json_lines = "\n".join(line for line in lines if line.startswith("["))
        # The JSON block could span multiple lines - find the contiguous block.
        # More robustly: find the index of the first "\n[\n" or start-of-"[\n".
        out = result.output
        # Find the first occurrence of a newline-followed-by-'[' or start of string with '['
        import re
        m = re.search(r'(?:^|\n)(\[)', out)
        assert m is not None, f"No JSON array in output: {out!r}"
        json_start = m.start(1)
        payload = json.loads(out[json_start:])
        assert payload[0]["name"] == "runtime_wiring"
        assert payload[0]["status"] == "ok"

    def test_human_output_includes_new_checks(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli, ["doctor", "--data-dir", str(tmp_path)]
        )
        assert result.exit_code in (0, 1), result.output
        assert "runtime_config" in result.output
        assert "provider_registry" in result.output
        assert "runtime_wiring" in result.output

    def test_existing_checks_still_present(self, tmp_path: Path) -> None:
        """Existing checks (data_dir, config, python, packages) are preserved."""
        runner = CliRunner()
        result = runner.invoke(
            cli, ["doctor", "--json", "--data-dir", str(tmp_path)]
        )
        assert result.exit_code in (0, 1), result.output
        payload = json.loads(result.output)
        names = {item["name"] for item in payload}
        assert "data_dir" in names
        assert "config" in names
        assert "python" in names
        assert "packages" in names
