"""The ``[permissions]`` config shape layer (audit W3-1).

Same paradigm as :mod:`corlinman_agent.runtime_defaults` — and for the same
reason: the agent's systemd unit carries no ``EnvironmentFile`` and
``ChatStart`` carries no config sections, so the ``py-config.json`` sidecar
is the ONLY channel through which an operator's ``[permissions]`` block can
reach the agent process. The block is applied here (a process-global,
lock-guarded singleton) and **read at call time** by
:class:`~corlinman_agent.authz.gate.AuthzGate` — never frozen into a gate
at construction. That is the whole point of W3-1: the old
``PermissionGate`` froze its rules/strict/mode in ``__init__`` and was
built exactly once per process, so no configuration change could ever take
effect without a restart (fact M5 in the design plan).

Precedence, highest first (decision C5 of the plan):

1. the ``[permissions]`` config block (this module);
2. the legacy ``CORLINMAN_AGENT_*`` environment variables;
3. the project settings file (``<cwd>/.corlinman/settings.local.json``);
4. the user settings file (``<data_dir>/settings.json``);
5. the built-in default.

``None`` everywhere means "not configured" — that is what lets the env /
file layers still apply per knob instead of the config block pinning
everything the moment one key is set.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from typing import Any

from corlinman_agent.authz.model import _VALID_ACTIONS

__all__ = [
    "PermissionsDefaults",
    "apply_permissions_config",
    "generation",
    "get_permissions_defaults",
    "permissions_defaults_from_config",
    "reset_permissions_defaults",
    "resolve_default_action",
    "resolve_last_match_wins",
    "resolve_mode",
    "resolve_strict",
]

_TRUTHY = {"1", "true", "yes", "on"}


class PermissionsDefaults:
    """Operator-chosen ``[permissions]`` values. ``None`` = "not configured".

    A plain class (not a dataclass) because ``rules`` holds unhashable
    dicts; equality is by field value so the gate's snapshot cache can key
    on the generation counter instead.
    """

    __slots__ = ("default_action", "last_match_wins", "mode", "rules", "strict")

    def __init__(
        self,
        *,
        mode: str | None = None,
        strict: bool | None = None,
        default_action: str | None = None,
        last_match_wins: bool | None = None,
        rules: tuple[dict[str, Any], ...] | None = None,
    ) -> None:
        self.mode = mode
        self.strict = strict
        self.default_action = default_action
        self.last_match_wins = last_match_wins
        self.rules = rules

    def as_dict(self) -> dict[str, Any]:
        """Only the values actually configured — feeds structured logs.

        ``rules`` is reported as a COUNT, not the full list, so the
        apply-time log line stays one line.
        """
        out: dict[str, Any] = {}
        if self.mode is not None:
            out["mode"] = self.mode
        if self.strict is not None:
            out["strict"] = self.strict
        if self.default_action is not None:
            out["default_action"] = self.default_action
        if self.last_match_wins is not None:
            out["last_match_wins"] = self.last_match_wins
        if self.rules is not None:
            out["rules"] = len(self.rules)
        return out


_DEFAULTS = PermissionsDefaults()
_GENERATION = 0
_LOCK = threading.RLock()


def get_permissions_defaults() -> PermissionsDefaults:
    with _LOCK:
        return _DEFAULTS


def generation() -> int:
    """Monotonic write counter — the gate's snapshot-cache key.

    Bumped on every :func:`apply_permissions_config` /
    :func:`reset_permissions_defaults` so a gate can cheaply detect "config
    unchanged, reuse the compiled matcher" without deep-comparing rule
    lists on every tool call.
    """
    with _LOCK:
        return _GENERATION


def _set(defaults: PermissionsDefaults) -> None:
    global _DEFAULTS, _GENERATION
    with _LOCK:
        _DEFAULTS = defaults
        _GENERATION += 1


def reset_permissions_defaults() -> None:
    """Restore the unconfigured state (used by tests)."""
    _set(PermissionsDefaults())


# ---------------------------------------------------------------------------
# Config parsing — tolerant by design (sidecar-load path; a bad value must
# degrade to "unconfigured", never take the agent process down)
# ---------------------------------------------------------------------------


def _cfg_bool(section: Mapping[str, Any], key: str) -> bool | None:
    raw = section.get(key)
    # ``bool`` is an ``int`` subclass — check it FIRST so an int can never
    # masquerade as a flag (and vice versa).
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip():
        return raw.strip().lower() in _TRUTHY
    return None


def _cfg_str(section: Mapping[str, Any], key: str) -> str | None:
    raw = section.get(key)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _cfg_rules(section: Mapping[str, Any]) -> tuple[dict[str, Any], ...] | None:
    raw = section.get("rules")
    if not isinstance(raw, list):
        return None
    # Keep raw dicts (the JSON wire shape) — validation happens in
    # ``parse_rule_list`` so config / env / file rules share one parser.
    return tuple(entry for entry in raw if isinstance(entry, dict))


def permissions_defaults_from_config(
    section: Mapping[str, Any] | None,
) -> PermissionsDefaults:
    """Parse a ``[permissions]`` block. Tolerant — see module docstring."""
    if not isinstance(section, Mapping):
        return PermissionsDefaults()
    default_action = _cfg_str(section, "default_action")
    if default_action is not None and default_action not in _VALID_ACTIONS:
        default_action = None
    return PermissionsDefaults(
        mode=_cfg_str(section, "mode"),
        strict=_cfg_bool(section, "strict"),
        default_action=default_action,
        last_match_wins=_cfg_bool(section, "last_match_wins"),
        rules=_cfg_rules(section),
    )


def apply_permissions_config(
    section: Mapping[str, Any] | None,
) -> PermissionsDefaults:
    """Apply a whole ``[permissions]`` block.

    The single entry point both the in-process boot path
    (``app_factory._apply_agent_side_config``) and the agent sidecar loader
    (``main._apply_agent_config_from_sidecar``) call, so the two can never
    drift apart.
    """
    defaults = permissions_defaults_from_config(section)
    _set(defaults)
    return defaults


# ---------------------------------------------------------------------------
# Resolvers — config > env > project file > user file > built-in.
# Read at call time; the file-layer contributions are passed in by the gate
# (which owns the file reads + their mtime caching).
# ---------------------------------------------------------------------------


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() in _TRUTHY


def resolve_strict(
    project_strict: bool | None = None,
    user_strict: bool | None = None,
) -> bool:
    """The ONE strict-mode precedence chain (dedup of the two old copies).

    ``[permissions].strict`` > ``[agent_runtime].strict_mode`` >
    ``$CORLINMAN_AGENT_STRICT_MODE`` > project settings file > user
    settings file > ``False``. Replaces both
    ``runtime_defaults.strict_mode_enabled``'s chain (which had no file
    layer) and ``permission_settings.build_permission_gate``'s inline copy.
    """
    configured = get_permissions_defaults().strict
    if configured is not None:
        return configured
    # Lazy import — runtime_defaults delegates its own strict resolver back
    # here, so a module-level import either way would be a cycle.
    from corlinman_agent import runtime_defaults as _limits  # noqa: PLC0415

    legacy_cfg = _limits.get_agent_runtime_defaults().strict_mode
    if legacy_cfg is not None:
        return legacy_cfg
    env_value = _env_bool("CORLINMAN_AGENT_STRICT_MODE")
    if env_value is not None:
        return env_value
    if project_strict is not None:
        return project_strict
    if user_strict is not None:
        return user_strict
    return False


def resolve_mode(
    project_mode: str | None = None,
    user_mode: str | None = None,
) -> str:
    """Raw mode string per the C5 chain (caller coerces via PermissionMode)."""
    configured = get_permissions_defaults().mode
    if configured is not None:
        return configured
    env_value = os.environ.get("CORLINMAN_AGENT_PERMISSION_MODE", "").strip()
    if env_value:
        return env_value
    if project_mode:
        return project_mode
    if user_mode:
        return user_mode
    return ""


def resolve_last_match_wins() -> bool:
    """C3: last-match-wins is the ONE default; env opt-out still honoured."""
    configured = get_permissions_defaults().last_match_wins
    if configured is not None:
        return configured
    env_value = _env_bool("CORLINMAN_AGENT_PERMISSION_LAST_MATCH_WINS")
    if env_value is not None:
        return env_value
    return True


def resolve_default_action() -> str:
    """The gate's fallback action when nothing else decided (default allow)."""
    configured = get_permissions_defaults().default_action
    if configured is not None:
        return configured
    return "allow"
