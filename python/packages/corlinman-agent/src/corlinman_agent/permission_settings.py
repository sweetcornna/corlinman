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
3. ``CORLINMAN_AGENT_PERMISSIONS`` — the env layer, still the final word
   so existing deployments keep their exact behaviour.

File schema (everything optional, parsed tolerantly)::

    {"permissions": {"rules": [{"tool": ..., "action": ...}, ...],
                     "mode": "default|acceptEdits|plan|bypass",
                     "strict": false}}

``mode`` / ``strict`` follow env > project > user precedence. When NO
settings file contributes anything the builder returns
``PermissionGate.from_env()`` verbatim — a settings-less deployment is
byte-identical to the pre-E1 behaviour (including its first-match-wins
default). With file layers present the gate stacks last-match-wins so a
later (more specific) layer's rule overrides an earlier one; an explicit
``CORLINMAN_AGENT_PERMISSION_LAST_MATCH_WINS`` env var still wins either
way.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import structlog

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
    """Build the production gate from settings files + environment.

    The env layer keeps its historical final word: env rules stack last,
    and an env-declared ``mode`` / ``strict`` overrides any file's. With
    no settings file contributing anything this returns
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

    strict_env = os.environ.get("CORLINMAN_AGENT_STRICT_MODE", "").strip().lower()
    if strict_env:
        strict = strict_env in ("1", "true", "yes", "on")
    elif proj_strict is not None:
        strict = proj_strict
    elif user_strict is not None:
        strict = user_strict
    else:
        strict = False

    mode_env = os.environ.get("CORLINMAN_AGENT_PERMISSION_MODE", "").strip()
    mode = PermissionMode.coerce(mode_env or proj_mode or user_mode or "")

    lmw_env = (
        os.environ.get("CORLINMAN_AGENT_PERMISSION_LAST_MATCH_WINS", "")
        .strip()
        .lower()
    )
    # Layer precedence (project beats user, env beats both) NEEDS
    # last-match-wins; only an explicit env opt-out flips it.
    last_match_wins = lmw_env in ("1", "true", "yes", "on") if lmw_env else True

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

    Backs the console's persist answer: the settings file is read (a
    corrupt file is replaced rather than crashing the grant), the rule is
    appended once (idempotent), and the file is written atomically
    (tmp + rename) so a crash mid-write can't half-corrupt the settings
    every future gate build reads. Returns the settings path.
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
