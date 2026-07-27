"""Agent-runtime knobs, applied from the ``[agent_runtime]`` config block.

Every value here used to be readable *only* from a ``CORLINMAN_*``
environment variable, which in a native deployment means it was not
readable at all. The agent's systemd unit deliberately carries no
``EnvironmentFile`` — it sets exactly ``HOME`` /
``CORLINMAN_EXECUTION_STATE_DIR`` / ``CORLINMAN_PY_CONFIG`` /
``CORLINMAN_PY_SOCKET`` — and ``ChatStart`` carries no config sections. So
the agent process sees only what ``py-config.json`` renders, and every
knob below sat pinned at its built-in default with no way to change it.
Same failure shape as the ``[voice]`` incident in v1.39.0, just spread
across two dozen switches instead of one.

Precedence, highest first:

1. **these defaults** (what the operator wrote in ``config.toml``);
2. the ``CORLINMAN_*`` environment variable;
3. the built-in default.

Config sits above env deliberately, matching
:mod:`corlinman_agent.voice.defaults` and :mod:`corlinman_agent.web.defaults`:
the env vars predate the config section, and a value an operator set must
not be silently overridden by a stale export on the host.

Read at call time, never at import
----------------------------------
Every resolver below is a **function**, and callers must invoke it per use
rather than binding the result to a module constant. The sidecar is loaded
*after* the agent's modules are imported, so an import-time
``os.environ.get(...)`` freezes the built-in default before configuration
ever arrives — the config lands in this module and changes nothing. That
is exactly the trap ``reasoning_loop`` was in before this module existed.
Clamping lives here too, so a knob cannot mean one thing when it comes
from config and another when it comes from env.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

__all__ = [
    "AgentRuntimeDefaults",
    "agent_runtime_defaults_from_config",
    "apply_agent_runtime_config",
    "compact_summary_breaker_limit",
    "compact_summary_cooldown_rounds",
    "compact_summary_threshold",
    "context_budget_override",
    "context_reserve_cap",
    "context_reserve_fraction",
    "context_reserve_tokens",
    "execute_code_enabled",
    "get_agent_runtime_defaults",
    "mailbox_maxsize",
    "max_rounds",
    "python_executable",
    "require_read_before_edit",
    "reset_agent_runtime_defaults",
    "sandbox_backend",
    "sandbox_image",
    "sandbox_user",
    "set_agent_runtime_defaults",
    "shell_task_max_lifetime_s",
    "shell_task_max_log_bytes",
    "shell_task_read_max_bytes",
    "shell_tasks_max",
    "skill_refresh_interval_ms",
    "strict_mode_enabled",
    "tool_result_cap",
    "tool_result_spill",
    "turn_output_budget",
    "web_fetch_allow_private",
]


@dataclass(frozen=True, slots=True)
class AgentRuntimeDefaults:
    """Operator-chosen values. ``None`` everywhere means "not configured".

    ``None`` rather than the built-in default is load-bearing: it is what
    lets the env layer still apply for a knob the operator left alone,
    instead of the config block silently pinning all two dozen values the
    moment anyone sets one of them.
    """

    # -- reasoning loop budgets ------------------------------------------
    max_rounds: int | None = None
    tool_result_cap: int | None = None
    tool_result_spill: int | None = None
    turn_output_budget: int | None = None
    context_budget: int | None = None
    context_reserve_fraction: float | None = None
    context_reserve_cap: int | None = None
    context_reserve_tokens: int | None = None
    compact_summary_threshold: float | None = None
    compact_summary_cooldown_rounds: int | None = None
    compact_summary_breaker_limit: int | None = None
    # -- background shell tasks ------------------------------------------
    shell_tasks_max: int | None = None
    shell_task_max_lifetime_s: int | None = None
    shell_task_max_log_bytes: int | None = None
    shell_task_read_max_bytes: int | None = None
    # -- misc runtime ----------------------------------------------------
    mailbox_maxsize: int | None = None
    skill_refresh_interval_ms: int | None = None
    # -- capability switches ---------------------------------------------
    enable_execute_code: bool | None = None
    web_fetch_allow_private: bool | None = None
    require_read_before_edit: bool | None = None
    strict_mode: bool | None = None
    sandbox_backend: str = ""
    sandbox_image: str = ""
    sandbox_user: str = ""
    python: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Only the values actually configured — feeds structured logs."""
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if value is None or value == "":
                continue
            out[f.name] = value
        return out


_DEFAULTS = AgentRuntimeDefaults()
_LOCK = threading.RLock()


def get_agent_runtime_defaults() -> AgentRuntimeDefaults:
    with _LOCK:
        return _DEFAULTS


def set_agent_runtime_defaults(defaults: AgentRuntimeDefaults) -> None:
    global _DEFAULTS
    with _LOCK:
        _DEFAULTS = defaults


def reset_agent_runtime_defaults() -> None:
    """Restore the unconfigured state (used by tests)."""
    set_agent_runtime_defaults(AgentRuntimeDefaults())


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def _cfg_int(section: Mapping[str, Any], key: str) -> int | None:
    raw = section.get(key)
    # ``bool`` is an ``int`` subclass; a stray ``true`` here means the
    # operator confused two knobs, and silently reading it as 1 would be
    # worse than ignoring it.
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _cfg_float(section: Mapping[str, Any], key: str) -> float | None:
    raw = section.get(key)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _cfg_bool(section: Mapping[str, Any], key: str) -> bool | None:
    raw = section.get(key)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip():
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return None


def _cfg_str(section: Mapping[str, Any], key: str) -> str:
    raw = section.get(key)
    return raw.strip() if isinstance(raw, str) and raw.strip() else ""


def agent_runtime_defaults_from_config(
    section: Mapping[str, Any] | None,
) -> AgentRuntimeDefaults:
    """Parse an ``[agent_runtime]`` block.

    Tolerant by design — a malformed value drops to "unconfigured" instead
    of raising, because this runs on the sidecar-load path where one bad
    key must not take the agent process down.
    """
    if not isinstance(section, Mapping):
        return AgentRuntimeDefaults()
    return AgentRuntimeDefaults(
        max_rounds=_cfg_int(section, "max_rounds"),
        tool_result_cap=_cfg_int(section, "tool_result_cap"),
        tool_result_spill=_cfg_int(section, "tool_result_spill"),
        turn_output_budget=_cfg_int(section, "turn_output_budget"),
        context_budget=_cfg_int(section, "context_budget"),
        context_reserve_fraction=_cfg_float(section, "context_reserve_fraction"),
        context_reserve_cap=_cfg_int(section, "context_reserve_cap"),
        context_reserve_tokens=_cfg_int(section, "context_reserve_tokens"),
        compact_summary_threshold=_cfg_float(section, "compact_summary_threshold"),
        compact_summary_cooldown_rounds=_cfg_int(section, "compact_summary_cooldown_rounds"),
        compact_summary_breaker_limit=_cfg_int(section, "compact_summary_breaker_limit"),
        shell_tasks_max=_cfg_int(section, "shell_tasks_max"),
        shell_task_max_lifetime_s=_cfg_int(section, "shell_task_max_lifetime_s"),
        shell_task_max_log_bytes=_cfg_int(section, "shell_task_max_log_bytes"),
        shell_task_read_max_bytes=_cfg_int(section, "shell_task_read_max_bytes"),
        mailbox_maxsize=_cfg_int(section, "mailbox_maxsize"),
        skill_refresh_interval_ms=_cfg_int(section, "skill_refresh_interval_ms"),
        enable_execute_code=_cfg_bool(section, "enable_execute_code"),
        web_fetch_allow_private=_cfg_bool(section, "web_fetch_allow_private"),
        require_read_before_edit=_cfg_bool(section, "require_read_before_edit"),
        strict_mode=_cfg_bool(section, "strict_mode"),
        sandbox_backend=_cfg_str(section, "sandbox_backend").lower(),
        sandbox_image=_cfg_str(section, "sandbox_image"),
        sandbox_user=_cfg_str(section, "sandbox_user"),
        python=_cfg_str(section, "python"),
    )


def apply_agent_runtime_config(
    section: Mapping[str, Any] | None,
) -> AgentRuntimeDefaults:
    """Apply a whole ``[agent_runtime]`` block.

    The single entry point both the in-process boot path and the agent
    sidecar loader call, so the two can never drift apart.
    """
    defaults = agent_runtime_defaults_from_config(section)
    set_agent_runtime_defaults(defaults)
    return defaults


# ---------------------------------------------------------------------------
# Resolvers — config > env > built-in, clamped once, here
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_int(cfg_value: int | None, env_name: str, default: int, *, floor: int) -> int:
    if cfg_value is not None:
        return max(floor, cfg_value)
    env_value = _env_int(env_name, default)
    if env_value is not None:
        return max(floor, env_value)
    return default


def max_rounds() -> int:
    """Provider rounds allowed in one turn before the loop short-circuits."""
    return _resolve_int(
        get_agent_runtime_defaults().max_rounds,
        "CORLINMAN_AGENT_MAX_ROUNDS",
        60,
        floor=8,
    )


def tool_result_cap() -> int:
    """Per-tool-result character cap for results re-fed to the provider."""
    return _resolve_int(
        get_agent_runtime_defaults().tool_result_cap,
        "CORLINMAN_TOOL_RESULT_CAP",
        8_000,
        floor=1_000,
    )


def tool_result_spill() -> int:
    """Size at which a tool result is spilled to a file instead of inlined."""
    return _resolve_int(
        get_agent_runtime_defaults().tool_result_spill,
        "CORLINMAN_TOOL_RESULT_SPILL",
        65_536,
        floor=16_000,
    )


def turn_output_budget() -> int:
    """Total tool-output characters one turn may inline before spilling."""
    return _resolve_int(
        get_agent_runtime_defaults().turn_output_budget,
        "CORLINMAN_TURN_OUTPUT_BUDGET",
        400_000,
        floor=50_000,
    )


def context_budget_override() -> int | None:
    """Flat compaction budget that PINS every model, or ``None`` to derive.

    Unset is the interesting value here, which is why this resolver returns
    ``None`` rather than a number: without an override the loop sizes the
    budget from the model's own declared context window.
    """
    configured = get_agent_runtime_defaults().context_budget
    if configured is not None:
        # ``0`` is the natural "no override" spelling in a TOML file where
        # every other knob is a positive number.
        return max(8_000, configured) if configured > 0 else None
    raw = os.environ.get("CORLINMAN_CONTEXT_BUDGET")
    if raw is None:
        return None
    try:
        return max(8_000, int(raw))
    except ValueError:
        return None


def context_reserve_fraction() -> float:
    """Fraction of a model's window held back for the response."""
    configured = get_agent_runtime_defaults().context_reserve_fraction
    if configured is not None and configured > 0:
        return configured
    env_value = _env_float("CORLINMAN_CONTEXT_RESERVE_FRACTION")
    if env_value is not None and env_value > 0:
        return env_value
    return 0.15


def context_reserve_cap() -> int:
    """Absolute ceiling on the proportional reserve."""
    configured = get_agent_runtime_defaults().context_reserve_cap
    if configured is not None and configured > 0:
        return configured
    env_value = _env_float("CORLINMAN_CONTEXT_RESERVE_CAP")
    if env_value is not None and env_value > 0:
        return int(env_value)
    return 48_000


def context_reserve_tokens() -> int | None:
    """Fixed reserve (claude-code ``AUTOCOMPACT_BUFFER``), or ``None``."""
    configured = get_agent_runtime_defaults().context_reserve_tokens
    if configured is not None:
        return max(0, configured) if configured > 0 else None
    raw = os.environ.get("CORLINMAN_CONTEXT_RESERVE_TOKENS")
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


def compact_summary_threshold() -> float:
    """Budget fraction at which the summarizing compaction path fires.

    Clamped to ``(0.5, 1.0]``: below that the cheap elision path would never
    run, above it the heavyweight summary would fire on every round.
    """
    configured = get_agent_runtime_defaults().compact_summary_threshold
    value = (
        configured if configured is not None else _env_float("CORLINMAN_COMPACT_SUMMARY_THRESHOLD")
    )
    if value is None or value <= 0.5 or value > 1.0:
        return 0.95
    return value


def compact_summary_cooldown_rounds() -> int:
    """Rounds the slow summarization path is skipped after a failure."""
    return _resolve_int(
        get_agent_runtime_defaults().compact_summary_cooldown_rounds,
        "CORLINMAN_COMPACT_SUMMARY_COOLDOWN_ROUNDS",
        5,
        floor=1,
    )


def compact_summary_breaker_limit() -> int:
    """Consecutive summary failures before the slow path is disabled."""
    return _resolve_int(
        get_agent_runtime_defaults().compact_summary_breaker_limit,
        "CORLINMAN_COMPACT_SUMMARY_BREAKER_LIMIT",
        3,
        floor=1,
    )


def shell_tasks_max() -> int:
    """Concurrent background shell tasks allowed per agent."""
    return _resolve_int(
        get_agent_runtime_defaults().shell_tasks_max,
        "CORLINMAN_SHELL_TASKS_MAX",
        8,
        floor=1,
    )


def shell_task_max_lifetime_s() -> float:
    """Wall-clock ceiling on one background shell task, in seconds.

    Floored just above zero rather than at 1: a misconfigured ``0`` must
    not disable the watchdog (or divide-by-zero a caller), but sub-second
    lifetimes are legitimate in tests.
    """
    configured = get_agent_runtime_defaults().shell_task_max_lifetime_s
    if configured is not None:
        return max(0.1, float(configured))
    env_value = _env_float("CORLINMAN_SHELL_TASK_MAX_LIFETIME_S")
    if env_value is not None:
        return max(0.1, env_value)
    return 1_800.0


def shell_task_max_log_bytes() -> int:
    """Spill-file size cap for one background shell task."""
    return _resolve_int(
        get_agent_runtime_defaults().shell_task_max_log_bytes,
        "CORLINMAN_SHELL_TASK_MAX_LOG_BYTES",
        16 * 1024 * 1024,
        floor=4_096,
    )


def shell_task_read_max_bytes() -> int:
    """Per-read window returned from a background task's spill file."""
    return _resolve_int(
        get_agent_runtime_defaults().shell_task_read_max_bytes,
        "CORLINMAN_SHELL_TASK_READ_MAX_BYTES",
        65_536,
        floor=4_096,
    )


def mailbox_maxsize() -> int:
    """Bound on a subagent mailbox before sends block.

    An unbounded queue is never allowed here — that is the bug the bound
    exists to fix — so a non-positive value falls back to the default
    rather than being honoured.
    """
    return _resolve_int(
        get_agent_runtime_defaults().mailbox_maxsize,
        "CORLINMAN_MAILBOX_MAXSIZE",
        1_024,
        floor=1,
    )


def skill_refresh_interval_ms() -> int:
    """Debounce window for skill-directory rescans (``0`` disables it)."""
    configured = get_agent_runtime_defaults().skill_refresh_interval_ms
    if configured is not None:
        return max(0, configured)
    raw = os.environ.get("CORLINMAN_SKILL_REFRESH_INTERVAL_MS")
    if raw is None or raw == "":
        return 30_000
    try:
        return max(0, int(raw))
    except ValueError:
        return 30_000


def execute_code_enabled() -> bool:
    """Whether the ``execute_code`` REPL tool is available at all."""
    configured = get_agent_runtime_defaults().enable_execute_code
    if configured is not None:
        return configured
    return _env_bool("CORLINMAN_ENABLE_EXECUTE_CODE") or False


def web_fetch_allow_private() -> bool:
    """Whether ``web_fetch`` may reach private/loopback addresses."""
    configured = get_agent_runtime_defaults().web_fetch_allow_private
    if configured is not None:
        return configured
    return _env_bool("CORLINMAN_WEB_FETCH_ALLOW_PRIVATE") or False


def require_read_before_edit() -> bool:
    """Whether an edit must be preceded by a read of the same file."""
    configured = get_agent_runtime_defaults().require_read_before_edit
    if configured is not None:
        return configured
    env_value = _env_bool("CORLINMAN_REQUIRE_READ_BEFORE_EDIT")
    return True if env_value is None else env_value


def strict_mode_enabled() -> bool:
    """Whether every mutating tool call needs explicit approval."""
    configured = get_agent_runtime_defaults().strict_mode
    if configured is not None:
        return configured
    return _env_bool("CORLINMAN_AGENT_STRICT_MODE") or False


def sandbox_backend() -> str:
    """Sandbox backend id for the agent's own shell (``local``/``docker``)."""
    configured = get_agent_runtime_defaults().sandbox_backend
    if configured:
        return configured
    return (os.environ.get("CORLINMAN_SANDBOX_BACKEND") or "").strip().lower()


def sandbox_image() -> str:
    """Container image used when the sandbox backend is ``docker``."""
    return get_agent_runtime_defaults().sandbox_image or os.environ.get(
        "CORLINMAN_SANDBOX_IMAGE", ""
    )


def sandbox_user() -> str:
    """UID/GID the sandbox container runs as."""
    return get_agent_runtime_defaults().sandbox_user or os.environ.get("CORLINMAN_SANDBOX_USER", "")


def python_executable() -> str:
    """Interpreter used by ``execute_code`` (empty → ``sys.executable``)."""
    return get_agent_runtime_defaults().python or os.environ.get("CORLINMAN_PYTHON", "")
