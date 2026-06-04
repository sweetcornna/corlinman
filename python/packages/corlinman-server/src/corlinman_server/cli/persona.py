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


__all__ = ["migrate_persona_body"]
