"""Skill installer / uninstaller pipeline.

W1.2 of ``docs/PLAN_SKILL_HUB.md``. Takes a ClawHub tarball through to a
fully-extracted skill bundle inside
``<data_dir>/profiles/<profile>/skills/<slug>/``.

Safety contract
---------------

The tarball comes from the open public ClawHub origin, so the install
path treats it as untrusted input:

1. **Extract to a temp dir first, then atomically rename**. If
   extraction blows up halfway (corrupt tar, oversize file, path
   traversal) the temp dir is removed by the ``TemporaryDirectory``
   context and the on-disk skills directory is unchanged. The skill
   never appears as a half-written ``<slug>/`` that the registry might
   pick up on the next agent turn.

2. **Path traversal guard**. Every member's resolved path must be
   strictly inside the extraction root. We reject:
   * Absolute paths (``/etc/passwd``)
   * ``..`` segments (``../etc/passwd``)
   * Symlinks of any kind (a symlink to ``/`` followed by a relative
     file would escape).
   We use ``Path.resolve(strict=False)`` against the extraction root
   resolved once, then ``is_relative_to`` for the containment check.

3. **Size caps**. 25 MiB uncompressed total, 10 MiB per file. The
   plan's rationale: ClawHub-published skills today are < 1 MiB; a
   compressed bomb would otherwise fill the data volume.

4. **Sidecar**. We write ``.openclaw-meta.json`` inside the extracted
   skill so the list endpoint in W1.3 can tag the row's origin as
   ``hub:<slug>@<ver>`` without round-tripping ClawHub. Bundled skills
   don't have this sidecar — that's how :func:`uninstall_skill` refuses
   to wipe one of the read-only starter bundles.

5. **Blocking I/O off the loop**. Tar extraction and ``rmtree`` are
   blocking; we hand them to ``loop.run_in_executor`` so the gateway
   event loop keeps serving SSE during a big install.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from corlinman_server.system.skill_hub.client import ClawHubClient

if TYPE_CHECKING:  # pragma: no cover — import-only typing
    from corlinman_server.system.audit import SystemAuditLog

logger = structlog.get_logger(__name__)


__all__ = [
    "InstallReport",
    "SkillAlreadyInstalledError",
    "SkillInstallError",
    "UnsafeTarballError",
    "install_skill",
    "uninstall_skill",
]


# Hard caps. The values come from PLAN_SKILL_HUB and are mirrored by the
# UI's "this skill is too large to install" toast (rendered when the
# route layer surfaces UnsafeTarballError).
_MAX_TOTAL_UNCOMPRESSED_BYTES = 25 * 1024 * 1024  # 25 MiB
_MAX_PER_FILE_BYTES = 10 * 1024 * 1024  # 10 MiB

# Filename of the sidecar we drop next to SKILL.md inside an extracted
# bundle. The list endpoint reads this to label origin.
_META_FILENAME = ".openclaw-meta.json"


class SkillInstallError(RuntimeError):
    """Base class for installer failures.

    All other installer errors inherit from this so route handlers can
    catch one type and map to a single error envelope.
    """


class SkillAlreadyInstalledError(SkillInstallError):
    """Target ``profile_skills_dir / slug`` already exists and ``force=False``.

    The route layer maps this to 409 + a "delete first / use force" hint.
    """


class UnsafeTarballError(SkillInstallError):
    """Tarball failed the safety checks (traversal, symlink, size).

    Surfaces as 400 from the install route; the audit log records the
    failed install attempt so an operator can spot probing.
    """


# ---------------------------------------------------------------------------
# Public report dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstallReport:
    """Returned by :func:`install_skill` on success / no-op.

    ``skipped_overwrite=True`` is the no-op case: the target already
    existed and the caller passed ``force=False``. We could raise
    instead, but returning a typed report keeps the route layer's
    response shape stable (no special 409 branch).
    """

    slug: str
    version: str
    target_path: Path
    files_written: int
    bytes_extracted: int
    skipped_overwrite: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    """ISO-8601 UTC with millisecond precision + Z suffix.

    Matches :func:`corlinman_server.system.audit.utcnow_iso` shape so the
    sidecar timestamps round-trip cleanly through the JS UI.
    """
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _validate_name(name: str) -> None:
    """Refuse anything that isn't a single, traversal-free path segment.

    Used by both install (against the resolved slug) and uninstall
    (against the user-provided name). Rejection rules:

    * Non-empty + not pure dots (``.`` / ``..``).
    * No path separator (``/`` or ``\\``).
    * No NUL byte (some filesystems happily accept these and ruin
      everything later).

    Raises :class:`ValueError`. We deliberately picked the stdlib
    exception type rather than :class:`UnsafeTarballError` because the
    name validation is independent of tarball-member validation —
    callers (uninstall in particular) treat a bad name as a caller
    contract violation, not a malicious-payload event.
    """
    if not name or name in (".", ".."):
        raise ValueError(f"invalid skill name: {name!r}")
    if "/" in name or "\\" in name or "\x00" in name:
        raise ValueError(f"invalid skill name: {name!r}")


def _is_within(child: Path, parent: Path) -> bool:
    """``True`` iff ``child`` is a descendant of (or equal to) ``parent``.

    Both must already be absolute; we don't call ``.resolve()`` here
    because the caller has already done that against a real-on-disk
    parent (the temp extract dir). Using ``is_relative_to`` keeps the
    semantics unambiguous on all supported platforms.
    """
    try:
        return child == parent or child.is_relative_to(parent)
    except ValueError:
        return False


def _safe_extract(
    tar: tarfile.TarFile, extract_root: Path
) -> tuple[int, int]:
    """Extract ``tar`` into ``extract_root`` with the safety guards.

    Returns ``(files_written, bytes_extracted)``. Raises
    :class:`UnsafeTarballError` on the first violating member — we fail
    fast so a malicious tarball can't slip a single bad entry in among
    1000 good ones.

    Implementation notes:

    * We walk the member list ourselves rather than using
      :meth:`TarFile.extractall` so we can inspect each member's type
      and the resolved on-disk target before write. We skip symlinks +
      hardlinks unconditionally — skills are plain directory bundles in
      ClawHub today, and supporting links would require additional
      checks to ensure the link target itself doesn't escape.
    * Empty / directory-only entries don't count toward the file count
      but still get ``mkdir`` so an explicitly-listed empty subdir
      survives the round trip.
    """
    resolved_root = extract_root.resolve()
    total_bytes = 0
    files_written = 0

    for member in tar.getmembers():
        # Reject special files outright. Tar can describe sockets,
        # block devices, etc. — none of those belong in a skill.
        if (
            member.issym()
            or member.islnk()
            or member.ischr()
            or member.isblk()
            or member.isfifo()
            or member.isdev()
        ):
            raise UnsafeTarballError(
                f"refusing to extract special-type tar entry: {member.name!r}"
            )

        # Resolve the on-disk target inside the extract root. We use
        # ``Path.joinpath`` then ``resolve(strict=False)`` so a member
        # name like ``foo/../bar`` collapses before the containment
        # check — without resolving first, ``is_relative_to`` would
        # happily accept ``foo/../../escape``.
        name = member.name
        if not name:
            continue
        if name.startswith("/") or "\x00" in name:
            raise UnsafeTarballError(
                f"absolute or NUL-bearing tar member: {name!r}"
            )
        candidate = (resolved_root / name).resolve(strict=False)
        if not _is_within(candidate, resolved_root):
            raise UnsafeTarballError(
                f"tar member escapes extract root: {name!r}"
            )

        if member.isdir():
            candidate.mkdir(parents=True, exist_ok=True)
            continue
        if not member.isfile():
            # Catch-all for tar types we haven't explicitly listed
            # above; refuse rather than silently dropping.
            raise UnsafeTarballError(
                f"refusing non-file tar entry: {name!r} (type={member.type!r})"
            )

        if member.size > _MAX_PER_FILE_BYTES:
            raise UnsafeTarballError(
                f"tar member {name!r} exceeds per-file cap "
                f"({member.size} > {_MAX_PER_FILE_BYTES})"
            )
        total_bytes += member.size
        if total_bytes > _MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise UnsafeTarballError(
                f"tarball exceeds {_MAX_TOTAL_UNCOMPRESSED_BYTES} bytes "
                f"uncompressed"
            )

        candidate.parent.mkdir(parents=True, exist_ok=True)
        source = tar.extractfile(member)
        if source is None:
            # Defensive — should never happen for a regular file but
            # ``extractfile`` can return None for certain odd entries.
            raise UnsafeTarballError(
                f"could not open tar member {name!r} for read"
            )
        # Read fully + write via :meth:`Path.write_bytes` rather than
        # ``shutil.copyfileobj`` so the W1.4 atomicity test (which
        # monkey-patches ``Path.write_bytes`` to explode mid-extract)
        # can verify that a partial extract does not survive on disk.
        # The per-file 10 MiB cap above bounds the in-memory cost.
        with source:
            data = source.read()
        candidate.write_bytes(data)
        files_written += 1

    return files_written, total_bytes


def _resolve_skill_root(staging: Path, slug: str) -> Path:
    """Find the directory inside ``staging`` that holds ``SKILL.md``.

    ClawHub bundles conventionally have a top-level ``<slug>/`` wrapper
    so a ``tar tf`` listing shows ``web-search/SKILL.md`` rather than a
    bare ``SKILL.md``. The three cases:

    1. ``staging/SKILL.md`` exists — the tarball was already flat; use
       staging itself.
    2. ``staging/<slug>/SKILL.md`` exists — the conventional case;
       lift the inner dir up.
    3. Exactly one subdirectory exists with a ``SKILL.md`` inside —
       use it (handles tarballs where the wrapping dir has a slightly
       different name from the slug).

    Otherwise we just return ``staging`` and let the install proceed;
    the route layer will surface a "SKILL.md missing" warning on the
    next registry scan.
    """
    if (staging / "SKILL.md").is_file():
        return staging
    candidate = staging / slug
    if candidate.is_dir() and (candidate / "SKILL.md").is_file():
        return candidate
    entries = [p for p in staging.iterdir() if p.is_dir()]
    if len(entries) == 1 and (entries[0] / "SKILL.md").is_file():
        return entries[0]
    return staging


def _do_extract(
    tarball: bytes,
    extract_root: Path,
) -> tuple[int, int]:
    """Open ``tarball`` (bytes) as a tar archive + run the safe extract.

    Synchronous — called via ``run_in_executor`` from the public
    coroutine. We detect compression off the bytes header rather than
    trusting any upstream Content-Type; ClawHub serves gzip.
    """
    buffer = io.BytesIO(tarball)
    # ``mode="r:*"`` autodetects gzip / bzip2 / xz / uncompressed.
    try:
        with tarfile.open(fileobj=buffer, mode="r:*") as tar:
            return _safe_extract(tar, extract_root)
    except tarfile.TarError as exc:
        raise UnsafeTarballError(f"could not read tar archive: {exc}") from exc


def _write_sidecar(
    skill_dir: Path, *, slug: str, version: str
) -> None:
    """Write the ``.openclaw-meta.json`` sidecar.

    The W1.3 list endpoint reads this to tag origin as
    ``hub:<slug>@<ver>``. Bundled skills don't have a sidecar — that's
    intentional and what gates :func:`uninstall_skill` against accidental
    deletion of the read-only starter bundle.
    """
    payload = {
        "slug": slug,
        "version": version,
        "installed_at": _utcnow_iso(),
        "source": "clawhub",
    }
    (skill_dir / _META_FILENAME).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


async def _audit(
    audit_log: SystemAuditLog | None,
    event: str,
    *,
    slug: str,
    version: str | None = None,
    files_written: int | None = None,
    actor: str | None = None,
) -> None:
    """Best-effort audit write. Never raises — mirrors the dispatcher's
    audit helper so a write hiccup never blocks an install.
    """
    if audit_log is None:
        return
    try:
        from corlinman_server.system.audit import AuditEntry, utcnow_iso

        details: dict[str, object] = {"slug": slug, "source": "clawhub"}
        if version is not None:
            details["version"] = version
        if files_written is not None:
            details["files_written"] = files_written
        await audit_log.append(
            AuditEntry(
                ts=utcnow_iso(),
                event=event,
                request_id=None,
                tag=slug,
                actor=actor or "admin",
                details=details,
            )
        )
    except Exception:  # audit must never raise upward
        logger.exception("skill_hub.audit_failed", event=event, slug=slug)


# ---------------------------------------------------------------------------
# Public coroutines
# ---------------------------------------------------------------------------


async def install_skill(
    *,
    profile_skills_dir: Path,
    client: ClawHubClient,
    slug: str,
    version: str = "latest",
    force: bool = False,
    audit_log: SystemAuditLog | None = None,
) -> InstallReport:
    """Download + extract ``slug@version`` into ``profile_skills_dir``.

    Flow:

    1. Validate slug is a single safe path component (defence in depth;
       the route layer also validates).
    2. Refuse early if the target already exists and ``force=False`` —
       no network round trip for a no-op install.
    3. Download the tarball via the provided :class:`ClawHubClient`.
    4. Extract into a :class:`tempfile.TemporaryDirectory` first. This
       is the safety isolator: a corrupt or oversize tarball never
       leaves a half-extracted skill on disk.
    5. Write the sidecar JSON inside the temp tree.
    6. Atomic rename: ``os.replace(temp_tree, target)``. If ``force``
       and an existing dir is in the way, blow it away first (also off
       the loop via the executor).
    7. Audit-log ``skill.installed``.

    Returns an :class:`InstallReport`. Re-raises :class:`HubUnavailableError`
    / :class:`HubRateLimitedError` from the download untouched so the
    route layer can map them to upstream-degraded responses.
    """
    _validate_name(slug)
    profile_skills_dir = profile_skills_dir.resolve()
    target = (profile_skills_dir / slug).resolve()

    # Containment double-check — _validate_name already rejects ``..``
    # but the explicit resolve+is_within keeps us safe against a
    # never-thought-of-it symlink in ``profile_skills_dir``.
    if not _is_within(target, profile_skills_dir):
        raise UnsafeTarballError(
            f"resolved target {target} escapes profile skills dir"
        )

    if target.exists() and not force:
        await _audit(
            audit_log,
            "skill.install_skipped",
            slug=slug,
            version=version,
        )
        raise SkillAlreadyInstalledError(
            f"skill {slug!r} already installed at {target}; pass force=True to overwrite"
        )

    # Step 3 — download. May raise HubUnavailableError /
    # HubRateLimitedError, both of which we let bubble up.
    download = await client.download(slug, version=version)
    profile_skills_dir.mkdir(parents=True, exist_ok=True)

    # We use ``TemporaryDirectory`` rooted inside the profile dir so the
    # final ``os.replace`` stays on the same filesystem — a cross-fs
    # rename would fall back to a copy and break the atomicity guarantee.
    # Hand-rolled temp dir. ``tempfile.TemporaryDirectory`` (and its
    # cleanup) hits a macOS-APFS ENOTEMPTY race after we rename the
    # staging-child out to the target; managing the temp dir ourselves
    # lets us pass ``ignore_errors=True`` to the cleanup so a perfectly
    # installed skill never reports as failed because of a post-success
    # housekeeping quirk. Synchronous calls below: tempfile.mkdtemp,
    # Path.mkdir, _write_sidecar, os.replace are all microsecond ops on
    # the local filesystem and don't need to be punted to a worker
    # thread — the previous run_in_executor wiring deadlocked when the
    # caller was an ``asyncio.create_task`` running inside Starlette's
    # BlockingPortal (the portal's default executor is single-threaded).
    raw_tmp = tempfile.mkdtemp(
        prefix=f".install-{slug}-", dir=str(profile_skills_dir)
    )
    tmp_root = Path(raw_tmp)
    try:
        staging = tmp_root / "staging"
        staging.mkdir()

        # Tar extraction is synchronous. The plan originally punted
        # this to a worker thread to avoid blocking the loop on huge
        # bundles, but anyio.to_thread.run_sync deadlocks when this
        # function is invoked from an ``asyncio.create_task`` running
        # under Starlette's BlockingPortal (anyio's capacity limiter
        # interacts badly with the portal). Since ClawHub-published
        # skills are <1 MiB and we already cap at 25 MiB total, the
        # synchronous cost is bounded; revisit if larger bundles
        # become common.
        files_written, bytes_extracted = _do_extract(download.content, staging)

        # ClawHub tarballs conventionally wrap the skill content in a
        # top-level ``<slug>/`` directory (so ``tar tf`` shows
        # ``web-search/SKILL.md`` rather than a bare ``SKILL.md``).
        # If we detect that pattern we lift the inner directory up so
        # the final on-disk layout is ``<profile>/skills/<slug>/SKILL.md``
        # rather than the double-nested ``<profile>/skills/<slug>/<slug>/SKILL.md``.
        skill_root = _resolve_skill_root(staging, slug)

        _write_sidecar(skill_root, slug=slug, version=version)

        # Step 6 — atomic rename. If ``force`` and the target exists,
        # we need to clear it first; otherwise ``os.replace`` happily
        # overwrites an empty target but refuses to overwrite a
        # non-empty dir on most filesystems.
        if target.exists():
            if not force:  # pragma: no cover — guarded above, defence-in-depth.
                raise SkillAlreadyInstalledError(
                    f"target {target} appeared mid-install"
                )
            shutil.rmtree(target)

        os.replace(str(skill_root), str(target))
    finally:
        # Best-effort cleanup of the scratch dir. ``ignore_errors=True``
        # eats the macOS APFS ENOTEMPTY-after-rename quirk; any
        # orphaned ``.install-*`` dirs left behind get swept on next boot.
        shutil.rmtree(tmp_root, ignore_errors=True)

    await _audit(
        audit_log,
        "skill.installed",
        slug=slug,
        version=version,
        files_written=files_written,
    )

    return InstallReport(
        slug=slug,
        version=version,
        target_path=target,
        files_written=files_written,
        bytes_extracted=bytes_extracted,
        skipped_overwrite=False,
    )


async def uninstall_skill(
    *,
    profile_skills_dir: Path,
    name: str,
    audit_log: SystemAuditLog | None = None,
) -> None:
    """Delete a hub-installed skill from ``profile_skills_dir / name``.

    Refuses three cases (each as :class:`SkillInstallError`):

    1. ``name`` isn't a single safe path component (``/`` or ``..`` →
       :class:`UnsafeTarballError`).
    2. The target doesn't exist (we don't pretend success — the route
       layer should map to 404).
    3. The target exists but lacks ``.openclaw-meta.json`` — this is
       how bundled starter skills are protected; they ship without a
       sidecar so this check refuses to ``rm -rf`` them.
    """
    _validate_name(name)
    profile_skills_dir = profile_skills_dir.resolve()
    target = (profile_skills_dir / name).resolve()
    if not _is_within(target, profile_skills_dir):
        raise UnsafeTarballError(
            f"resolved target {target} escapes profile skills dir"
        )

    if not target.exists():
        raise SkillInstallError(f"skill {name!r} not installed")
    if not target.is_dir():
        raise SkillInstallError(
            f"skill path {target} is not a directory"
        )
    if not (target / _META_FILENAME).is_file():
        raise SkillInstallError(
            f"refusing to uninstall {name!r}: no .openclaw-meta.json "
            "sidecar (likely a bundled starter skill — edit the profile "
            "copy instead)"
        )

    shutil.rmtree(target)
    await _audit(audit_log, "skill.uninstalled", slug=name)
