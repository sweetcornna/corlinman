"""Process-wide image-generation defaults from ``[models]``.

The mirror of :mod:`corlinman_agent.voice.defaults`, and it exists for the
same reason: ``image_generate`` resolves its provider inside the **agent**
process, which never sees the gateway config snapshot. Until an operator
bound a persona, the only way to steer image generation was the chat
provider fallback — there was no global "use this model for images"
setting at all, so the model hub had nothing to show.

Precedence, highest first:

1. the persona's ``image`` model binding (most specific);
2. **these defaults** (``[models].image_model`` / ``image_provider``);
3. the active chat provider (historical fallback).
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ImageDefaults",
    "get_image_defaults",
    "image_defaults_from_config",
    "reset_image_defaults",
    "set_image_defaults",
]


@dataclass(frozen=True, slots=True)
class ImageDefaults:
    """Operator-chosen image binding; empty means "not configured"."""

    provider: str = ""
    model: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.model or self.provider)

    def as_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model}


_DEFAULTS = ImageDefaults()
_LOCK = threading.RLock()


def get_image_defaults() -> ImageDefaults:
    with _LOCK:
        return _DEFAULTS


def set_image_defaults(defaults: ImageDefaults) -> None:
    global _DEFAULTS
    with _LOCK:
        _DEFAULTS = defaults


def reset_image_defaults() -> None:
    set_image_defaults(ImageDefaults())


def _clean(raw: Any) -> str:
    return raw.strip() if isinstance(raw, str) and raw.strip() else ""


def image_defaults_from_config(models_section: Mapping[str, Any] | None) -> ImageDefaults:
    """Parse ``[models].image_model`` / ``image_provider``.

    Tolerant: a malformed block yields "unconfigured" rather than raising,
    because this runs on the sidecar-load path.
    """
    if not isinstance(models_section, Mapping):
        return ImageDefaults()
    return ImageDefaults(
        provider=_clean(models_section.get("image_provider")),
        model=_clean(models_section.get("image_model")),
    )


def apply_image_config(models_section: Mapping[str, Any] | None) -> ImageDefaults:
    """Install the ``[models]`` image binding for this process."""
    defaults = image_defaults_from_config(models_section)
    set_image_defaults(defaults)
    return defaults
