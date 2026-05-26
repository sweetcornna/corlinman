"""First-boot starter-skill seeding.

A freshly-installed corlinman gateway has no skills in
``<data_dir>/profiles/default/skills/`` — the registry would be empty
and the agent would have no documented procedural knowledge to lean
on. To make the out-of-the-box experience hermes-like ("configure one
model, everything else just works"), the gateway ships a curated
bundle of starter ``SKILL.md`` files under
:mod:`corlinman_server.bundled_skills` and copies them into the default
profile's skills directory the first time the profile is created.

The bundle source is resolved in this order:

1. ``CORLINMAN_BUNDLED_SKILLS_DIR`` environment variable — full
   override, lets operators ship a private starter set without
   forking the package.
2. ``importlib.resources.files("corlinman_server.bundled_skills")`` —
   the in-wheel location, the normal case for installed deployments.

If neither resolves to an existing directory, seeding is a quiet
no-op (the gateway still boots; the operator can drop SKILL.md files
into the profile manually).

The copy step is **idempotent**: any ``*.md`` already present in the
target directory wins. That way an operator who hand-edited
``MEMORY.md`` worth of skill body never has it silently overwritten on
the next boot.

Two on-disk layouts are supported under the bundle root:

* **Flat** — ``<bundle>/<name>.md`` — copied to ``<target>/<name>.md``.
* **Nested** — ``<bundle>/<name>/SKILL.md`` (plus arbitrary siblings
  like ``references/``, ``scripts/``, ``.usage.json``) — copied as a
  whole subtree to ``<target>/<name>/``. The corlinman skills
  registry walks ``*.md`` recursively, so either layout shows up as a
  registered skill. The nested form is reserved for skills that ship
  scripts or asset bundles alongside the SKILL.md body.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SeedReport:
    """Outcome of one :func:`seed_starter_skills` call.

    ``copied`` is the list of skill filenames that were freshly written
    into ``target_dir``. ``skipped`` is the list that already existed
    (left untouched). ``source`` records which bundled root the copy
    came from — handy for log lines so operators can tell whether the
    in-wheel default or the ``CORLINMAN_BUNDLED_SKILLS_DIR`` override
    was used.
    """

    source: Path | None
    target: Path
    copied: tuple[str, ...]
    skipped: tuple[str, ...]


def _resolve_from_env() -> Path | None:
    """Honour the ``CORLINMAN_BUNDLED_SKILLS_DIR`` override.

    Empty / unset / whitespace-only values are treated the same — they
    do not match any path on disk, so we return ``None`` and let the
    next strategy run.
    """
    raw = os.environ.get("CORLINMAN_BUNDLED_SKILLS_DIR", "").strip()
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_dir():
        logger.warning(
            "starter_skills.env_dir_missing",
            path=str(candidate),
        )
        return None
    return candidate


def _resolve_from_package() -> Path | None:
    """Locate the in-wheel bundle via ``importlib.resources``.

    For editable installs and zipped wheels alike, ``files(...)``
    returns a ``Traversable`` we can materialise to a ``Path`` with
    :func:`importlib.resources.as_file`. We pin the path and immediately
    drop the context manager — the directory always lives on disk for
    the duration of the gateway process when corlinman is installed in
    its normal hatch layout.
    """
    try:
        traversable = files("corlinman_server.bundled_skills")
    except (ModuleNotFoundError, FileNotFoundError, TypeError):
        return None
    try:
        with as_file(traversable) as p:
            path = Path(p)
    except (FileNotFoundError, OSError):
        return None
    if not path.is_dir():
        return None
    return path


def bundled_skills_root() -> Path | None:
    """Resolve the starter-skill source directory or ``None``.

    Tries the env-var override first, then the in-wheel package data.
    Returns ``None`` if neither resolves to an existing directory — the
    caller treats that as "skip seeding" rather than as an error.
    """
    return _resolve_from_env() or _resolve_from_package()


def seed_starter_skills(target_dir: Path) -> SeedReport:
    """Copy every bundled ``*.md`` into ``target_dir`` if absent.

    Creates ``target_dir`` if it doesn't exist yet. Files already
    present in the target are left untouched and reported under
    ``skipped`` — never overwritten — so operator edits stick across
    reboots and a partial first-boot crash can be re-run safely.

    Returns a :class:`SeedReport` for logging / tests. A missing
    bundled source (``bundled_skills_root() is None``) yields an empty
    report with ``source=None`` and is **not** an error.
    """
    target = Path(target_dir)
    source = bundled_skills_root()
    if source is None:
        logger.info(
            "starter_skills.no_bundle_source",
            target=str(target),
        )
        return SeedReport(source=None, target=target, copied=(), skipped=())

    target.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    skipped: list[str] = []

    # ``sorted`` keeps the log output deterministic across platforms
    # (Linux/macOS readdir order differs), so CI diffs stay clean.
    for src_path in sorted(source.glob("*.md")):
        if not src_path.is_file():
            continue
        dst_path = target / src_path.name
        if dst_path.exists():
            skipped.append(src_path.name)
            continue
        try:
            shutil.copyfile(src_path, dst_path)
        except OSError as exc:  # pragma: no cover — defensive
            logger.warning(
                "starter_skills.copy_failed",
                src=str(src_path),
                dst=str(dst_path),
                error=str(exc),
            )
            continue
        copied.append(src_path.name)

    # Nested-layout subtree copy. We accept any subdirectory that
    # contains a ``SKILL.md`` — that's the hermes / corlinman convention
    # for skills shipping scripts or asset bundles. The whole subtree is
    # copied so siblings (``references/``, ``scripts/``, ``.usage.json``)
    # ride along; partial copies would leave the SKILL.md body referring
    # to missing files.
    for src_dir in sorted(p for p in source.iterdir() if p.is_dir()):
        # Skip dunder dirs (``__pycache__`` etc.) — they're not skills.
        if src_dir.name.startswith("_"):
            continue
        skill_md = src_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        dst_dir = target / src_dir.name
        if dst_dir.exists():
            skipped.append(src_dir.name)
            continue
        try:
            shutil.copytree(
                src_dir,
                dst_dir,
                ignore=shutil.ignore_patterns(
                    "__pycache__", "*.pyc", ".usage.json"
                ),
            )
        except OSError as exc:  # pragma: no cover — defensive
            logger.warning(
                "starter_skills.copy_failed",
                src=str(src_dir),
                dst=str(dst_dir),
                error=str(exc),
            )
            continue
        copied.append(src_dir.name)

    logger.info(
        "starter_skills.seeded",
        source=str(source),
        target=str(target),
        copied=len(copied),
        skipped=len(skipped),
    )
    return SeedReport(
        source=source,
        target=target,
        copied=tuple(copied),
        skipped=tuple(skipped),
    )


# ---------------------------------------------------------------------------
# W6 — bundled persona templates seeding
# ---------------------------------------------------------------------------
#
# Mirrors the starter-skills story above: a curated set of persona
# directories ships under ``corlinman_server.bundled_personas`` (today
# just ``grantley/daily_job.json``) and gets copied to
# ``<DATA_DIR>/bundled_personas/`` on first boot so operators can hand
# inspect / edit the templates without re-installing the wheel.
#
# Key contract: this seeder copies template **files** only — it does
# NOT register the embedded daily-publish jobs into the live scheduler.
# A fresh deploy must not start posting to QZone the second it boots;
# activation goes through ``POST /admin/scheduler/qzone/templates/
# grantley/enable`` which reads the seeded JSON and registers the job.


def _resolve_personas_from_env() -> Path | None:
    """Honour the ``CORLINMAN_BUNDLED_PERSONAS_DIR`` override.

    Empty / unset / whitespace-only values are treated the same — they
    do not match any path on disk, so we return ``None`` and let the
    next strategy run. Mirrors :func:`_resolve_from_env` for the
    starter-skills bundle.
    """
    raw = os.environ.get("CORLINMAN_BUNDLED_PERSONAS_DIR", "").strip()
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_dir():
        logger.warning(
            "bundled_personas.env_dir_missing",
            path=str(candidate),
        )
        return None
    return candidate


def _resolve_personas_from_package() -> Path | None:
    """Locate the in-wheel bundle via ``importlib.resources``.

    Mirrors :func:`_resolve_from_package` for the starter-skills
    bundle; the only difference is the package name we resolve.
    """
    try:
        traversable = files("corlinman_server.bundled_personas")
    except (ModuleNotFoundError, FileNotFoundError, TypeError):
        return None
    try:
        with as_file(traversable) as p:
            path = Path(p)
    except (FileNotFoundError, OSError):
        return None
    if not path.is_dir():
        return None
    return path


def bundled_personas_root() -> Path | None:
    """Resolve the bundled-persona source directory or ``None``.

    Tries the env-var override first, then the in-wheel package data.
    Returns ``None`` if neither resolves to an existing directory — the
    caller treats that as "skip seeding" rather than as an error.
    """
    return _resolve_personas_from_env() or _resolve_personas_from_package()


def seed_bundled_personas(target_dir: Path) -> SeedReport:
    """Recursively copy bundled persona subdirs into ``target_dir``.

    Each subdirectory of the bundle (skipping dunder dirs) is copied
    whole — that includes ``daily_job.json``, any future
    ``SYSTEM_PROMPT.md`` body, and any ``assets/`` payload. The
    existing-target check happens at the subdirectory level: if
    ``<target>/<persona_id>/`` already exists we leave it untouched
    so operator edits stick across reboots. A fresh
    ``<persona_id>`` directory in the bundle (a new persona shipped
    with the next gateway release) lands on the next boot.

    The seeder uses :func:`shutil.copytree` so siblings of
    ``daily_job.json`` ride along — partial copies that strand a
    referenced asset would silently break the template.

    Returns a :class:`SeedReport` for logging / tests. A missing
    bundled source (``bundled_personas_root() is None``) yields an
    empty report with ``source=None`` and is **not** an error.
    """
    target = Path(target_dir)
    source = bundled_personas_root()
    if source is None:
        logger.info(
            "bundled_personas.no_bundle_source",
            target=str(target),
        )
        return SeedReport(source=None, target=target, copied=(), skipped=())

    target.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    skipped: list[str] = []

    for src_dir in sorted(p for p in source.iterdir() if p.is_dir()):
        # Skip dunder dirs (``__pycache__`` etc.) — they're not personas.
        if src_dir.name.startswith("_"):
            continue
        dst_dir = target / src_dir.name
        if dst_dir.exists():
            skipped.append(src_dir.name)
            continue
        try:
            shutil.copytree(
                src_dir,
                dst_dir,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        except OSError as exc:  # pragma: no cover — defensive
            logger.warning(
                "bundled_personas.copy_failed",
                src=str(src_dir),
                dst=str(dst_dir),
                error=str(exc),
            )
            continue
        copied.append(src_dir.name)

    logger.info(
        "bundled_personas.seeded",
        source=str(source),
        target=str(target),
        copied=len(copied),
        skipped=len(skipped),
    )
    return SeedReport(
        source=source,
        target=target,
        copied=tuple(copied),
        skipped=tuple(skipped),
    )


__all__ = [
    "SeedReport",
    "bundled_personas_root",
    "bundled_skills_root",
    "seed_bundled_personas",
    "seed_starter_skills",
]
