"""Durable, layered permission settings (gap E1).

``PermissionGate.from_layered_sources`` shipped with zero production
callers: every deployment built its gate from ``from_env()`` alone, so the
only way to grant a durable permission rule was an environment variable,
and the console's interactive "always" grants evaporated with the session.

This module is the missing loader. Rule layers, least to most specific:

1. ``<data_dir>/settings.json`` — the user layer. ``data_dir`` resolves
   like every other corlinman store: ``$CORLINMAN_DATA_DIR`` or
   ``~/.corlinman``.
2. ``<project_dir>/.corlinman/settings.local.json`` — the project layer
   (``project_dir`` defaults to the process CWD). ``settings.local.json``
   mirrors the Claude Code convention: machine-local, expected to be
   gitignored.
3. ``CORLINMAN_AGENT_PERMISSIONS`` — the env layer (above both files;
   below the ``[permissions]`` config block, decision C5).

File schema (everything optional, parsed tolerantly)::

    {"permissions": {"rules": [{"tool": ..., "action": ...}, ...],
                     "mode": "default|acceptEdits|plan|bypass",
                     "strict": false}}

``mode`` / ``strict`` follow the deduplicated
:mod:`corlinman_agent.authz.defaults` chain (config > env > project >
user). When NO settings file contributes anything the builder returns
``PermissionGate.from_env()`` verbatim. Rule evaluation is
last-match-wins (C3) so a later (more specific) layer's rule overrides an
earlier one; an explicit ``CORLINMAN_AGENT_PERMISSION_LAST_MATCH_WINS``
env var still wins either way.

W3-1 note: the PRODUCTION gate is now
:class:`corlinman_agent.authz.gate.AuthzGate`, which re-reads all of these
layers at call time. This builder remains for compatibility (it returns a
frozen snapshot).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import structlog

from corlinman_agent.authz.defaults import (
    resolve_last_match_wins,
    resolve_mode,
    resolve_strict,
)
from corlinman_agent.permission import PermissionGate, PermissionMode

logger = structlog.get_logger(__name__)

#: User-layer settings file, sibling to the journal/memory stores.
SETTINGS_FILENAME = "settings.json"
#: Project-layer settings file, relative to the project root.
LOCAL_SETTINGS_RELPATH = Path(".corlinman") / "settings.local.json"

__all__ = [
    "LOCAL_SETTINGS_RELPATH",
    "SETTINGS_FILENAME",
    "build_permission_gate",
    "persist_allow_rule",
    "project_settings_path",
    "user_settings_path",
]


def user_settings_path(data_dir: Path | str | None = None) -> Path:
    """``<data_dir>/settings.json`` with the standard data-dir resolution."""
    if data_dir is None:
        env = os.environ.get("CORLINMAN_DATA_DIR", "").strip()
        base = Path(env) if env else Path.home() / ".corlinman"
    else:
        base = Path(data_dir)
    return base / SETTINGS_FILENAME


def project_settings_path(project_dir: Path | str | None = None) -> Path:
    """``<project_dir>/.corlinman/settings.local.json`` (default: CWD)."""
    base = Path(project_dir) if project_dir is not None else Path.cwd()
    return base / LOCAL_SETTINGS_RELPATH


def _read_permissions_block(
    path: Path,
) -> tuple[list[dict[str, Any]], str | None, bool | None]:
    """Tolerantly read one settings file's ``permissions`` block.

    Returns ``(rules, mode, strict)`` where ``mode`` / ``strict`` are
    ``None`` when the file doesn't declare them. Any read/parse/shape
    problem degrades to the empty contribution — permissions loading must
    never break agent boot.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return [], None, None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("agent.permission_settings.bad_json", path=str(path))
        return [], None, None
    block = data.get("permissions") if isinstance(data, dict) else None
    if not isinstance(block, dict):
        if block is not None:
            logger.warning(
                "agent.permission_settings.bad_block", path=str(path)
            )
        return [], None, None
    rules_raw = block.get("rules")
    rules = [r for r in rules_raw if isinstance(r, dict)] if isinstance(rules_raw, list) else []
    mode = block.get("mode")
    mode_out = mode.strip() if isinstance(mode, str) and mode.strip() else None
    strict = block.get("strict")
    strict_out = strict if isinstance(strict, bool) else None
    return rules, mode_out, strict_out


def build_permission_gate(
    data_dir: Path | str | None = None,
    project_dir: Path | str | None = None,
) -> PermissionGate:
    """Build a frozen gate snapshot from settings files + environment.

    Env rules stack last among these layers, and an env-declared ``mode``
    / ``strict`` overrides any file's (the ``[permissions]`` config block,
    when present, outranks even those — see ``authz.defaults``). With no
    settings file contributing anything this returns
    ``PermissionGate.from_env()`` unchanged.
    """
    user_rules, user_mode, user_strict = _read_permissions_block(
        user_settings_path(data_dir)
    )
    proj_rules, proj_mode, proj_strict = _read_permissions_block(
        project_settings_path(project_dir)
    )
    if not (user_rules or proj_rules or user_mode or proj_mode) and (
        user_strict is None and proj_strict is None
    ):
        return PermissionGate.from_env()

    env_rules_raw = os.environ.get("CORLINMAN_AGENT_PERMISSIONS", "")

    # W3-1: the strict / mode precedence chains are deduplicated into
    # corlinman_agent.authz.defaults — config > env > project > user.
    strict = resolve_strict(proj_strict, user_strict)
    mode = PermissionMode.coerce(resolve_mode(proj_mode, user_mode))

    # Layer precedence (project beats user, env beats both) NEEDS
    # last-match-wins; only an explicit opt-out flips it (C3).
    last_match_wins = resolve_last_match_wins()

    return PermissionGate.from_layered_sources(
        user_rules,
        proj_rules,
        env_rules_raw or None,
        strict=strict,
        mode=mode,
        last_match_wins=last_match_wins,
    )


def persist_allow_rule(
    tool: str, data_dir: Path | str | None = None
) -> Path:
    """Durably grant ``tool`` by appending an allow rule to the USER layer.

    .. deprecated:: W3-1
        The production write path moved to the
        :class:`corlinman_agent.authz.grants.GrantStore` — an interactive
        "always" answer records an args-scoped grant there instead of
        flattening it into a global unconditional allow rule. The settings
        file keeps being READ (operator-written policy); this writer stays
        only for compatibility and is no longer called by the console.

    The settings file is read (a corrupt file is replaced rather than
    crashing the grant), the rule is appended once (idempotent), and the
    file is written atomically (tmp + rename) so a crash mid-write can't
    half-corrupt the settings every future gate build reads. Returns the
    settings path.
    """
    path = user_settings_path(data_dir)
    data: dict[str, Any] = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, json.JSONDecodeError):
        pass
    block = data.get("permissions")
    if not isinstance(block, dict):
        block = {}
        data["permissions"] = block
    rules = block.get("rules")
    if not isinstance(rules, list):
        rules = []
        block["rules"] = rules
    entry = {"tool": str(tool), "action": "allow"}
    if entry not in rules:
        rules.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".settings-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_name, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return path
