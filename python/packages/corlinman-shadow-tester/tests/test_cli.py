"""Smoke tests for the ``corlinman-shadow-tester`` CLI.

Exercises:
- argparse surface (--help, run-once --help)
- disabled-config short-circuit (enabled=false → exit 0, zero summary)
- missing config file → exit 2
- malformed TOML → exit 2
- happy run-once against a real evolution.sqlite with no pending proposals
- JSON summary flag
- module export for [project.scripts]
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from corlinman_evolution_store import EvolutionStore
from corlinman_shadow_tester import cli
from corlinman_shadow_tester.cli import (
    _resolve_eval_set_dir,
    _resolve_evolution_db_path,
    _resolve_kb_path,
    _shadow_section,
    main,
)

# ---------------------------------------------------------------------------
# Help / argparse smoke tests
# ---------------------------------------------------------------------------


def test_help_smoke(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "corlinman-shadow-tester" in out
    assert "run-once" in out


def test_run_once_help_smoke(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["run-once", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--config" in out
    assert "--max-proposals" in out


# ---------------------------------------------------------------------------
# Config-loading helpers
# ---------------------------------------------------------------------------


def test_shadow_section_missing_returns_empty() -> None:
    assert _shadow_section({}) == {}


def test_shadow_section_reads_values() -> None:
    raw = {"evolution": {"shadow": {"enabled": True, "kb_path": "/kb.sqlite"}}}
    sec = _shadow_section(raw)
    assert sec["enabled"] is True
    assert sec["kb_path"] == "/kb.sqlite"


def test_resolve_evolution_db_path_prefers_override(tmp_path: Path) -> None:
    explicit = tmp_path / "my.sqlite"
    raw = {"evolution": {"observer": {"db_path": "/ignored.sqlite"}}}
    assert _resolve_evolution_db_path(raw, explicit) == explicit


def test_resolve_evolution_db_path_falls_back_to_observer() -> None:
    raw = {"evolution": {"observer": {"db_path": "/srv/evolution.sqlite"}}}
    assert _resolve_evolution_db_path(raw, None) == Path("/srv/evolution.sqlite")


def test_resolve_evolution_db_path_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    assert _resolve_evolution_db_path({}, None) == tmp_path / "evolution.sqlite"


def test_resolve_kb_path_reads_config() -> None:
    shadow = {"kb_path": "/mydata/kb.sqlite"}
    assert _resolve_kb_path(shadow, {}) == Path("/mydata/kb.sqlite")


def test_resolve_kb_path_falls_back_to_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    assert _resolve_kb_path({}, {}) == tmp_path / "kb.sqlite"


def test_resolve_eval_set_dir_reads_config() -> None:
    shadow = {"eval_set_dir": "/mydata/eval_sets"}
    assert _resolve_eval_set_dir(shadow, {}) == Path("/mydata/eval_sets")


def test_resolve_eval_set_dir_falls_back_to_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    assert _resolve_eval_set_dir({}, {}) == tmp_path / "evolution" / "eval_sets"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_run_once_missing_config_file_exits_2(tmp_path: Path) -> None:
    rc = main(["run-once", "--config", str(tmp_path / "nope.toml")])
    assert rc == 2


def test_run_once_malformed_toml_exits_2(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("[[invalid\n", encoding="utf-8")
    rc = main(["run-once", "--config", str(bad)])
    assert rc == 2


# ---------------------------------------------------------------------------
# Disabled-config short-circuit
# ---------------------------------------------------------------------------


def test_run_once_disabled_exits_0_with_zero_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When [evolution.shadow].enabled = false the CLI exits 0 with a
    zero summary (no-op path), NOT exit 2."""
    config = tmp_path / "corlinman.toml"
    config.write_text(
        "[evolution.shadow]\nenabled = false\n",
        encoding="utf-8",
    )
    rc = main(["run-once", "--config", str(config), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    summary = json.loads(out)
    assert summary == {
        "proposals_claimed": 0,
        "proposals_completed": 0,
        "proposals_failed": 0,
        "cases_run": 0,
        "errors": 0,
    }


def test_run_once_no_shadow_section_is_noop(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A config with no [evolution.shadow] section at all should behave the
    same as enabled = false."""
    config = tmp_path / "corlinman.toml"
    config.write_text("[server]\nhost = \"0.0.0.0\"\n", encoding="utf-8")
    rc = main(["run-once", "--config", str(config), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    summary = json.loads(out)
    assert summary["proposals_claimed"] == 0


# ---------------------------------------------------------------------------
# Happy-path end-to-end: real evolution.sqlite, enabled=true, empty DB
# ---------------------------------------------------------------------------


def test_run_once_end_to_end_empty_db_emits_zero_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "evolution.sqlite"
    kb = tmp_path / "kb.sqlite"
    eval_sets = tmp_path / "eval_sets"
    eval_sets.mkdir()

    # Seed the evolution schema so EvolutionStore.open succeeds.
    # main() calls asyncio.run internally, so this must be synchronous
    # (no outer event loop).
    async def _seed() -> None:
        store = await EvolutionStore.open(db)
        await store.close()

    asyncio.run(_seed())

    config = tmp_path / "corlinman.toml"
    config.write_text(
        f"""
[evolution.shadow]
enabled = true
kb_path = "{kb.as_posix()}"
eval_set_dir = "{eval_sets.as_posix()}"

[evolution.observer]
db_path = "{db.as_posix()}"
""",
        encoding="utf-8",
    )

    rc = main(["run-once", "--config", str(config), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    summary = json.loads(out)
    assert summary == {
        "proposals_claimed": 0,
        "proposals_completed": 0,
        "proposals_failed": 0,
        "cases_run": 0,
        "errors": 0,
    }


# ---------------------------------------------------------------------------
# Module export check
# ---------------------------------------------------------------------------


def test_module_exports_main_for_console_script() -> None:
    """``[project.scripts]`` points at ``corlinman_shadow_tester.cli:main``.
    Smoke that the attribute resolves."""
    assert callable(cli.main)
