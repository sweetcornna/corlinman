"""Marketplace *plugin* installer / uninstaller pipeline.

A plugin is a directory bundle (``manifest.json`` + code, optionally a
``plugin.toml``) shipped as a gzip tarball from the marketplace source. The
install path is the plugin analogue of
:mod:`corlinman_server.system.skill_hub.installer` and **reuses that
module's hardened, already-audited primitives** rather than re-deriving the
tar-safety logic:

* :func:`~corlinman_server.system.skill_hub.installer._do_extract` — open
  the gzip bytes + run the path-traversal / symlink / size-cap guarded
  extract into a staging dir.
* :func:`~corlinman_server.system.skill_hub.installer._validate_name` —
  reject a slug that isn't a single, traversal-free path segment.
* :func:`~corlinman_server.system.skill_hub.installer._is_within` —
  containment check for the resolved target.
* :data:`~corlinman_server.system.skill_hub.installer._META_FILENAME` — the
  ``.openclaw-meta.json`` sidecar filename.
* :class:`~corlinman_server.system.skill_hub.installer.UnsafeTarballError` —
  reused for tar-traversal rejections so the route layer keeps one
  400-mapping path for every extension kind.

The one difference from ``install_skill`` is the *entrypoint shape*: the
marketplace route has already downloaded the tarball bytes via a
:class:`MarketplaceSource`, so :func:`install_plugin` takes raw ``content``
bytes rather than a download client.

Safety contract (inherited from the skill installer)
----------------------------------------------------

1. Extract to a temp staging dir first, then atomically ``os.replace`` onto
   the target — a corrupt / oversize / traversing tarball never leaves a
   half-written ``<slug>/`` for the registry to pick up.
2. Path-traversal + symlink + absolute-path + size-cap guards live in the
   reused ``_do_extract`` and raise :class:`UnsafeTarballError`.
3. A ``.openclaw-meta.json`` sidecar records provenance; its absence is how
   :func:`uninstall_plugin` refuses to ``rm -rf`` a bundled / hand-placed
   plugin.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

# Reuse the hardened, already-audited primitives from the skill installer.
# We import (never edit) these so the tar-safety logic has exactly one
# implementation across every marketplace extension kind.
from corlinman_server.system.skill_hub.installer import (
    _META_FILENAME,
    UnsafeTarballError,
    _do_extract,
    _is_within,
    _validate_name,
)

if TYPE_CHECKING:  # pragma: no cover — import-only typing
    from corlinman_server.system.audit import SystemAuditLog

logger = structlog.get_logger(__name__)

__all__ = [
    "PluginAlreadyInstalledError",
    "PluginInstallError",
    "PluginInstallReport",
    "UnsafeTarballError",
    "install_plugin",
    "uninstall_plugin",
]


# Manifest filenames a plugin bundle is recognised by. ``plugin-manifest.toml``
# is the canonical PluginRegistry format (and the one that hot-loads); the
# JSON / ``plugin.toml`` spellings are accepted fallbacks so a bundle that
# ships only those still installs (it just won't hot-load until it carries a
# ``plugin-manifest.toml``).
_PLUGIN_MANIFEST_CANONICAL = "plugin-manifest.toml"
_PLUGIN_MANIFEST = "manifest.json"
_PLUGIN_MANIFEST_FALLBACK = "plugin.toml"
_PLUGIN_MANIFEST_MARKERS = (
    _PLUGIN_MANIFEST_CANONICAL,
    _PLUGIN_MANIFEST,
    _PLUGIN_MANIFEST_FALLBACK,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PluginInstallError(RuntimeError):
    """Base class for plugin-installer failures.

    All other installer errors inherit from this so route handlers can
    catch one type and map to a single error envelope.
    :class:`UnsafeTarballError` (reused from the skill installer) is a
    sibling of this hierarchy, not a subclass — the route layer catches
    both.
    """


class PluginAlreadyInstalledError(PluginInstallError):
    """Target ``plugins_dir / slug`` already exists and ``force=False``.

    The route layer maps this to 409 + a "delete first / use force" hint.
    """


# ---------------------------------------------------------------------------
# Public report dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PluginInstallReport:
    """Returned by :func:`install_plugin` on success.

    Mirrors :class:`corlinman_server.system.skill_hub.installer.InstallReport`
    minus the ``skipped_overwrite`` field — :func:`install_plugin` raises
    :class:`PluginAlreadyInstalledError` on the existing-target/no-force case
    rather than returning a no-op report, so the report always describes a
    real install.
    """

    slug: str
    version: str
    target_path: Path
    files_written: int
    bytes_extracted: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    """ISO-8601 UTC with millisecond precision + ``Z`` suffix.

    Matches the skill installer's sidecar timestamp shape so the two
    ``.openclaw-meta.json`` flavours round-trip identically through the UI.
    """
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _resolve_plugin_root(staging: Path, slug: str) -> Path:
    """Find the directory inside ``staging`` that holds the plugin manifest.

    Marketplace plugin tarballs conventionally wrap the bundle in a
    top-level ``<slug>/`` directory (so ``tar tf`` shows
    ``echo-plugin/manifest.json`` rather than a bare ``manifest.json``).
    This mirrors
    :func:`corlinman_server.system.skill_hub.installer._resolve_skill_root`
    three-case logic, but locates ``manifest.json`` (falling back to
    ``plugin.toml``) instead of ``SKILL.md``:

    1. ``staging/manifest.json`` exists — already flat; use staging itself.
    2. ``staging/<slug>/manifest.json`` exists — the conventional case;
       lift the inner dir up.
    3. Exactly one subdirectory exists with a manifest inside — use it
       (handles a wrapping dir whose name differs from the slug).

    Otherwise return ``staging`` and let the install proceed; the route
    layer surfaces a "manifest missing" warning on the next registry scan.
    """

    def _has_manifest(d: Path) -> bool:
        return any((d / marker).is_file() for marker in _PLUGIN_MANIFEST_MARKERS)

    if _has_manifest(staging):
        return staging
    candidate = staging / slug
    if candidate.is_dir() and _has_manifest(candidate):
        return candidate
    entries = [p for p in staging.iterdir() if p.is_dir()]
    if len(entries) == 1 and _has_manifest(entries[0]):
        return entries[0]
    return staging


def _write_sidecar(
    plugin_dir: Path, *, slug: str, version: str, source: str
) -> None:
    """Write the ``.openclaw-meta.json`` provenance sidecar.

    The list endpoint reads this to tag the plugin's origin, and
    :func:`uninstall_plugin` uses its presence to gate deletion (no sidecar
    → refuse, so a hand-placed / bundled plugin is never wiped).
    """
    payload = {
        "slug": slug,
        "version": version,
        "installed_at": _utcnow_iso(),
        "source": source,
    }
    (plugin_dir / _META_FILENAME).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


async def _audit(
    audit_log: SystemAuditLog | None,
    event: str,
    *,
    slug: str,
    version: str | None = None,
    source: str | None = None,
    files_written: int | None = None,
    actor: str | None = None,
) -> None:
    """Best-effort audit write. Never raises — mirrors the skill installer's
    audit helper so a write hiccup never blocks an install.
    """
    if audit_log is None:
        return
    try:
        from corlinman_server.system.audit import AuditEntry, utcnow_iso

        details: dict[str, object] = {"slug": slug, "kind": "plugin"}
        if source is not None:
            details["source"] = source
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
        logger.exception("marketplace.plugin.audit_failed", event=event, slug=slug)


# ---------------------------------------------------------------------------
# Public coroutines
# ---------------------------------------------------------------------------


async def install_plugin(
    *,
    plugins_dir: Path,
    content: bytes,
    slug: str,
    version: str = "latest",
    source: str = "github",
    force: bool = False,
    audit_log: SystemAuditLog | None = None,
) -> PluginInstallReport:
    """Extract a downloaded plugin tarball into ``plugins_dir / slug``.

    Unlike :func:`corlinman_server.system.skill_hub.installer.install_skill`,
    this takes the **raw tarball bytes** (already fetched by the route via a
    :class:`MarketplaceSource`) rather than a download client.

    Flow (mirrors ``install_skill``):

    1. Validate ``slug`` is a single safe path component.
    2. Resolve ``target = plugins_dir / slug`` and double-check containment.
    3. Refuse early with :class:`PluginAlreadyInstalledError` if the target
       exists and ``force=False`` — no extraction for a no-op install.
    4. Extract into a :func:`tempfile.mkdtemp` staging dir under
       ``plugins_dir`` (same filesystem so the final rename is atomic) via
       the reused hardened ``_do_extract``.
    5. Resolve the manifest-bearing root with :func:`_resolve_plugin_root`.
    6. Write the ``.openclaw-meta.json`` sidecar inside that root.
    7. If ``force`` and the target exists, ``shutil.rmtree`` it, then
       ``os.replace`` the staging root onto the target.
    8. Best-effort cleanup of the staging dir + audit ``plugin.installed``.

    Re-raises :class:`UnsafeTarballError` from extraction untouched so the
    route layer maps it to a single 400 path.
    """
    _validate_name(slug)
    plugins_dir = plugins_dir.resolve()
    target = (plugins_dir / slug).resolve()

    # Containment double-check — _validate_name already rejects ``..`` but
    # the explicit resolve+is_within keeps us safe against a symlink in
    # ``plugins_dir``.
    if not _is_within(target, plugins_dir):
        raise UnsafeTarballError(
            f"resolved target {target} escapes plugins dir"
        )

    if target.exists() and not force:
        await _audit(
            audit_log,
            "plugin.install_skipped",
            slug=slug,
            version=version,
            source=source,
        )
        raise PluginAlreadyInstalledError(
            f"plugin {slug!r} already installed at {target}; "
            "pass force=True to overwrite"
        )

    plugins_dir.mkdir(parents=True, exist_ok=True)

    # Hand-rolled temp dir rooted *inside* plugins_dir so the final
    # ``os.replace`` stays on the same filesystem (a cross-fs rename would
    # fall back to a copy + break the atomicity guarantee). ``ignore_errors``
    # on cleanup eats the macOS-APFS ENOTEMPTY-after-rename quirk — the same
    # reasoning the skill installer documents.
    raw_tmp = tempfile.mkdtemp(prefix=f".install-{slug}-", dir=str(plugins_dir))
    tmp_root = Path(raw_tmp)
    try:
        staging = tmp_root / "staging"
        staging.mkdir()

        # Reused hardened extractor: opens the gzip bytes, enforces the
        # path-traversal / symlink / absolute-path / size guards, and raises
        # UnsafeTarballError on the first violating member.
        files_written, bytes_extracted = _do_extract(content, staging)

        plugin_root = _resolve_plugin_root(staging, slug)

        _write_sidecar(plugin_root, slug=slug, version=version, source=source)

        if target.exists():
            if not force:  # pragma: no cover — guarded above, defence-in-depth.
                raise PluginAlreadyInstalledError(
                    f"target {target} appeared mid-install"
                )
            shutil.rmtree(target)

        os.replace(str(plugin_root), str(target))
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    await _audit(
        audit_log,
        "plugin.installed",
        slug=slug,
        version=version,
        source=source,
        files_written=files_written,
    )

    return PluginInstallReport(
        slug=slug,
        version=version,
        target_path=target,
        files_written=files_written,
        bytes_extracted=bytes_extracted,
    )


async def uninstall_plugin(
    *,
    plugins_dir: Path,
    slug: str,
    audit_log: SystemAuditLog | None = None,
) -> None:
    """Delete a marketplace-installed plugin from ``plugins_dir / slug``.

    Refuses (each as :class:`PluginInstallError`, traversal as
    :class:`UnsafeTarballError`):

    1. ``slug`` isn't a single safe path component (``/`` / ``..`` →
       :class:`UnsafeTarballError` via the resolved-target check; the bad
       name itself trips :func:`_validate_name`'s ``ValueError`` first).
    2. The target doesn't exist (no pretend-success; route → 404).
    3. The target exists but lacks ``.openclaw-meta.json`` — this protects
       bundled / hand-placed plugins from being ``rm -rf``'d, exactly as
       :func:`uninstall_skill` does.
    """
    _validate_name(slug)
    plugins_dir = plugins_dir.resolve()
    target = (plugins_dir / slug).resolve()
    if not _is_within(target, plugins_dir):
        raise UnsafeTarballError(
            f"resolved target {target} escapes plugins dir"
        )

    if not target.exists():
        raise PluginInstallError(f"plugin {slug!r} not installed")
    if not target.is_dir():
        raise PluginInstallError(f"plugin path {target} is not a directory")
    if not (target / _META_FILENAME).is_file():
        raise PluginInstallError(
            f"refusing to uninstall {slug!r}: no .openclaw-meta.json sidecar "
            "(likely a bundled or hand-placed plugin)"
        )

    shutil.rmtree(target)
    await _audit(audit_log, "plugin.uninstalled", slug=slug)
