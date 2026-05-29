"""Tests for ``corlinman init`` — the interactive headless-setup wizard.

Covers the happy path: user accepts password rotation, picks a mock
provider, sets a default model alias, declines embeddings. The CliRunner
feeds prompts via stdin; assertions check the on-disk TOML shape matches
what ``POST /admin/onboard/finalize`` would have written.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner
from corlinman_server.cli.init import (
    _atomic_write_toml,
    _load_existing,
    _resolve_config_path,
    _supported_kinds,
)
from corlinman_server.cli.main import cli

# ---------------------------------------------------------------------------
# Unit-level: helper functions
# ---------------------------------------------------------------------------


class TestSupportedKinds:
    def test_returns_non_empty(self) -> None:
        kinds = _supported_kinds()
        assert kinds, "must return at least one provider kind"
        # ``mock`` should always be there — it's the zero-credential path
        assert "mock" in kinds


class TestLoadExisting:
    def test_missing_file_is_empty_dict(self, tmp_path: Path) -> None:
        assert _load_existing(tmp_path / "absent.toml") == {}

    def test_malformed_toml_is_empty_dict(self, tmp_path: Path) -> None:
        p = tmp_path / "broken.toml"
        p.write_text("not valid toml !!! = [\n", encoding="utf-8")
        assert _load_existing(p) == {}

    def test_valid_toml_is_parsed(self, tmp_path: Path) -> None:
        p = tmp_path / "ok.toml"
        p.write_text('[admin]\nusername = "admin"\n', encoding="utf-8")
        parsed = _load_existing(p)
        assert parsed["admin"]["username"] == "admin"


class TestAtomicWriteToml:
    def test_writes_and_replaces(self, tmp_path: Path) -> None:
        p = tmp_path / "config.toml"
        _atomic_write_toml(p, {"server": {"port": 6005}})
        assert p.exists()
        parsed = tomllib.loads(p.read_text(encoding="utf-8"))
        assert parsed["server"]["port"] == 6005

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        p = tmp_path / "nested" / "deep" / "config.toml"
        _atomic_write_toml(p, {"x": 1})
        assert p.exists()


class TestResolveConfigPath:
    def test_explicit_config_wins(self, tmp_path: Path) -> None:
        explicit = tmp_path / "custom.toml"
        assert _resolve_config_path(explicit, None) == explicit

    def test_data_dir_fallback(self, tmp_path: Path) -> None:
        out = _resolve_config_path(None, tmp_path)
        assert out == tmp_path / "config.toml"


# ---------------------------------------------------------------------------
# CLI integration — happy path through the wizard
# ---------------------------------------------------------------------------


class TestInitCLI:
    def test_happy_path_writes_provider_and_model_alias(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Run with a mock provider end-to-end; verify TOML output shape."""
        # No prior config; default admin block absent — wizard will offer to
        # set credentials. We say "no" to the password change (no admin block
        # exists to flag must_change_password=true) and pick the mock
        # provider so we don't hit the argon2 hash path.
        config_path = tmp_path / "config.toml"

        # CliRunner stdin-feeding: each line = one prompt response. Order:
        #   1. "Change admin password now?"   → n
        #   2. "Configure an LLM provider now?" → y (default)
        #   3. "Pick a kind (number or name)" → mock
        #   4. "Provider slot name"           → mock (default)
        #   5. "Default model alias"          → mock-default
        #   6. "Enable an embedding provider?" → n (default)
        stdin = "n\ny\nmock\nmock\nmock-default\nn\n"

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--config", str(config_path)],
            input=stdin,
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert config_path.exists(), result.output

        parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))

        # Provider entry matches /admin/onboard/finalize wire shape
        assert "providers" in parsed
        assert "mock" in parsed["providers"]
        mock_entry = parsed["providers"]["mock"]
        assert mock_entry["kind"] == "mock"
        assert mock_entry["enabled"] is True
        # mock has no api_key (we skip the prompt for kind == "mock")
        assert "api_key" not in mock_entry

        # Default model alias
        assert parsed["models"]["default"] == "mock-default"
        assert "mock-default" in parsed["models"]["aliases"]
        alias = parsed["models"]["aliases"]["mock-default"]
        assert alias["provider"] == "mock"
        assert alias["model"] == "mock-default"

        # No embedding (we declined)
        assert "embedding" not in parsed

    def test_no_changes_skips_write(self, tmp_path: Path) -> None:
        """When operator declines all prompts, no file should be written."""
        config_path = tmp_path / "config.toml"
        runner = CliRunner()
        # n = no password change, n = no provider configure
        result = runner.invoke(
            cli,
            ["init", "--config", str(config_path)],
            input="n\nn\n",
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert not config_path.exists()
        assert "left unchanged" in result.output

    def test_preserves_existing_sections(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Existing unrelated sections must survive the write."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "[server]\nport = 6005\n\n"
            '[admin]\nusername = "admin"\npassword_hash = "$argon2id$preserved"\n'
            "must_change_password = false\n",
            encoding="utf-8",
        )

        # Decline password change (must_change_password is already false → soft
        # prompt), accept provider config (mock).
        stdin = "n\ny\nmock\nmock\nmodel-x\nn\n"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--config", str(config_path)],
            input=stdin,
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

        parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
        # [server] preserved
        assert parsed["server"]["port"] == 6005
        # [admin] preserved verbatim
        assert parsed["admin"]["password_hash"] == "$argon2id$preserved"
        # New provider added
        assert "mock" in parsed["providers"]
        assert parsed["models"]["default"] == "model-x"

    def test_with_embedding_writes_embedding_block(self, tmp_path: Path) -> None:
        """When operator opts in to embeddings, the [embedding] block lands."""
        config_path = tmp_path / "config.toml"
        # n password, y provider, mock kind, mock slot, default alias,
        # y embedding, embedding-model name
        stdin = "n\ny\nmock\nmock\ndefault\ny\ntext-embedding-3-small\n"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--config", str(config_path)],
            input=stdin,
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
        assert parsed["embedding"]["provider"] == "mock"
        assert parsed["embedding"]["model"] == "text-embedding-3-small"
        assert parsed["embedding"]["enabled"] is True

    def test_init_is_registered_in_root_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "init" in result.output
