"""First-boot seeding of the MCP servers corlinman ships with.

A fresh gateway knows about no MCP servers, so the model's only web reach is
the builtin ``web_search`` — a keyless DuckDuckGo HTML scrape that returns
five title/URL/snippet rows and nothing else. The point of this module is
that a new install arrives with a real research stack instead: multi-engine
search with fallback, reader-mode page fetch, document reading, and a
one-shot ``research`` that collapses search→fetch→fetch into a single tool
call.

Mechanism
---------

The bundle is *seeded*, not hard-wired: each entry is written into the
marketplace's ``mcp_servers.sqlite`` on first boot, after which it is an
ordinary installed server the operator can reconfigure, disable, or delete
from the UI like any other.

"Only offer once" is load-bearing and is **not** the same as "insert if
absent". A server the operator deliberately deleted is absent, and
re-inserting it on the next restart would be the gateway overruling them
every boot. So the names that have ever been seeded are recorded in a
sentinel file (:data:`_SENTINEL_NAME`) beside the store, and a deleted name
listed there stays deleted.

Factory-pristine rows are the sole exception: when their complete persisted
spec and provenance match a known factory revision, they are refreshed to
the current bundle while preserving the operator's enabled state. This lets
existing installs receive safe bundle additions without overwriting any row
the operator edited.

Failure is quiet by construction: a seeding or refresh error is logged and
the boot continues. Worst case the operator installs or updates the server
from the marketplace by hand.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

__all__ = [
    "BUNDLED_MCP_SERVERS",
    "BundledMcpServer",
    "seed_bundled_mcp_servers",
]

_SENTINEL_NAME = ".mcp_bundled_seeded.json"
"""Records which bundled names have ever been seeded, so a deleted server
stays deleted."""

#: Minimum free-search-mcp release corlinman will launch.
#:
#: 0.8.0 closed three SSRF guard bypasses in that project's own URL
#: validation. Pinning the floor here means an operator whose cache or index
#: still offers 0.7.x gets a visible spawn failure (the server shows up in
#: the UI as ``error``) rather than a silently vulnerable fetcher — the
#: search tools are, by their nature, pointed at URLs the model chose.
FREE_SEARCH_MIN_VERSION = "0.8.0"


@dataclass(frozen=True, slots=True)
class BundledMcpServer:
    """One factory MCP server.

    ``spec`` is the mapping :class:`corlinman_mcp_server.McpServerSpec` is
    built from — the same shape the marketplace persists for a hand-installed
    server, so nothing downstream can tell a seeded row from an installed one.
    ``workspace_env`` maps environment-variable names to paths relative to the
    agent workspace; those values are materialized before persistence rather
    than baking one installation's absolute path into the module-level bundle.
    """

    name: str
    spec: dict[str, Any]
    version: str
    source: str = "bundled"
    enabled: bool = True
    workspace_env: tuple[tuple[str, str], ...] = ()


BUNDLED_MCP_SERVERS: tuple[BundledMcpServer, ...] = (
    BundledMcpServer(
        name="search",
        version=FREE_SEARCH_MIN_VERSION,
        spec={
            "transport": "stdio",
            "command": "uvx",
            # A requirement specifier, not a bare name: uvx resolves the
            # newest release satisfying it, so the floor above is enforced
            # at spawn time and later releases are picked up without a
            # corlinman change.
            "args": [f"free-search-mcp>={FREE_SEARCH_MIN_VERSION}"],
            # The first uvx run downloads the package; give it room. Later
            # boots hit the uv cache and connect in well under a second.
            "handshake_timeout_s": 180.0,
            "call_timeout_s": 120.0,
        },
        workspace_env=(
            ("SEARCH_MCP_DOWNLOAD_DIR", "downloads/search-mcp"),
        ),
    ),
)

# SHA-256 of every complete factory revision that may be refreshed in place.
# The digest covers source + version + the unmaterialized spec, including
# ``<workspace>`` placeholders for per-install paths. Current revisions live
# here too: a CI guard forces future bundle edits to append their digest, so
# every upgrade path is an explicit decision rather than a broad match.
_FACTORY_REVISION_SHA256: dict[str, frozenset[str]] = {
    "search": frozenset(
        {
            # Download-enabled factory revision.
            "c71b5d2390fd01db6f7b599f7162b16ae24ee291cff0a6bc73fc6f45d05371f9",
            # 5440ae96 (#199) — original bundled search server.
            "46bffd1648c5f650f0e1a50ed36be7a97dacc419c1de78364c446e51079c6074",
        }
    ),
}

_WORKSPACE_PLACEHOLDER = "<workspace>"


def _sentinel_path(data_dir: Path) -> Path:
    return data_dir / _SENTINEL_NAME


def _load_seeded(data_dir: Path) -> set[str]:
    """Names already seeded. An unreadable/corrupt sentinel reads as empty.

    Treating a corrupt sentinel as "nothing seeded" can re-seed a deleted
    server once; treating it as "everything seeded" would permanently
    suppress the bundle on a fresh install. The first failure mode is
    recoverable by the operator, the second is invisible.
    """
    path = _sentinel_path(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except Exception as exc:  # noqa: BLE001 — corrupt sentinel must not block boot
        logger.warning("gateway.mcp.bundle_sentinel_unreadable", error=str(exc))
        return set()
    if isinstance(raw, dict):
        names = raw.get("seeded")
        if isinstance(names, list):
            return {str(n) for n in names}
    return set()


def _save_seeded(data_dir: Path, names: set[str]) -> None:
    path = _sentinel_path(data_dir)
    payload = {"seeded": sorted(names)}
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("gateway.mcp.bundle_sentinel_write_failed", error=str(exc))


def _materialize_spec(entry: BundledMcpServer, workspace: Path) -> dict[str, Any]:
    spec = copy.deepcopy(entry.spec)
    if entry.workspace_env:
        env = {
            str(key): str(value)
            for key, value in dict(spec.get("env") or {}).items()
        }
        for key, relative in entry.workspace_env:
            env[key] = str(workspace / relative)
        spec["env"] = env
    return spec


def _factory_revision_digest(
    spec: dict[str, Any],
    *,
    source: str | None,
    version: str | None,
) -> str:
    payload = {
        "source": source,
        "version": version,
        "spec": spec,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _current_factory_revision_digest(entry: BundledMcpServer) -> str:
    return _factory_revision_digest(
        _materialize_spec(entry, Path(_WORKSPACE_PLACEHOLDER)),
        source=entry.source,
        version=entry.version,
    )


def _factory_revision_digests(entry: BundledMcpServer) -> frozenset[str]:
    return _FACTORY_REVISION_SHA256.get(entry.name, frozenset())


def _is_factory_pristine(entry: BundledMcpServer, row: Any, workspace: Path) -> bool:
    normalized = copy.deepcopy(row.spec)
    if entry.workspace_env:
        env = normalized.get("env")
        if isinstance(env, dict):
            for key, relative in entry.workspace_env:
                if str(env.get(key, "")) == str(workspace / relative):
                    env[key] = f"{_WORKSPACE_PLACEHOLDER}/{relative}"
    digest = _factory_revision_digest(
        normalized,
        source=row.source,
        version=row.version,
    )
    return digest in _factory_revision_digests(entry)


def seed_bundled_mcp_servers(
    store: Any,
    data_dir: Path,
    *,
    workspace: Path | None = None,
    bundle: tuple[BundledMcpServer, ...] = BUNDLED_MCP_SERVERS,
    enabled_override: bool | None = None,
) -> list[str]:
    """Seed any never-before-seeded bundled server into ``store``.

    Parameters
    ----------
    store
        An open :class:`~corlinman_server.system.marketplace.mcp_store.\
McpServerStore`.
    data_dir
        Where the sentinel lives — the same directory as the store.
    workspace
        Agent-visible shared workspace. Defaults to ``data_dir / "workspace"``
        for historical flat layouts; split-process boot passes its resolved
        execution-state workspace explicitly.
    bundle
        Override for tests.
    enabled_override
        Force the seeded rows enabled/disabled, ignoring each entry's own
        default. This is how ``[mcp].bundle_enabled = false`` keeps the
        factory servers registered-but-inert for an operator who wants to
        review them before anything spawns.

    Returns
    -------
    list[str]
        The names seeded by *this* call (empty on a repeat boot).
    """
    resolved_workspace = workspace or data_dir / "workspace"
    seeded_before = _load_seeded(data_dir)
    newly: list[str] = []
    # Whether the sentinel needs rewriting. Distinct from ``newly``: a
    # server we recorded as already-present changes the sentinel without
    # having been seeded by this call.
    dirty = False

    for entry in bundle:
        if entry.name in seeded_before:
            try:
                current = store.get(entry.name)
            except Exception as exc:  # noqa: BLE001 — refresh never blocks boot
                logger.debug(
                    "gateway.mcp.bundle_probe_failed",
                    server=entry.name,
                    error=str(exc),
                )
                continue
            # Missing after being offered means the operator deleted it.
            if current is None:
                continue
            if not _is_factory_pristine(entry, current, resolved_workspace):
                continue
            current_spec = _materialize_spec(entry, resolved_workspace)
            if (
                current.spec == current_spec
                and current.source == entry.source
                and current.version == entry.version
            ):
                continue
            try:
                store.upsert(
                    entry.name,
                    current_spec,
                    source=entry.source,
                    version=entry.version,
                    enabled=current.enabled,
                )
            except Exception as exc:  # noqa: BLE001 — refresh never blocks boot
                logger.warning(
                    "gateway.mcp.bundle_refresh_failed",
                    server=entry.name,
                    error=str(exc),
                )
                continue
            logger.info(
                "gateway.mcp.bundle_refreshed",
                server=entry.name,
                version=entry.version,
                enabled=current.enabled,
            )
            continue

        # A row already under this name is itself evidence the server has
        # been offered — either the sentinel could not be written (read-only
        # data dir) or the operator installed something under the same name
        # by hand. Either way, overwriting it would clobber their spec on
        # every boot, so record it as offered and leave it alone.
        try:
            if store.get(entry.name) is not None:
                seeded_before.add(entry.name)
                dirty = True
                logger.debug(
                    "gateway.mcp.bundle_already_present", server=entry.name
                )
                continue
        except Exception as exc:  # noqa: BLE001 — probe failure falls through
            logger.debug(
                "gateway.mcp.bundle_probe_failed", server=entry.name, error=str(exc)
            )

        enabled = entry.enabled if enabled_override is None else enabled_override
        try:
            store.upsert(
                entry.name,
                _materialize_spec(entry, resolved_workspace),
                source=entry.source,
                version=entry.version,
                enabled=enabled,
            )
        except Exception as exc:  # noqa: BLE001 — one bad entry never blocks boot
            logger.warning(
                "gateway.mcp.bundle_seed_failed", server=entry.name, error=str(exc)
            )
            continue
        newly.append(entry.name)
        # Recorded even though the row exists: the record is "we have offered
        # this", so a later delete is respected.
        seeded_before.add(entry.name)
        dirty = True
        logger.info(
            "gateway.mcp.bundle_seeded",
            server=entry.name,
            version=entry.version,
            enabled=enabled,
        )

    if dirty:
        _save_seeded(data_dir, seeded_before)
    return newly
