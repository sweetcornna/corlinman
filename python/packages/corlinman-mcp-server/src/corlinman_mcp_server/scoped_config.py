"""Scoped ``.mcp.json`` discovery + precedence merge (Dim 5).

claude-code resolves MCP servers from layered config scopes; corlinman
historically read one flat map out of the gateway config. This module
closes the gap with three file scopes merged over the inline config:

============  =============================  ==========================
scope         file                           typical ownership
============  =============================  ==========================
``user``      ``~/.corlinman/mcp.json``      one operator, all projects
``project``   ``<project>/.mcp.json``        checked into the repo
``local``     ``<project>/.mcp.local.json``  gitignored per-checkout
============  =============================  ==========================

Precedence (later wins, per server *name*):
``inline config < user < project < local``.

File shape mirrors claude-code's — ``{"mcpServers": {name: {...}}}`` —
with corlinman's ``mcp_servers`` / ``servers`` keys accepted as
aliases. Every entry parses through :meth:`McpServerSpec.from_mapping`,
so the body schema is identical to the ``[mcp.servers]`` TOML table.

Total-function contract: an unreadable / malformed file is logged and
skipped, never raised — MCP config must not be able to kill a boot.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from .client_manager import McpServerSpec, load_server_specs

logger = structlog.get_logger(__name__)

__all__ = ["SCOPE_ORDER", "load_scoped_server_specs", "scope_files"]

#: Merge order, weakest first. ``inline`` is the gateway config object.
SCOPE_ORDER: tuple[str, ...] = ("inline", "user", "project", "local")

_FILE_KEYS = ("mcpServers", "mcp_servers", "servers")


def scope_files(
    *, project_dir: Path | None = None, user_dir: Path | None = None
) -> dict[str, Path]:
    """The candidate config file per scope (existing or not).

    ``project_dir`` defaults to the process CWD (the console's project /
    the gateway's working directory); ``user_dir`` defaults to
    ``~/.corlinman``.
    """
    project = Path(project_dir) if project_dir is not None else Path.cwd()
    user = Path(user_dir) if user_dir is not None else Path.home() / ".corlinman"
    return {
        "user": user / "mcp.json",
        "project": project / ".mcp.json",
        "local": project / ".mcp.local.json",
    }


def _specs_from_file(path: Path, scope: str) -> list[McpServerSpec]:
    """Parse one scope file into specs. Missing → ``[]``; malformed →
    logged + ``[]`` (total function)."""
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning(
            "mcp.scoped_config.unreadable", scope=scope, path=str(path), error=str(exc)
        )
        return []
    try:
        data = json.loads(raw_text)
    except ValueError as exc:
        logger.warning(
            "mcp.scoped_config.malformed", scope=scope, path=str(path), error=str(exc)
        )
        return []
    if not isinstance(data, dict):
        logger.warning(
            "mcp.scoped_config.not_an_object", scope=scope, path=str(path)
        )
        return []
    servers: Any = None
    for key in _FILE_KEYS:
        if isinstance(data.get(key), dict):
            servers = data[key]
            break
    if servers is None:
        return []
    specs: list[McpServerSpec] = []
    for name, body in servers.items():
        try:
            specs.append(McpServerSpec.from_mapping(str(name), body))
        except Exception as exc:  # noqa: BLE001 — skip one bad entry, keep the rest
            logger.warning(
                "mcp.scoped_config.bad_server",
                scope=scope,
                path=str(path),
                server=str(name),
                error=str(exc),
            )
    return specs


def load_scoped_server_specs(
    config: Any,
    *,
    project_dir: Path | None = None,
    user_dir: Path | None = None,
) -> list[McpServerSpec]:
    """Merge inline-config specs with the three ``.mcp.json`` scopes.

    Dedup is by server name with the strongest scope winning
    (``local > project > user > inline``); insertion order is preserved
    from weakest to strongest so a fully-shadowed server keeps its
    original position in listings.
    """
    files = scope_files(project_dir=project_dir, user_dir=user_dir)
    merged: dict[str, McpServerSpec] = {
        s.name: s for s in load_server_specs(config)
    }
    counts: dict[str, int] = {}
    for scope in ("user", "project", "local"):
        specs = _specs_from_file(files[scope], scope)
        counts[scope] = len(specs)
        for spec in specs:
            merged[spec.name] = spec
    if any(counts.values()):
        logger.info(
            "mcp.scoped_config.merged",
            total=len(merged),
            **{f"{k}_count": v for k, v in counts.items()},
        )
    return list(merged.values())
