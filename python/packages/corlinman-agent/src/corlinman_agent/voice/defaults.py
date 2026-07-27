"""Process-wide voice defaults, applied from the ``[voice]`` config block.

The ``text_to_speech`` tool runs inside the **agent** process, which never
sees the gateway's config snapshot — it learns about providers through the
``py-config.json`` sidecar. Without this module the ``[voice]`` block would
only reach the admin preview route, so an operator could pick a backend and
voice in the UI, hear it in the audition, and still have channels send the
built-in default. Config that does not take effect is worse than no config
at all.

:func:`set_voice_defaults` is called on the sidecar-load path (and at
in-process boot) and is idempotent; :func:`synthesize` consults it between
the caller's own arguments and the environment variables.

Precedence, highest first:

1. tool-call arguments (the model asked for a specific voice);
2. persona/provider params (a persona bound its own voice);
3. **these defaults** (what the operator set in the UI);
4. ``CORLINMAN_TTS_*`` environment variables;
5. the backend's built-in default.

Config sits above the env vars deliberately: the env vars predate the
settings page, and an operator changing a value in the UI must not be
silently overridden by a stale export on the host.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

__all__ = [
    "VoiceDefaults",
    "get_voice_defaults",
    "reset_voice_defaults",
    "set_voice_defaults",
    "voice_defaults_from_config",
]


@dataclass(frozen=True, slots=True)
class VoiceDefaults:
    """Operator-chosen defaults; empty strings mean "not configured"."""

    enabled: bool = True
    backend: str = ""
    voice: str = ""
    model: str = ""
    fmt: str = ""
    instructions: str = ""
    speed: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "backend": self.backend,
            "voice": self.voice,
            "model": self.model,
            "format": self.fmt,
            "instructions": self.instructions,
            "speed": self.speed,
        }


_DEFAULTS = VoiceDefaults()
_LOCK = threading.RLock()


def get_voice_defaults() -> VoiceDefaults:
    with _LOCK:
        return _DEFAULTS


def set_voice_defaults(defaults: VoiceDefaults) -> None:
    global _DEFAULTS
    with _LOCK:
        _DEFAULTS = defaults


def reset_voice_defaults() -> None:
    """Restore the unconfigured state (used by tests)."""
    set_voice_defaults(VoiceDefaults())


def _clean_str(raw: Any) -> str:
    return raw.strip() if isinstance(raw, str) and raw.strip() else ""


def voice_defaults_from_config(section: Mapping[str, Any] | None) -> VoiceDefaults:
    """Parse a ``[voice]`` config block into :class:`VoiceDefaults`.

    Tolerant by design — a malformed value falls back to "unconfigured"
    rather than raising, because this runs on the sidecar-load path where
    a bad block must not take the agent process down.
    """
    if not isinstance(section, Mapping):
        return VoiceDefaults()
    raw_speed = section.get("speed")
    speed = float(raw_speed) if isinstance(raw_speed, (int, float)) else None
    return VoiceDefaults(
        enabled=bool(section.get("enabled", True)),
        backend=_clean_str(section.get("backend")),
        voice=_clean_str(section.get("voice")),
        model=_clean_str(section.get("model")),
        fmt=_clean_str(section.get("format")),
        instructions=_clean_str(section.get("instructions")),
        speed=speed,
    )


def apply_voice_config(section: Mapping[str, Any] | None) -> VoiceDefaults:
    """Apply a whole ``[voice]`` block: custom backends + defaults.

    The single entry point both the in-process boot path and the agent
    sidecar loader call, so the two can never drift apart.
    """
    from corlinman_agent.voice.catalog import register_backends_from_config

    backends = section.get("backends") if isinstance(section, Mapping) else None
    register_backends_from_config(backends if isinstance(backends, Mapping) else None)
    defaults = voice_defaults_from_config(section)
    set_voice_defaults(defaults)
    return defaults


def merged(**overrides: Any) -> VoiceDefaults:
    """Return the current defaults with ``overrides`` applied (test seam)."""
    return replace(get_voice_defaults(), **overrides)
