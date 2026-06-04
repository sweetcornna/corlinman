"""Focused tests for ``corlinman migrate-persona-body``.

Covers:
* --dry-run shows a diff when the body has drifted, makes no writes
* --force applies the update on a stale row
* --force is a no-op when the row is already up-to-date
* Refuses to update a non-builtin row (wrong is_builtin guard)
* Refuses when the persona db does not exist
* Refuses when persona_id is not a known builtin
* --dry-run and --force together produce an error
* Neither --dry-run nor --force produces an error
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from click.testing import CliRunner
from corlinman_server.cli.main import cli
from corlinman_server.persona.store import Persona, PersonaStore, seed_builtin_personas

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _persona_db(tmp_path: Path) -> Path:
    return tmp_path / "personas.sqlite"


async def _make_store(db_path: Path) -> PersonaStore:
    return await PersonaStore.open(db_path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_flags_is_an_error(tmp_path: Path) -> None:
    """Neither --dry-run nor --force must print an error and exit non-zero."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["migrate-persona-body", "--persona-db", str(_persona_db(tmp_path))],
    )
    assert result.exit_code != 0
    assert "--dry-run" in result.output or "error" in result.output.lower()


def test_dry_run_and_force_together_are_rejected(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "migrate-persona-body",
            "--dry-run",
            "--force",
            "--persona-db",
            str(_persona_db(tmp_path)),
        ],
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower() or "error" in result.output.lower()


def test_unknown_builtin_id_errors(tmp_path: Path) -> None:
    db = _persona_db(tmp_path)
    # Create an empty DB so the path check doesn't fire first.
    asyncio.run(PersonaStore.open(db)).close  # noqa: B018 — just touch the file
    asyncio.run(_touch_db(db))

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "migrate-persona-body",
            "--id",
            "totally-unknown-persona-xyz",
            "--dry-run",
            "--persona-db",
            str(db),
        ],
    )
    assert result.exit_code != 0
    assert "totally-unknown-persona-xyz" in result.output or "not a known" in result.output


async def _touch_db(db_path: Path) -> None:
    """Open and immediately close a persona store to ensure the file exists."""
    store = await PersonaStore.open(db_path)
    await store.close()


def test_missing_db_errors(tmp_path: Path) -> None:
    nonexistent = tmp_path / "nope" / "personas.sqlite"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "migrate-persona-body",
            "--dry-run",
            "--persona-db",
            str(nonexistent),
        ],
    )
    assert result.exit_code != 0
    assert "not found" in result.output.lower() or "error" in result.output.lower()


def test_dry_run_detects_stale_body(tmp_path: Path) -> None:
    """--dry-run shows a diff when the stored body differs from the bundle."""
    db = _persona_db(tmp_path)

    async def _setup() -> None:
        store = await PersonaStore.open(db)
        # Seed so the row exists, then overwrite with a stale body via update().
        await seed_builtin_personas(store)
        await store.update("grantley", system_prompt="STALE BODY")
        await store.close()

    asyncio.run(_setup())

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["migrate-persona-body", "--dry-run", "--persona-db", str(db)],
    )
    assert result.exit_code == 0, result.output
    # Diff output should contain the stale marker and indicate dry-run.
    assert "STALE BODY" in result.output
    assert "dry-run" in result.output.lower()


def test_dry_run_no_change_reports_up_to_date(tmp_path: Path) -> None:
    """--dry-run with an already-current row reports "up-to-date"."""
    db = _persona_db(tmp_path)

    async def _setup() -> None:
        store = await PersonaStore.open(db)
        await seed_builtin_personas(store)
        await store.close()

    asyncio.run(_setup())

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["migrate-persona-body", "--dry-run", "--persona-db", str(db)],
    )
    assert result.exit_code == 0, result.output
    assert "up-to-date" in result.output.lower()


def test_force_updates_stale_body(tmp_path: Path) -> None:
    """--force writes the bundled body when the stored body is stale."""
    from corlinman_server.persona.default_grantley import (
        DEFAULT_GRANTLEY_ID,
        load_default_grantley_body,
    )

    db = _persona_db(tmp_path)

    async def _setup() -> None:
        store = await PersonaStore.open(db)
        await seed_builtin_personas(store)
        await store.update(DEFAULT_GRANTLEY_ID, system_prompt="STALE BODY")
        await store.close()

    asyncio.run(_setup())

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["migrate-persona-body", "--force", "--persona-db", str(db)],
    )
    assert result.exit_code == 0, result.output
    assert "updated" in result.output.lower()

    # Verify the DB row now matches the bundled body.
    async def _verify() -> str:
        store = await PersonaStore.open(db)
        p = await store.get(DEFAULT_GRANTLEY_ID)
        await store.close()
        assert p is not None
        return p.system_prompt

    body = asyncio.run(_verify())
    assert body == load_default_grantley_body()


def test_force_no_op_when_already_current(tmp_path: Path) -> None:
    """--force reports nothing to do when the stored body is already current."""
    db = _persona_db(tmp_path)

    async def _setup() -> None:
        store = await PersonaStore.open(db)
        await seed_builtin_personas(store)
        await store.close()

    asyncio.run(_setup())

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["migrate-persona-body", "--force", "--persona-db", str(db)],
    )
    assert result.exit_code == 0, result.output
    assert "up-to-date" in result.output.lower()


def test_refuses_non_builtin_row(tmp_path: Path) -> None:
    """--force must not clobber a non-builtin row that happens to use a
    builtin persona id (e.g. operator inserted 'grantley' manually without
    the is_builtin flag)."""
    from corlinman_server.persona.default_grantley import DEFAULT_GRANTLEY_ID

    db = _persona_db(tmp_path)

    now = int(time.time() * 1000)
    custom = Persona(
        id=DEFAULT_GRANTLEY_ID,
        display_name="Custom Grantley",
        short_summary="custom",
        system_prompt="custom body",
        is_builtin=False,  # NOT a builtin
        created_at_ms=now,
        updated_at_ms=now,
    )

    async def _setup() -> None:
        store = await PersonaStore.open(db)
        await store.create(custom)
        await store.close()

    asyncio.run(_setup())

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["migrate-persona-body", "--force", "--persona-db", str(db)],
    )
    assert result.exit_code != 0
    assert "is_builtin" in result.output or "not flagged" in result.output.lower()


def test_missing_seeded_row_errors(tmp_path: Path) -> None:
    """--force must error when the row simply doesn't exist yet (empty DB)."""
    db = _persona_db(tmp_path)
    asyncio.run(_touch_db(db))

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["migrate-persona-body", "--force", "--persona-db", str(db)],
    )
    assert result.exit_code != 0
    assert "does not exist" in result.output.lower() or "error" in result.output.lower()
