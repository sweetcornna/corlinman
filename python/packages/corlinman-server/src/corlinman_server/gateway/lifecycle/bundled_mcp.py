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
marketplace's ``mcp_servers.sqlite`` exactly once, after which it is an
ordinary installed server the operator can reconfigure, disable, or delete
from the UI like any other.

"Exactly once" is load-bearing and is **not** the same as "insert if
absent". A server the operator deliberately deleted is absent, and
re-inserting it on the next restart would be the gateway overruling them
every boot. So the names that have ever been seeded are recorded in a
sentinel file (:data:`_SENTINEL_NAME`) beside the store, and a name listed
there is never seeded again regardless of whether its row still exists.

Failure is quiet by construction: a seeding error is logged and the boot
continues. Worst case the operator installs the server from the marketplace
by hand.
"""

from __future__ import annotations

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
    """

    name: str
    spec: dict[str, Any]
    version: str
    source: str = "bundled"
    enabled: bool = True


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
    ),
)


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


def seed_bundled_mcp_servers(
    store: Any,
    data_dir: Path,
    *,
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
    seeded_before = _load_seeded(data_dir)
    newly: list[str] = []
    # Whether the sentinel needs rewriting. Distinct from ``newly``: a
    # server we recorded as already-present changes the sentinel without
    # having been seeded by this call.
    dirty = False

    for entry in bundle:
        if entry.name in seeded_before:
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
                dict(entry.spec),
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
