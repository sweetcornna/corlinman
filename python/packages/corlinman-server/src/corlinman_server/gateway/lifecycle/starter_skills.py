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

The copy step is **idempotent** and operator-edits-win: a skill
already present in the target directory is only ever touched when its
``SKILL.md`` (or legacy flat ``<name>.md``) is byte-identical to a
known factory revision — the current bundle or one of the recorded
historical hashes in :data:`_FACTORY_SKILL_MD_SHA256`. Such
factory-pristine copies are refreshed in place to the new bundle
(including ``references/`` siblings), so existing deployments pick up
bundle upgrades like the P6 giant-skill split. Anything the operator
hand-edited never matches a factory hash and is left untouched.

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

import hashlib
import os
import shutil
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Factory-content hashes — the pristine-upgrade allowlist
# ---------------------------------------------------------------------------
#
# sha256 of every byte-exact SKILL.md revision that ever shipped on main
# for the four giant bundled skills *before* the P6 progressive-disclosure
# split (PR #184, commit 204f4da5) rewrote them as slim SKILL.md +
# references/*.md. Deployments seeded from any of these revisions never
# received the references/ subtree; matching one of these hashes (or the
# current bundle) proves the operator never edited the skill, so the
# seeder may refresh the whole directory to the new bundle.
#
# Source: computed from git history with
#   git show <commit>:python/packages/corlinman-server/src/\
#     corlinman_server/bundled_skills/<name>/SKILL.md | shasum -a 256
# over every commit that touched the file (git log -- <path>); the
# originating commits are noted inline. None of the four ever shipped in
# the legacy flat <name>.md layout.
_FACTORY_SKILL_MD_SHA256: dict[str, frozenset[str]] = {
    "configure-persona": frozenset(
        {
            # 204f4da5 (#184) — P6 progressive-disclosure split (current)
            "16b382fc01c52e6a44a7f0351a48d6f3a31a91cd6b5d8ea4444bb4ffee7e204c",
            # bf4eff76 / 4b66e4fe — last pre-split revision (#164)
            "7311db6e85bc660624d21f4348384b3908072254aa9c7417b65d91c84425a955",
            # 20aa602d — v1.7.0 onboarding wizard
            "3dd72a644b84dd52bb61c20aeb4c8518b5e0ab03805bc4376eb0c922d03decfb",
            # cfd540bd — W2 hotfix
            "06fe367850a55d641d8963895aa61b9a99b4ac761ebeb24745de6d39c5b5df98",
            # ed8ed30d — Stage 0 distill (W2)
            "98adf2eff8f895da9a05e94c557bc29afce5621bf50307dbcbfcf507fd686c53",
            # a5d36490 / 45d94406 — staged materials flow rewrite
            "b81c3024efb2ac964ea06b113dc5fa7c2f803a897fda9f90b85ed4bd07af7b75",
            # 29e691c6 — earlier wizard revision
            "3d4addfe6c5369aaeb4db49344b1cf527245356e42747bb98c2f340e9c09ab63",
            # eb23c828 — first bundled revision
            "0ea00b495cdc745149417bd59de97ab3a4cb4612a60c89aaf52f3abedbce28fa",
        }
    ),
    "darwin-skill": frozenset(
        {
            # 204f4da5 (#184) — P6 progressive-disclosure split (current)
            "514fa2e8e9b8cb36d505e7fcca1382fdb56444e8b241212aec440b0dae272cd2",
            # a5d36490 .. bf4eff76 — sole pre-split revision (W1 bundle)
            "ea1c92431f8a2b056861481f74c83413f16c7a5e2d100a9eee3f12759edc48d4",
        }
    ),
    "huashu-design": frozenset(
        {
            # 204f4da5 (#184) — P6 progressive-disclosure split (current)
            "ccad74f458f71cf25d2fe78415eea5a62cfb20a2860bd38030984abac477c703",
            # bf4eff76 — last pre-split revision (#164)
            "e56ec50de356937ce2b9a54fc5e9db43f20962dfb8970db15cd5b0926ac5e7e2",
            # a5d36490 .. 4b66e4fe — original W1 bundle revision
            "08a4e526c8a3184b18eb52134fe5d2e4a5868bc746b540323c31c9b899ba91be",
        }
    ),
    "nuwa-skill": frozenset(
        {
            # 204f4da5 (#184) — P6 progressive-disclosure split (current)
            "69e70e02d692301e0e630a00e51a739377496df9fe6df655037b937fa8cd284f",
            # bf4eff76 — last pre-split revision (#164)
            "9fd06d566ca69581ad350c05e524c81c78822092c0d281f3a49d26304d9bd7b0",
            # a5d36490 .. 4b66e4fe — original W1 bundle revision
            "120f4b19a7677d7dca9c761d5ce7dc3a15f4511daf0ed4a15ecce7ff529e51d7",
        }
    ),
}

# Per-skill usage bookkeeping written by the runtime next to SKILL.md;
# never part of the bundle, must survive a factory refresh.
_USAGE_FILENAME = ".usage.json"

_COPYTREE_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", _USAGE_FILENAME)


@dataclass(frozen=True)
class SeedReport:
    """Outcome of one :func:`seed_starter_skills` call.

    ``copied`` is the list of skill filenames that were freshly written
    into ``target_dir``. ``skipped`` is the list that already existed
    (left untouched). ``refreshed`` is the list of factory-pristine
    entries that were upgraded in place to the current bundle content.
    ``source`` records which bundled root the copy came from — handy
    for log lines so operators can tell whether the in-wheel default or
    the ``CORLINMAN_BUNDLED_SKILLS_DIR`` override was used.
    """

    source: Path | None
    target: Path
    copied: tuple[str, ...]
    skipped: tuple[str, ...]
    refreshed: tuple[str, ...] = ()


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


def _sha256_file(path: Path) -> str | None:
    """sha256 hex digest of ``path``, or ``None`` if unreadable."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _is_factory_content(name: str, bundle_md: Path, existing_md: Path) -> bool:
    """True when ``existing_md`` is byte-identical to factory content.

    Factory content is either the current bundle's file or one of the
    recorded historical revisions in :data:`_FACTORY_SKILL_MD_SHA256`.
    An unreadable / missing ``existing_md`` is *not* factory content —
    the caller decides how to treat broken layouts.
    """
    existing = _sha256_file(existing_md)
    if existing is None:
        return False
    if existing == _sha256_file(bundle_md):
        return True
    return existing in _FACTORY_SKILL_MD_SHA256.get(name, frozenset())


def _subtree_in_sync(src_dir: Path, dst_dir: Path) -> bool:
    """True when every seedable file in ``src_dir`` exists byte-identical
    in ``dst_dir``.

    Only bundle-side files are compared (extra operator files in the
    target don't break sync) and the copytree ignore set is honoured, so
    a freshly-refreshed target reports in-sync on the next boot — that's
    what keeps the refresh path idempotent instead of re-copying every
    start.
    """
    for src in src_dir.rglob("*"):
        rel = src.relative_to(src_dir)
        if any(part == "__pycache__" for part in rel.parts):
            continue
        if src.name.endswith(".pyc") or src.name == _USAGE_FILENAME:
            continue
        if src.is_dir():
            continue
        dst = dst_dir / rel
        try:
            if not dst.is_file() or src.read_bytes() != dst.read_bytes():
                return False
        except OSError:
            return False
    return True


def _only_factory_artifacts(src_dir: Path, dst_dir: Path) -> bool:
    """True when every file under ``dst_dir`` has a same-relative-path
    counterpart in the bundle (or is the runtime usage sidecar) — the
    shape a crashed partial refresh leaves behind. Any unknown file means
    operator content and forbids the destructive self-heal (review fix:
    a SKILL.md-less dir used to be rmtree'd unconditionally)."""
    try:
        for path in dst_dir.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(dst_dir)
            if rel.name == _USAGE_FILENAME:
                continue
            if not (src_dir / rel).is_file():
                return False
    except OSError:  # pragma: no cover — unreadable tree: be conservative
        return False
    return True


def seed_starter_skills(target_dir: Path) -> SeedReport:
    """Copy every bundled ``*.md`` into ``target_dir`` if absent.

    Creates ``target_dir`` if it doesn't exist yet. Files already
    present in the target follow the operator-edits-win contract: an
    entry whose ``SKILL.md`` (or flat body) still carries factory
    content — the current bundle or a recorded historical revision —
    is refreshed in place to the new bundle; anything else is reported
    under ``skipped`` and never overwritten, so operator edits stick
    across reboots and a partial first-boot crash can be re-run safely.

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
    refreshed: list[str] = []

    # ``sorted`` keeps the log output deterministic across platforms
    # (Linux/macOS readdir order differs), so CI diffs stay clean.
    for src_path in sorted(source.glob("*.md")):
        if not src_path.is_file():
            continue
        dst_path = target / src_path.name
        if dst_path.exists():
            if not _is_factory_content(src_path.stem, src_path, dst_path):
                skipped.append(src_path.name)
                continue
            if _sha256_file(dst_path) == _sha256_file(src_path):
                # Already the current bundle body — nothing to refresh.
                skipped.append(src_path.name)
                continue
            # Factory-pristine but stale — upgrade in place.
            try:
                shutil.copyfile(src_path, dst_path)
            except OSError as exc:  # pragma: no cover — defensive
                logger.warning(
                    "starter_skills.refresh_failed",
                    src=str(src_path),
                    dst=str(dst_path),
                    error=str(exc),
                )
                skipped.append(src_path.name)
                continue
            logger.info(
                "starter_skills.refreshed",
                skill=src_path.name,
                layout="flat",
                dst=str(dst_path),
            )
            refreshed.append(src_path.name)
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

        # A now-nested bundle skill may sit in the target as a legacy
        # flat ``<name>.md`` from an old deployment. If that flat body
        # is factory-pristine, drop it so the nested copy below can land
        # without producing a duplicate registry entry; an operator-
        # edited flat body wins and stays put.
        legacy_flat = target / f"{src_dir.name}.md"
        if legacy_flat.is_file() and _is_factory_content(
            src_dir.name, skill_md, legacy_flat
        ):
            try:
                legacy_flat.unlink()
                logger.info(
                    "starter_skills.refreshed",
                    skill=src_dir.name,
                    layout="flat_to_nested",
                    dst=str(legacy_flat),
                )
            except OSError as exc:  # pragma: no cover — defensive
                logger.warning(
                    "starter_skills.refresh_failed",
                    src=str(skill_md),
                    dst=str(legacy_flat),
                    error=str(exc),
                )

        if dst_dir.exists():
            existing_md = dst_dir / "SKILL.md"
            if existing_md.is_file() and not _is_factory_content(
                src_dir.name, skill_md, existing_md
            ):
                skipped.append(src_dir.name)
                continue
            # SKILL.md absent: could be a crashed partial refresh — but
            # equally an operator who deleted the entrypoint on purpose
            # or parked their own files here. Review fix: self-heal ONLY
            # when everything present is provably factory-shaped
            # (relative paths that exist in the bundle, plus the usage
            # sidecar); one unknown file → operator content, skip.
            if not existing_md.is_file() and not _only_factory_artifacts(
                src_dir, dst_dir
            ):
                skipped.append(src_dir.name)
                continue
            if _subtree_in_sync(src_dir, dst_dir):
                skipped.append(src_dir.name)
                continue
            # Factory-pristine but stale (e.g. seeded before the P6
            # split, so references/ never landed) — refresh the whole
            # subtree, preserving runtime usage bookkeeping.
            usage_path = dst_dir / _USAGE_FILENAME
            try:
                usage_bytes = (
                    usage_path.read_bytes() if usage_path.is_file() else None
                )
                shutil.rmtree(dst_dir)
                shutil.copytree(src_dir, dst_dir, ignore=_COPYTREE_IGNORE)
                if usage_bytes is not None:
                    usage_path.write_bytes(usage_bytes)
            except OSError as exc:  # pragma: no cover — defensive
                logger.warning(
                    "starter_skills.refresh_failed",
                    src=str(src_dir),
                    dst=str(dst_dir),
                    error=str(exc),
                )
                skipped.append(src_dir.name)
                continue
            logger.info(
                "starter_skills.refreshed",
                skill=src_dir.name,
                layout="nested",
                dst=str(dst_dir),
            )
            refreshed.append(src_dir.name)
            continue
        try:
            shutil.copytree(src_dir, dst_dir, ignore=_COPYTREE_IGNORE)
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
        refreshed=len(refreshed),
    )
    return SeedReport(
        source=source,
        target=target,
        copied=tuple(copied),
        skipped=tuple(skipped),
        refreshed=tuple(refreshed),
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
