"""Portable bundle (de)serialise helpers for ``persona-export`` /
``persona-import``.

Bundle layout on disk (one directory per persona)::

    <OUT_DIR>/
      <persona_id>/
        persona.json          — display_name / short_summary / system_prompt / is_builtin
        life_seeds.yaml       — operator override events file (optional)
        assets/
          <kind>/
            <label>.<ext>     — blob file
            <label>.meta.json — AssetRecord fields (kind / label / file_name / mime / sha256)

The ``persona.json`` carries ``format_version: 1`` so a future incompatible
change can be detected and rejected at import time.

This module contains no Click decorators — it is pure async logic that the
CLI commands call after argument parsing, making it independently testable.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from corlinman_server.persona.store import Persona

#: Bumped when the on-disk layout changes in an incompatible way.
BUNDLE_FORMAT_VERSION: int = 1


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------


def _persona_json(persona: Persona) -> dict[str, Any]:
    """Convert a :class:`Persona` to the bundle ``persona.json`` dict."""
    return {
        "format_version": BUNDLE_FORMAT_VERSION,
        "id": persona.id,
        "display_name": persona.display_name,
        "short_summary": persona.short_summary,
        "system_prompt": persona.system_prompt,
        "is_builtin": persona.is_builtin,
    }


async def export_persona(
    persona_id: str,
    *,
    out_dir: Path,
    persona_db: Path,
    asset_sqlite: Path,
    asset_base_dir: Path,
    data_dir: Path | None,
) -> int:
    """Export one persona into ``<out_dir>/<persona_id>/``.

    Returns 0 on success, 1 on error (prints the reason to stderr via
    :func:`click.echo`).
    """
    import click

    from corlinman_server.persona.asset_store import PersonaAssetStore
    from corlinman_server.persona.store import PersonaStore

    # ---- open stores ----
    if not persona_db.exists():
        click.echo(
            f"error: persona database not found at {persona_db}.\n"
            "  Run `corlinman onboard` or start the server once first.",
            err=True,
        )
        return 1

    ps = await PersonaStore.open(persona_db)
    try:
        persona = await ps.get(persona_id)
    finally:
        await ps.close()

    if persona is None:
        click.echo(
            f"error: persona '{persona_id}' not found in {persona_db}.",
            err=True,
        )
        return 1

    # ---- bundle dir ----
    # Start from a clean directory so stale blobs / .meta.json / life_seeds
    # from a prior export can't resurrect deleted data on a later import.
    bundle = out_dir / persona_id
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True, exist_ok=True)

    # ---- persona.json ----
    pj = bundle / "persona.json"
    pj.write_text(
        json.dumps(_persona_json(persona), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # ---- life-seeds override ----
    if data_dir is not None:
        try:
            from corlinman_agent.persona.life import (  # type: ignore[import-untyped]
                _override_seed_path,
                _valid_persona_slug,
            )
        except ImportError:
            pass
        else:
            if _valid_persona_slug(persona_id):
                src = _override_seed_path(persona_id, data_dir)
                if src.is_file():
                    dst = bundle / "life_seeds.yaml"
                    dst.write_bytes(src.read_bytes())
                    click.echo(f"  life-seeds override: {dst.name}")

    # ---- assets ----
    if asset_sqlite.exists():
        pas = await PersonaAssetStore.open(asset_sqlite, asset_base_dir)
        try:
            records = await pas.list(persona_id)
        finally:
            await pas.close()

        if records:
            for rec in records:
                kind_dir = bundle / "assets" / rec.kind
                kind_dir.mkdir(parents=True, exist_ok=True)
                # derive extension from mime
                ext = {
                    "image/png": "png",
                    "image/jpeg": "jpg",
                    "image/webp": "webp",
                    "image/gif": "gif",
                }.get(rec.mime, "bin")
                blob_src = pas.path_for(rec)
                if not blob_src.is_file():
                    click.echo(
                        f"  warning: blob missing for asset '{rec.label}' "
                        f"({rec.kind}), skipping.",
                        err=True,
                    )
                    continue
                # Copy blob
                blob_dst = kind_dir / f"{rec.label}.{ext}"
                blob_dst.write_bytes(blob_src.read_bytes())
                # Write metadata sidecar
                meta: dict[str, Any] = {
                    "kind": rec.kind,
                    "label": rec.label,
                    "file_name": rec.file_name,
                    "mime": rec.mime,
                    "sha256": rec.sha256,
                    "size_bytes": rec.size_bytes,
                }
                meta_dst = kind_dir / f"{rec.label}.meta.json"
                meta_dst.write_text(
                    json.dumps(meta, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            click.echo(f"  assets: {len(records)} file(s) exported")

    click.echo(f"persona '{persona_id}' exported → {bundle}")
    return 0


# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------


def _load_bundle(bundle: Path) -> dict[str, Any] | None:
    """Load and validate ``bundle/persona.json``. Returns ``None`` on
    error (prints to stderr)."""
    import click

    pj = bundle / "persona.json"
    if not pj.is_file():
        click.echo(
            f"error: bundle is missing persona.json: {bundle}", err=True
        )
        return None
    try:
        data: dict[str, Any] = json.loads(pj.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        click.echo(f"error: cannot read persona.json: {exc}", err=True)
        return None
    version = data.get("format_version", 0)
    if version != BUNDLE_FORMAT_VERSION:
        click.echo(
            f"error: unsupported bundle format_version {version!r} "
            f"(expected {BUNDLE_FORMAT_VERSION})",
            err=True,
        )
        return None
    for field in ("id", "display_name", "short_summary", "system_prompt"):
        if field not in data:
            click.echo(
                f"error: persona.json missing required field '{field}'",
                err=True,
            )
            return None
    return data


async def import_persona(
    bundle: Path,
    *,
    persona_db: Path,
    asset_sqlite: Path,
    asset_base_dir: Path,
    data_dir: Path | None,
    overwrite: bool,
) -> int:
    """Import one persona from a bundle directory.

    Returns 0 on success, 1 on error.
    """
    import click

    from corlinman_server.persona.asset_store import PersonaAssetStore
    from corlinman_server.persona.store import Persona, PersonaError, PersonaExists, PersonaStore

    data = _load_bundle(bundle)
    if data is None:
        return 1

    persona_id: str = data["id"]

    # ---- slug guard ----
    # The id is interpolated into the persona row AND asset blob paths
    # (<asset_base>/<id>/<kind>/<sha>). A crafted id with path separators or
    # dot segments would escape the asset tree, so reject it up front before
    # any create / asset-restore happens.
    from corlinman_agent.persona.life import (  # type: ignore[import-untyped]
        _valid_persona_slug,
    )

    if not _valid_persona_slug(persona_id):
        click.echo(
            f"error: bundle persona id {persona_id!r} is not a valid slug "
            "([a-z0-9_-]); refusing to import.",
            err=True,
        )
        return 1

    # ---- open persona store ----
    ps = await PersonaStore.open(persona_db)
    try:
        existing = await ps.get(persona_id)

        if existing is not None and existing.is_builtin and not overwrite:
            click.echo(
                f"skip: '{persona_id}' is a builtin row — use --overwrite "
                "to force.",
                err=True,
            )
            return 1

        if existing is not None and not existing.is_builtin and not overwrite:
            click.echo(
                f"skip: '{persona_id}' already exists — use --overwrite "
                "to replace it.",
                err=True,
            )
            return 1

        import time as _time

        now = int(_time.time() * 1000)

        if existing is None:
            candidate = Persona(
                id=persona_id,
                display_name=data["display_name"],
                short_summary=data["short_summary"],
                system_prompt=data["system_prompt"],
                is_builtin=False,  # create() enforces non-builtin
                created_at_ms=now,
                updated_at_ms=now,
            )
            try:
                await ps.create(candidate)
                click.echo(f"persona '{persona_id}' created.")
            except PersonaExists:
                # Race: another process inserted between our get() and create().
                click.echo(
                    f"error: persona '{persona_id}' appeared concurrently.",
                    err=True,
                )
                return 1
        else:
            # Update the mutable fields; is_builtin is preserved by update().
            try:
                await ps.update(
                    persona_id,
                    display_name=data["display_name"],
                    short_summary=data["short_summary"],
                    system_prompt=data["system_prompt"],
                )
                click.echo(f"persona '{persona_id}' updated (overwrite).")
            except PersonaError as exc:
                click.echo(
                    f"error: could not update persona '{persona_id}': {exc}",
                    err=True,
                )
                return 1
    finally:
        await ps.close()

    # ---- life-seeds override ----
    seeds_file = bundle / "life_seeds.yaml"
    if seeds_file.is_file():
        if data_dir is None:
            click.echo(
                "  warning: no data dir — life_seeds.yaml not restored.",
                err=True,
            )
        else:
            try:
                from corlinman_agent.persona.life import (  # type: ignore[import-untyped]
                    _override_seed_path,
                    _valid_persona_slug,
                )
            except ImportError:
                click.echo(
                    "  warning: corlinman_agent not available — "
                    "life_seeds.yaml not restored.",
                    err=True,
                )
            else:
                if _valid_persona_slug(persona_id):
                    dst = _override_seed_path(persona_id, data_dir)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dst.with_suffix(".yaml.tmp")
                    tmp.write_bytes(seeds_file.read_bytes())
                    tmp.replace(dst)
                    click.echo(f"  life-seeds override restored → {dst}")

    # ---- assets ----
    assets_dir = bundle / "assets"
    if assets_dir.is_dir():
        pas = await PersonaAssetStore.open(asset_sqlite, asset_base_dir)
        try:
            imported = 0
            for kind_dir in sorted(assets_dir.iterdir()):
                kind = kind_dir.name
                if kind not in ("emoji", "reference"):
                    continue
                for meta_file in sorted(kind_dir.glob("*.meta.json")):
                    try:
                        meta: dict[str, Any] = json.loads(
                            meta_file.read_text(encoding="utf-8")
                        )
                    except (json.JSONDecodeError, OSError) as exc:
                        click.echo(
                            f"  warning: cannot read {meta_file.name}: {exc}",
                            err=True,
                        )
                        continue
                    label = meta.get("label", meta_file.stem)
                    mime = meta.get("mime", "image/png")
                    file_name = meta.get("file_name", meta_file.stem)
                    # Derive expected blob filename
                    ext = {
                        "image/png": "png",
                        "image/jpeg": "jpg",
                        "image/webp": "webp",
                        "image/gif": "gif",
                    }.get(mime, "bin")
                    blob_path = kind_dir / f"{label}.{ext}"
                    if not blob_path.is_file():
                        click.echo(
                            f"  warning: blob missing for '{label}' ({kind}), "
                            "skipping.",
                            err=True,
                        )
                        continue
                    blob_bytes = blob_path.read_bytes()
                    # Verify sha256 if present in meta
                    expected_sha = meta.get("sha256")
                    if expected_sha:
                        actual_sha = hashlib.sha256(blob_bytes).hexdigest()
                        if actual_sha != expected_sha:
                            click.echo(
                                f"  warning: sha256 mismatch for '{label}' "
                                f"({kind}), skipping (expected {expected_sha[:8]}…, "
                                f"got {actual_sha[:8]}…).",
                                err=True,
                            )
                            continue
                    try:

                        await pas.put(
                            persona_id,
                            kind,  # type: ignore[arg-type]
                            label,
                            bytes_=blob_bytes,
                            mime=mime,
                            file_name=file_name,
                        )
                        imported += 1
                    except Exception as exc:  # noqa: BLE001
                        click.echo(
                            f"  warning: could not import asset '{label}' "
                            f"({kind}): {exc}",
                            err=True,
                        )
        finally:
            await pas.close()
        if imported:
            click.echo(f"  assets: {imported} file(s) imported")

    click.echo(f"persona '{persona_id}' import complete.")
    return 0


__all__ = [
    "BUNDLE_FORMAT_VERSION",
    "export_persona",
    "import_persona",
]
