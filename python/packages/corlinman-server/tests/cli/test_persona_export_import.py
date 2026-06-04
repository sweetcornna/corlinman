"""Round-trip tests for ``corlinman persona-export`` / ``persona-import``.

Covers:
* export then import reproduces persona record + assets
* export --id targets a single persona
* export --all-custom skips builtins
* import skips builtin rows without --overwrite
* import --overwrite updates an existing (including builtin) row
* import rejects a bundle without persona.json
* export of nonexistent persona id errors
* export with no custom personas reports nothing-to-export
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from corlinman_server.cli.main import cli
from corlinman_server.persona.asset_store import PersonaAssetStore
from corlinman_server.persona.store import Persona, PersonaStore, seed_builtin_personas

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

# Minimal valid PNG bytes (1×1 pixel) for asset upload.
_PNG_MAGIC = bytes.fromhex(
    "89504E470D0A1A0A0000000D494844520000000100000001"
    "0802000000907753DE"
)

_RUNNER = CliRunner()


def _png(extra: int = 0) -> bytes:
    return _PNG_MAGIC + b"\x00" * extra


async def _open_ps(db: Path) -> PersonaStore:
    return await PersonaStore.open(db)


async def _open_pas(sqlite: Path, base: Path) -> PersonaAssetStore:
    return await PersonaAssetStore.open(sqlite, base)


def _now() -> int:
    import time
    return int(time.time() * 1000)


async def _insert_custom(db: Path, persona_id: str, display: str = "Test") -> None:
    now = _now()
    ps = await _open_ps(db)
    candidate = Persona(
        id=persona_id,
        display_name=display,
        short_summary="A test persona",
        system_prompt="You are a test persona.",
        is_builtin=False,
        created_at_ms=now,
        updated_at_ms=now,
    )
    try:
        await ps.create(candidate)
    finally:
        await ps.close()


async def _add_asset(sqlite: Path, base: Path, persona_id: str, label: str) -> None:
    pas = await _open_pas(sqlite, base)
    try:
        await pas.put(
            persona_id,
            "reference",
            label,
            bytes_=_png(),
            mime="image/png",
            file_name=f"{label}.png",
        )
    finally:
        await pas.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_paths(tmp_path: Path):
    """Return (persona_db, asset_sqlite, asset_base, data_dir) tuple."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return (
        data_dir / "personas.sqlite",
        data_dir / "persona_assets.sqlite",
        data_dir / "personas",
        data_dir,
    )


# ---------------------------------------------------------------------------
# Happy-path round-trip
# ---------------------------------------------------------------------------


def test_export_import_round_trip(tmp_path: Path, db_paths) -> None:
    """Export then import reproduces the persona row + assets faithfully."""
    persona_db, asset_sqlite, asset_base, data_dir = db_paths
    out_dir = tmp_path / "export"
    import_dir = tmp_path / "import_target"

    # Setup: create a custom persona + one reference image asset.
    asyncio.run(_insert_custom(persona_db, "vivian", "Vivian"))
    asyncio.run(_add_asset(asset_sqlite, asset_base, "vivian", "front"))

    # Export.
    result = _RUNNER.invoke(
        cli,
        [
            "persona-export",
            "--id", "vivian",
            "--out", str(out_dir),
            "--persona-db", str(persona_db),
            "--data-dir", str(data_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    bundle = out_dir / "vivian"
    assert (bundle / "persona.json").is_file()
    assert (bundle / "assets" / "reference" / "front.png").is_file()
    assert (bundle / "assets" / "reference" / "front.meta.json").is_file()

    # Import into a fresh store.
    import_db = import_dir / "personas.sqlite"
    import_asset_sqlite = import_dir / "persona_assets.sqlite"
    import_asset_base = import_dir / "personas"
    import_dir.mkdir(parents=True, exist_ok=True)

    result2 = _RUNNER.invoke(
        cli,
        [
            "persona-import",
            "--in", str(out_dir),
            "--persona-db", str(import_db),
            "--data-dir", str(import_dir),
        ],
    )
    assert result2.exit_code == 0, result2.output

    # Verify the persona record.
    async def _check() -> tuple[Persona | None, list]:
        ps = await PersonaStore.open(import_db)
        try:
            p = await ps.get("vivian")
        finally:
            await ps.close()
        pas = await PersonaAssetStore.open(import_asset_sqlite, import_asset_base)
        try:
            assets = await pas.list("vivian")
        finally:
            await pas.close()
        return p, assets

    persona, assets = asyncio.run(_check())
    assert persona is not None
    assert persona.display_name == "Vivian"
    assert persona.system_prompt == "You are a test persona."
    assert persona.is_builtin is False
    assert len(assets) == 1
    asset = assets[0]
    assert asset.kind == "reference"
    assert asset.label == "front"
    assert asset.mime == "image/png"
    # sha256 must match the original bytes.
    expected_sha = hashlib.sha256(_png()).hexdigest()
    assert asset.sha256 == expected_sha


# ---------------------------------------------------------------------------
# --all-custom skips builtins
# ---------------------------------------------------------------------------


def test_all_custom_skips_builtins(tmp_path: Path, db_paths) -> None:
    persona_db, _asset_sqlite, _asset_base, data_dir = db_paths
    out_dir = tmp_path / "export"

    # Seed a builtin AND create a custom persona.
    async def _setup() -> None:
        ps = await PersonaStore.open(persona_db)
        await seed_builtin_personas(ps)
        await ps.close()

    asyncio.run(_setup())
    asyncio.run(_insert_custom(persona_db, "lycaon", "Lycaon"))

    result = _RUNNER.invoke(
        cli,
        [
            "persona-export",
            "--all-custom",
            "--out", str(out_dir),
            "--persona-db", str(persona_db),
            "--data-dir", str(data_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    exported = {d.name for d in out_dir.iterdir() if d.is_dir()}
    # Custom persona must be in the export, builtin must NOT be.
    assert "lycaon" in exported
    assert "grantley" not in exported


# ---------------------------------------------------------------------------
# Import skips builtins without --overwrite
# ---------------------------------------------------------------------------


def test_import_skips_builtin_without_overwrite(tmp_path: Path, db_paths) -> None:
    persona_db, _asset_sqlite, _asset_base, data_dir = db_paths
    out_dir = tmp_path / "export"

    # Seed the builtin grantley locally.
    async def _setup() -> None:
        ps = await PersonaStore.open(persona_db)
        await seed_builtin_personas(ps)
        await ps.close()

    asyncio.run(_setup())

    # Manually craft a bundle that claims grantley is NOT a builtin so
    # export would include it, but has is_builtin=True in the json.
    bundle = out_dir / "grantley"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "persona.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "id": "grantley",
                "display_name": "Grantley Override",
                "short_summary": "overridden",
                "system_prompt": "overridden body",
                "is_builtin": True,
            }
        ),
        encoding="utf-8",
    )

    # Import into the same DB that already has grantley as builtin.
    result = _RUNNER.invoke(
        cli,
        [
            "persona-import",
            "--in", str(out_dir),
            "--persona-db", str(persona_db),
            "--data-dir", str(data_dir),
        ],
    )
    # Should fail (skipped builtin → errors path).
    assert result.exit_code != 0

    # Verify the row was NOT overwritten.
    async def _check() -> Persona | None:
        ps = await PersonaStore.open(persona_db)
        try:
            return await ps.get("grantley")
        finally:
            await ps.close()

    persona = asyncio.run(_check())
    assert persona is not None
    assert persona.display_name != "Grantley Override"


# ---------------------------------------------------------------------------
# Import --overwrite updates existing row
# ---------------------------------------------------------------------------


def test_import_overwrite_updates_existing(tmp_path: Path, db_paths) -> None:
    persona_db, _, __, data_dir = db_paths
    out_dir = tmp_path / "export"

    asyncio.run(_insert_custom(persona_db, "vivian", "Vivian Original"))

    # Bundle with updated display name.
    bundle = out_dir / "vivian"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "persona.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "id": "vivian",
                "display_name": "Vivian Updated",
                "short_summary": "updated summary",
                "system_prompt": "Updated body.",
                "is_builtin": False,
            }
        ),
        encoding="utf-8",
    )

    result = _RUNNER.invoke(
        cli,
        [
            "persona-import",
            "--in", str(out_dir),
            "--overwrite",
            "--persona-db", str(persona_db),
            "--data-dir", str(data_dir),
        ],
    )
    assert result.exit_code == 0, result.output

    async def _check() -> Persona | None:
        ps = await PersonaStore.open(persona_db)
        try:
            return await ps.get("vivian")
        finally:
            await ps.close()

    persona = asyncio.run(_check())
    assert persona is not None
    assert persona.display_name == "Vivian Updated"
    assert persona.system_prompt == "Updated body."


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_export_unknown_id_errors(tmp_path: Path, db_paths) -> None:
    persona_db, _, __, data_dir = db_paths
    out_dir = tmp_path / "export"

    # Create an empty DB (no personas).
    asyncio.run(_open_ps(persona_db)).close  # noqa: B018
    async def _touch() -> None:
        ps = await PersonaStore.open(persona_db)
        await ps.close()

    asyncio.run(_touch())

    result = _RUNNER.invoke(
        cli,
        [
            "persona-export",
            "--id", "nonexistent-xyz",
            "--out", str(out_dir),
            "--persona-db", str(persona_db),
            "--data-dir", str(data_dir),
        ],
    )
    assert result.exit_code != 0


def test_export_no_custom_personas_is_ok(tmp_path: Path, db_paths) -> None:
    """``--all-custom`` on a store with only builtins should exit 0."""
    persona_db, _, __, data_dir = db_paths
    out_dir = tmp_path / "export"

    async def _seed() -> None:
        ps = await PersonaStore.open(persona_db)
        await seed_builtin_personas(ps)
        await ps.close()

    asyncio.run(_seed())

    result = _RUNNER.invoke(
        cli,
        [
            "persona-export",
            "--all-custom",
            "--out", str(out_dir),
            "--persona-db", str(persona_db),
            "--data-dir", str(data_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "nothing" in result.output.lower() or "no custom" in result.output.lower()


def test_import_empty_dir_errors(tmp_path: Path, db_paths) -> None:
    persona_db, _, __, data_dir = db_paths
    empty = tmp_path / "empty"
    empty.mkdir()

    result = _RUNNER.invoke(
        cli,
        [
            "persona-import",
            "--in", str(empty),
            "--persona-db", str(persona_db),
            "--data-dir", str(data_dir),
        ],
    )
    assert result.exit_code != 0
    assert "no persona bundles" in result.output.lower() or "error" in result.output.lower()


def test_import_bundle_missing_persona_json_errors(tmp_path: Path, db_paths) -> None:
    persona_db, _, __, data_dir = db_paths
    broken = tmp_path / "broken_bundles" / "mypersona"
    broken.mkdir(parents=True, exist_ok=True)
    # No persona.json — the bundle directory exists but is empty.

    result = _RUNNER.invoke(
        cli,
        [
            "persona-import",
            "--in", str(tmp_path / "broken_bundles"),
            "--persona-db", str(persona_db),
            "--data-dir", str(data_dir),
        ],
    )
    assert result.exit_code != 0


@pytest.mark.parametrize("evil_id", ["../escape", "a/b", "..", "foo/../bar"])
def test_import_rejects_path_traversal_id(
    tmp_path: Path, db_paths, evil_id: str
) -> None:
    """A bundle whose id contains path separators / dot segments is
    rejected before any row is created or asset blob is written."""
    persona_db, _, __, data_dir = db_paths
    out_dir = tmp_path / "export"

    # The on-disk bundle dir name is benign; the *id inside* persona.json is
    # the attacker-controlled value that would escape the asset tree.
    bundle = out_dir / "benign"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "persona.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "id": evil_id,
                "display_name": "Evil",
                "short_summary": "evil",
                "system_prompt": "evil body",
                "is_builtin": False,
            }
        ),
        encoding="utf-8",
    )

    result = _RUNNER.invoke(
        cli,
        [
            "persona-import",
            "--in", str(out_dir),
            "--persona-db", str(persona_db),
            "--data-dir", str(data_dir),
        ],
    )
    # Must error out with a clear slug message …
    assert result.exit_code != 0
    assert "slug" in result.output.lower()
    # … and must NOT have created any persona row for the crafted id.

    async def _check() -> Persona | None:
        ps = await PersonaStore.open(persona_db)
        try:
            return await ps.get(evil_id)
        finally:
            await ps.close()

    assert asyncio.run(_check()) is None


def test_import_skips_existing_custom_without_overwrite(
    tmp_path: Path, db_paths
) -> None:
    """Importing over an existing *custom* persona without --overwrite skips
    (mirrors the builtin guard) and leaves the row untouched."""
    persona_db, _, __, data_dir = db_paths
    out_dir = tmp_path / "export"

    asyncio.run(_insert_custom(persona_db, "vivian", "Vivian Original"))

    bundle = out_dir / "vivian"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "persona.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "id": "vivian",
                "display_name": "Vivian Updated",
                "short_summary": "updated summary",
                "system_prompt": "Updated body.",
                "is_builtin": False,
            }
        ),
        encoding="utf-8",
    )

    # No --overwrite → should skip, exit nonzero, and not mutate the row.
    result = _RUNNER.invoke(
        cli,
        [
            "persona-import",
            "--in", str(out_dir),
            "--persona-db", str(persona_db),
            "--data-dir", str(data_dir),
        ],
    )
    assert result.exit_code != 0
    assert "skip" in result.output.lower()

    async def _check() -> Persona | None:
        ps = await PersonaStore.open(persona_db)
        try:
            return await ps.get("vivian")
        finally:
            await ps.close()

    persona = asyncio.run(_check())
    assert persona is not None
    assert persona.display_name == "Vivian Original"

    # With --overwrite the same bundle now updates the row.
    result2 = _RUNNER.invoke(
        cli,
        [
            "persona-import",
            "--in", str(out_dir),
            "--overwrite",
            "--persona-db", str(persona_db),
            "--data-dir", str(data_dir),
        ],
    )
    assert result2.exit_code == 0, result2.output

    persona2 = asyncio.run(_check())
    assert persona2 is not None
    assert persona2.display_name == "Vivian Updated"
    assert persona2.system_prompt == "Updated body."


def test_reexport_clears_stale_files(tmp_path: Path, db_paths) -> None:
    """Re-exporting into a dir that holds files from a prior export removes
    the stale files so a later import can't resurrect deleted data."""
    persona_db, asset_sqlite, asset_base, data_dir = db_paths
    out_dir = tmp_path / "export"

    asyncio.run(_insert_custom(persona_db, "vivian", "Vivian"))
    asyncio.run(_add_asset(asset_sqlite, asset_base, "vivian", "front"))

    def _export() -> None:
        result = _RUNNER.invoke(
            cli,
            [
                "persona-export",
                "--id", "vivian",
                "--out", str(out_dir),
                "--persona-db", str(persona_db),
                "--data-dir", str(data_dir),
            ],
        )
        assert result.exit_code == 0, result.output

    # First export: asset + its meta sidecar land on disk.
    _export()
    bundle = out_dir / "vivian"
    assert (bundle / "assets" / "reference" / "front.png").is_file()
    assert (bundle / "assets" / "reference" / "front.meta.json").is_file()

    # Drop the asset, then re-export.
    async def _delete_asset() -> None:
        pas = await PersonaAssetStore.open(asset_sqlite, asset_base)
        try:
            await pas.delete("vivian", "reference", "front")
        finally:
            await pas.close()

    asyncio.run(_delete_asset())
    _export()

    # The stale blob + meta from the first export must be gone — no resurrection.
    assert not (bundle / "assets" / "reference" / "front.png").exists()
    assert not (bundle / "assets" / "reference" / "front.meta.json").exists()
    # persona.json itself is rewritten and still present.
    assert (bundle / "persona.json").is_file()


def test_export_import_round_trips_life_seeds(tmp_path: Path, db_paths) -> None:
    """A persona's life-seeds override file survives export -> import."""
    from corlinman_agent.persona.life import _override_seed_path

    persona_db, _asset_sqlite, _asset_base, data_dir = db_paths
    out_dir = tmp_path / "export"
    import_dir = tmp_path / "import_target"
    import_dir.mkdir(parents=True, exist_ok=True)

    asyncio.run(_insert_custom(persona_db, "vivian", "Vivian"))
    seed_yaml = "academy_scene:\n  - 在天台钓鱼\n  - 训练场加练\n"
    src_seed = _override_seed_path("vivian", data_dir)
    src_seed.parent.mkdir(parents=True, exist_ok=True)
    src_seed.write_text(seed_yaml, encoding="utf-8")

    result = _RUNNER.invoke(
        cli,
        [
            "persona-export",
            "--id", "vivian",
            "--out", str(out_dir),
            "--persona-db", str(persona_db),
            "--data-dir", str(data_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (out_dir / "vivian" / "life_seeds.yaml").is_file()

    import_db = import_dir / "personas.sqlite"
    result2 = _RUNNER.invoke(
        cli,
        [
            "persona-import",
            "--in", str(out_dir),
            "--persona-db", str(import_db),
            "--data-dir", str(import_dir),
        ],
    )
    assert result2.exit_code == 0, result2.output
    restored = _override_seed_path("vivian", import_dir)
    assert restored.is_file()
    assert restored.read_text(encoding="utf-8") == seed_yaml
