"""Single source of truth for the running app's release version.

Historically several call sites read
``importlib.metadata.version("corlinman-server")`` directly. That is the
*sub-package* version, which is **not** bumped per release, so it drifts
from the root ``corlinman`` version used for git tags / GitHub releases
(``pyproject.toml`` ``[project].version``). The updater compared those two
different numbering spaces and was therefore *permanently* "update
available", even right after a successful upgrade.

This module resolves **one** version, in the same numbering space as the
release tags, and every reader (the updater, ``/healthz``, telemetry, the
restart broadcast, and the MCP ``ServerInfo``) routes through it so they
can never disagree.

Precedence
----------

1. ``CORLINMAN_VERSION`` env — but only when it parses as a real version.
   An explicit escape hatch for Docker images / ops that bake it; a git
   ref like ``main`` (which ``deploy/install.sh`` uses for a *different*
   purpose — the ref to install) is ignored so it can't leak in.
2. The root ``corlinman`` ``pyproject.toml`` ``[project].version``, found
   by walking up from this module's location and from the CWD (the native
   systemd unit sets ``WorkingDirectory=<repo>``). This is bumped per
   release and refreshed by ``git reset --hard <tag>`` on every upgrade,
   so it always reflects the deployed code on the editable native install.
3. ``importlib.metadata.version("corlinman-server")`` — legacy fallback
   (preserves prior behaviour when the source root can't be found, e.g. a
   Docker built-wheel with no ``CORLINMAN_VERSION`` set).
4. ``"0.0.0-dev"`` — last resort, intentionally lower than any release so
   the checker still flags "newer available" when run from a raw clone.

The value is re-resolved on each call (env lookup + a couple of tiny
``pyproject.toml`` reads) rather than memoised, so an ``CORLINMAN_VERSION``
override always takes effect and the callers — all low-frequency
(``/info`` / ``/healthz`` polls, boot-time telemetry) — never observe a
stale value. In practice it only changes across a process restart anyway,
since every upgrade restarts the gateway.
"""

from __future__ import annotations

import importlib.metadata
import os
import tomllib
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

__all__ = ["DEV_FALLBACK_VERSION", "resolve_app_version"]

# Last-resort sentinel, lower than any real release.
DEV_FALLBACK_VERSION = "0.0.0-dev"

# The root workspace package whose pyproject holds the release version.
_ROOT_PROJECT_NAME = "corlinman"

# Defensive bound on how far up the tree we walk looking for the root
# pyproject before giving up.
_MAX_WALK_DEPTH = 12


def _normalize(value: str) -> str:
    """Drop a single leading ``v``/``V`` so env stamps like ``v1.27.0``
    match the bare ``1.27.0`` that pyproject / package metadata carry."""
    value = value.strip()
    return value[1:] if value[:1] in ("v", "V") else value


def _looks_like_version(value: str) -> bool:
    """True when ``value`` looks like a real version (starts with a digit,
    optionally ``v``-prefixed) rather than a git ref such as ``main`` or a
    commit sha."""
    normalized = _normalize(value)
    return bool(normalized) and normalized[0].isdigit()


def _read_root_version_from(start: Path) -> str | None:
    """Walk up from ``start`` for the workspace-root ``pyproject.toml``
    whose ``[project].name == "corlinman"`` and return its
    ``[project].version`` (or ``None`` if not found)."""
    try:
        resolved = start.resolve()
    except OSError:
        return None
    for parent in (resolved, *resolved.parents)[:_MAX_WALK_DEPTH]:
        candidate = parent / "pyproject.toml"
        if not candidate.is_file():
            continue
        try:
            data = tomllib.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue
        project = data.get("project")
        if not isinstance(project, dict) or project.get("name") != _ROOT_PROJECT_NAME:
            continue
        version = project.get("version")
        if isinstance(version, str) and version.strip():
            return version.strip()
    return None


def resolve_app_version() -> str:
    """Resolve the running app's release version (see module docstring for
    the precedence chain)."""
    # 1. Explicit env stamp (Docker/ops), ignoring non-version refs.
    env_val = os.environ.get("CORLINMAN_VERSION", "").strip()
    if env_val and _looks_like_version(env_val):
        return _normalize(env_val)

    # 2. Root pyproject on the deployed source tree — from this module, and
    #    (native systemd sets WorkingDirectory=<repo>) from the CWD.
    for anchor in (Path(__file__).parent, _safe_cwd()):
        if anchor is None:
            continue
        found = _read_root_version_from(anchor)
        if found:
            return found

    # 3. Legacy: installed sub-package metadata.
    try:
        return importlib.metadata.version("corlinman-server")
    except importlib.metadata.PackageNotFoundError:
        pass

    # 4. Dev fallback.
    logger.debug("app_version.fallback", resolved=DEV_FALLBACK_VERSION)
    return DEV_FALLBACK_VERSION


def _safe_cwd() -> Path | None:
    try:
        return Path.cwd()
    except OSError:
        return None
