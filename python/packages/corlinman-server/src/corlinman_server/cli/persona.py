"""``corlinman migrate-persona-body`` — OPERATOR-INVOKED migration that
refreshes a built-in persona's body from the bundled defaults.

Why this exists
---------------
:func:`~corlinman_server.persona.store.seed_builtin_personas` is
idempotent and *skips* existing rows so operator customisations persist
across reboots.  The downside is that a production deployment that was
set up before the persona body gained a new block (e.g. the
``## 此刻的我（实时状态）`` live-state section) will never pick it up
automatically.

This command fills that gap.  It reads the on-disk bundled default
(``default_grantley.md`` + the Python constants) and applies the
changes to the matching row — but only when:

* the row is flagged ``is_builtin = 1`` (guards against accidental
  clobber of operator-created custom personas with the same id), and
* the operator has explicitly supplied ``--force`` (or inspected the
  diff via ``--dry-run`` and then re-run without ``--dry-run``).

Usage
-----

    # Show what would change (no writes):
    corlinman migrate-persona-body --dry-run

    # Apply to the default builtin (grantley):
    corlinman migrate-persona-body --force

    # Apply to a specific builtin by id:
    corlinman migrate-persona-body --id grantley --force

    # Custom persona DB path:
    corlinman migrate-persona-body --force \\
        --persona-db /opt/corlinman/data/personas.sqlite
"""

from __future__ import annotations

import asyncio
import difflib
import sys
from pathlib import Path

import click

from corlinman_server.cli._common import resolve_data_dir

# ---------------------------------------------------------------------------
# Shared path-resolution helpers (used by export + import)
# ---------------------------------------------------------------------------


def _resolve_persona_asset_sqlite(data_dir: Path) -> Path:
    """Return ``<data_dir>/persona_assets.sqlite``."""
    return data_dir / "persona_assets.sqlite"


def _resolve_persona_asset_base(data_dir: Path) -> Path:
    """Return ``<data_dir>/personas`` (blob base dir)."""
    return data_dir / "personas"

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

#: Mapping from builtin persona id → loader.  New builtins plug in here
#: so the command grows without touching the click surface.
_BUILTIN_LOADERS: dict[
    str,
    tuple[
        str,  # display_name
        str,  # short_summary
        # callable() → str  (avoids eagerly importing the module at
        # import time, matching the pattern in seed_builtin_personas)
        object,
    ],
] = {}


def _register_builtins() -> None:
    """Populate ``_BUILTIN_LOADERS`` lazily once (idempotent)."""
    if _BUILTIN_LOADERS:
        return
    from corlinman_server.persona.default_grantley import (
        DEFAULT_GRANTLEY_DISPLAY_NAME,
        DEFAULT_GRANTLEY_ID,
        DEFAULT_GRANTLEY_SUMMARY,
        load_default_grantley_body,
    )

    _BUILTIN_LOADERS[DEFAULT_GRANTLEY_ID] = (
        DEFAULT_GRANTLEY_DISPLAY_NAME,
        DEFAULT_GRANTLEY_SUMMARY,
        load_default_grantley_body,
    )


def _resolve_persona_db(data_dir: Path | None, persona_db: Path | None) -> Path:
    """Resolve the persona SQLite path.

    Priority:
    1. explicit ``--persona-db`` flag
    2. ``<data_dir>/personas.sqlite``
    """
    if persona_db is not None:
        return persona_db
    return resolve_data_dir(data_dir) / "personas.sqlite"


def _unified_diff(old: str, new: str, label: str) -> str:
    """Return a unified-diff string for ``old`` → ``new`` or an empty
    string when the content is identical."""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"current {label}",
            tofile=f"bundled {label}",
        )
    )
    return "".join(diff)


# ---------------------------------------------------------------------------
# Core async logic (separated so tests can drive it directly)
# ---------------------------------------------------------------------------


async def _run_migrate(
    *,
    persona_id: str,
    persona_db: Path,
    dry_run: bool,
    force: bool,
) -> int:
    """Perform (or preview) the migration.

    Returns an exit-code integer:
    * 0 — success / no change needed
    * 1 — error (missing row, not-builtin, etc.)
    """
    from corlinman_server.persona.store import PersonaStore

    _register_builtins()

    if persona_id not in _BUILTIN_LOADERS:
        click.echo(
            f"error: '{persona_id}' is not a known built-in persona id.  "
            f"Known ids: {', '.join(sorted(_BUILTIN_LOADERS))}",
            err=True,
        )
        return 1

    bundled_display, bundled_summary, loader = _BUILTIN_LOADERS[persona_id]
    try:
        bundled_body: str = loader()  # type: ignore[operator]
    except FileNotFoundError as exc:
        click.echo(f"error: bundled persona file missing: {exc}", err=True)
        return 1

    if not persona_db.exists():
        click.echo(
            f"error: persona database not found at {persona_db}.\n"
            "  Run `corlinman onboard` or start the server once first.",
            err=True,
        )
        return 1

    store = await PersonaStore.open(persona_db)
    try:
        existing = await store.get(persona_id)
        if existing is None:
            click.echo(
                f"error: persona '{persona_id}' does not exist in {persona_db}.\n"
                "  Run `corlinman onboard` or start the server once to seed it.",
                err=True,
            )
            return 1

        if not existing.is_builtin:
            click.echo(
                f"error: persona '{persona_id}' is not flagged is_builtin in the "
                "database.  This command only updates builtin rows to prevent "
                "accidental clobber of operator-created personas.",
                err=True,
            )
            return 1

        # Compute field-level diffs.
        diff_display = _unified_diff(
            existing.display_name, bundled_display, "display_name"
        )
        diff_summary = _unified_diff(
            existing.short_summary, bundled_summary, "short_summary"
        )
        diff_body = _unified_diff(
            existing.system_prompt, bundled_body, "system_prompt"
        )

        any_change = bool(diff_display or diff_summary or diff_body)

        # Always show the diff so operators can review it in both --dry-run
        # and --force modes.  In --force mode this confirms what was applied.
        if not any_change:
            click.echo(
                f"persona '{persona_id}' is already up-to-date with the bundled "
                "defaults.  Nothing to do."
            )
            return 0

        click.echo(f"persona '{persona_id}' — diff between current DB and bundled defaults:")
        click.echo("")

        if diff_display:
            click.echo("  [display_name]")
            for line in diff_display.splitlines():
                click.echo(f"  {line}")
            click.echo("")
        if diff_summary:
            click.echo("  [short_summary]")
            for line in diff_summary.splitlines():
                click.echo(f"  {line}")
            click.echo("")
        if diff_body:
            click.echo("  [system_prompt]")
            for line in diff_body.splitlines():
                click.echo(f"  {line}")
            click.echo("")

        if dry_run:
            click.echo("(dry-run — no changes written.  Re-run with --force to apply.)")
            return 0

        if not force:
            click.echo(
                "error: refusing to write without --force.  "
                "Review the diff above then re-run with --force.",
                err=True,
            )
            return 1

        # Apply via PersonaStore.update() — the public update() method allows
        # editing builtin rows (it preserves the is_builtin flag), which is
        # exactly what we need here.  We pass all three fields so even a
        # partial change (e.g. only display_name drifted) is atomically applied.
        await store.update(
            persona_id,
            display_name=bundled_display,
            short_summary=bundled_summary,
            system_prompt=bundled_body,
        )
        click.echo(
            f"persona '{persona_id}' updated from bundled defaults "
            f"(db: {persona_db})."
        )
        return 0

    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


@click.command(
    "migrate-persona-body",
    help=(
        "Refresh a built-in persona's body from the bundled defaults.\n\n"
        "Use --dry-run to inspect the diff without writing.  "
        "Use --force to actually apply the update.  "
        "Only rows flagged is_builtin=1 in the database are eligible."
    ),
)
@click.option(
    "--id",
    "persona_id",
    default="grantley",
    show_default=True,
    help="ID of the built-in persona to update.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the unified diff and exit without writing anything.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Actually apply the update (required unless --dry-run is given).",
)
@click.option(
    "--persona-db",
    "persona_db_arg",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Path to the persona SQLite database.  "
        "Defaults to <data-dir>/personas.sqlite."
    ),
)
@click.option(
    "--data-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override data directory (default: $CORLINMAN_DATA_DIR or ~/.corlinman).",
)
def migrate_persona_body(
    persona_id: str,
    dry_run: bool,
    force: bool,
    persona_db_arg: Path | None,
    data_dir: Path | None,
) -> None:
    """Refresh a built-in persona's body from the bundled defaults."""
    if dry_run and force:
        click.echo(
            "error: --dry-run and --force are mutually exclusive.",
            err=True,
        )
        sys.exit(1)

    if not dry_run and not force:
        click.echo(
            "error: specify --dry-run (preview) or --force (apply).  "
            "Run with --help for usage.",
            err=True,
        )
        sys.exit(1)

    persona_db = _resolve_persona_db(data_dir, persona_db_arg)
    exit_code = asyncio.run(
        _run_migrate(
            persona_id=persona_id,
            persona_db=persona_db,
            dry_run=dry_run,
            force=force,
        )
    )
    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# persona-export
# ---------------------------------------------------------------------------


@click.command(
    "persona-export",
    help=(
        "Export operator-created personas to portable bundles.\n\n"
        "Each persona is written as a sub-directory inside OUT_DIR:\n\n"
        "    <OUT_DIR>/<persona_id>/persona.json        — core fields\n"
        "    <OUT_DIR>/<persona_id>/life_seeds.yaml     — seed override (if any)\n"
        "    <OUT_DIR>/<persona_id>/assets/<kind>/…     — blob + metadata\n\n"
        "Default selects all non-builtin personas (--all-custom).  "
        "Pass --id to export a single persona by id."
    ),
)
@click.option(
    "--id",
    "persona_id",
    default=None,
    help="ID of the persona to export.  Mutually exclusive with --all-custom.",
)
@click.option(
    "--all-custom",
    "all_custom",
    is_flag=True,
    default=False,
    help="Export every non-builtin persona (default when --id is absent).",
)
@click.option(
    "--out",
    "out_dir",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Destination directory; created if absent.",
)
@click.option(
    "--persona-db",
    "persona_db_arg",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Path to the persona SQLite database.  "
        "Defaults to <data-dir>/personas.sqlite."
    ),
)
@click.option(
    "--data-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override data directory (default: $CORLINMAN_DATA_DIR or ~/.corlinman).",
)
def persona_export(
    persona_id: str | None,
    all_custom: bool,
    out_dir: Path,
    persona_db_arg: Path | None,
    data_dir: Path | None,
) -> None:
    """Export one or all custom personas to portable bundles."""
    if persona_id and all_custom:
        click.echo(
            "error: --id and --all-custom are mutually exclusive.", err=True
        )
        sys.exit(1)

    resolved_data = resolve_data_dir(data_dir)
    persona_db = _resolve_persona_db(resolved_data, persona_db_arg)
    asset_sqlite = _resolve_persona_asset_sqlite(resolved_data)
    asset_base = _resolve_persona_asset_base(resolved_data)

    exit_code = asyncio.run(
        _run_export(
            persona_id=persona_id,
            all_custom=(all_custom or persona_id is None),
            out_dir=out_dir,
            persona_db=persona_db,
            asset_sqlite=asset_sqlite,
            asset_base=asset_base,
            data_dir=resolved_data,
        )
    )
    sys.exit(exit_code)


async def _run_export(
    *,
    persona_id: str | None,
    all_custom: bool,
    out_dir: Path,
    persona_db: Path,
    asset_sqlite: Path,
    asset_base: Path,
    data_dir: Path,
) -> int:
    from corlinman_server.cli.persona_bundle import export_persona
    from corlinman_server.persona.store import PersonaStore

    if not persona_db.exists():
        click.echo(
            f"error: persona database not found at {persona_db}.\n"
            "  Run `corlinman onboard` or start the server once first.",
            err=True,
        )
        return 1

    ps = await PersonaStore.open(persona_db)
    try:
        all_personas = await ps.list()
    finally:
        await ps.close()

    if persona_id is not None:
        targets = [p for p in all_personas if p.id == persona_id]
        if not targets:
            click.echo(
                f"error: persona '{persona_id}' not found in {persona_db}.",
                err=True,
            )
            return 1
    else:
        # --all-custom (default): skip builtins
        targets = [p for p in all_personas if not p.is_builtin]
        if not targets:
            click.echo("no custom personas found — nothing to export.")
            return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    errors = 0
    for p in targets:
        rc = await export_persona(
            p.id,
            out_dir=out_dir,
            persona_db=persona_db,
            asset_sqlite=asset_sqlite,
            asset_base_dir=asset_base,
            data_dir=data_dir,
        )
        if rc != 0:
            errors += 1
    return 0 if errors == 0 else 1


# ---------------------------------------------------------------------------
# persona-import
# ---------------------------------------------------------------------------


@click.command(
    "persona-import",
    help=(
        "Import personas from previously-exported bundles.\n\n"
        "``IN_DIR`` must be a directory whose immediate sub-directories are "
        "persona bundle folders (each containing a ``persona.json``).\n\n"
        "Builtin personas are skipped unless --overwrite is given.  "
        "Non-builtin rows that already exist are also skipped without "
        "--overwrite."
    ),
)
@click.option(
    "--in",
    "in_dir",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Directory produced by ``persona-export``.",
)
@click.option(
    "--overwrite",
    is_flag=True,
    default=False,
    help="Overwrite any existing persona row (including builtins).",
)
@click.option(
    "--persona-db",
    "persona_db_arg",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Path to the persona SQLite database.  "
        "Defaults to <data-dir>/personas.sqlite."
    ),
)
@click.option(
    "--data-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override data directory (default: $CORLINMAN_DATA_DIR or ~/.corlinman).",
)
def persona_import(
    in_dir: Path,
    overwrite: bool,
    persona_db_arg: Path | None,
    data_dir: Path | None,
) -> None:
    """Import personas from a bundle directory into the persona store."""
    resolved_data = resolve_data_dir(data_dir)
    persona_db = _resolve_persona_db(resolved_data, persona_db_arg)
    asset_sqlite = _resolve_persona_asset_sqlite(resolved_data)
    asset_base = _resolve_persona_asset_base(resolved_data)

    exit_code = asyncio.run(
        _run_import(
            in_dir=in_dir,
            overwrite=overwrite,
            persona_db=persona_db,
            asset_sqlite=asset_sqlite,
            asset_base=asset_base,
            data_dir=resolved_data,
        )
    )
    sys.exit(exit_code)


async def _run_import(
    *,
    in_dir: Path,
    overwrite: bool,
    persona_db: Path,
    asset_sqlite: Path,
    asset_base: Path,
    data_dir: Path,
) -> int:
    from corlinman_server.cli.persona_bundle import import_persona

    # Discover bundle sub-directories (each must contain persona.json).
    bundles = sorted(
        d for d in in_dir.iterdir() if d.is_dir() and (d / "persona.json").is_file()
    )
    if not bundles:
        click.echo(
            f"error: no persona bundles found in {in_dir}.\n"
            "  A bundle is a sub-directory that contains a 'persona.json'.",
            err=True,
        )
        return 1

    errors = 0
    for bundle in bundles:
        rc = await import_persona(
            bundle,
            persona_db=persona_db,
            asset_sqlite=asset_sqlite,
            asset_base_dir=asset_base,
            data_dir=data_dir,
            overwrite=overwrite,
        )
        if rc != 0:
            errors += 1
    if errors:
        click.echo(
            f"{errors} persona(s) failed to import — see warnings above.",
            err=True,
        )
    return 0 if errors == 0 else 1


__all__ = ["migrate_persona_body", "persona_export", "persona_import"]
