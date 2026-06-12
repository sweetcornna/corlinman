"""Channels-side shim over :mod:`corlinman_server.binding_prefs_store`.

The store lives in corlinman-server (a soft dep of corlinman-channels —
channels stay importable standalone, same arrangement as the
``/sethome`` → ``home_channel_store`` handler). Every helper here is
**fail-open**: when the server package (or the SQLite file) is
unavailable the caller's defaults pass through untouched, so a stripped
deployment loses ``/new`` + ``/model`` but never a message.

Consumed from two places:

* the command handlers (``/new`` / ``/model`` in ``commands.py``) —
  writes;
* the request builders (``service._build_internal_request`` /
  ``service._build_text_channel_request``) — reads, applying the model
  override and folding the session epoch into the derived session key.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from corlinman_channels.common import ChannelBinding

__all__ = [
    "bump_session_epoch",
    "effective_model",
    "effective_persona_id",
    "effective_session_key",
    "get_prefs",
    "set_model_override",
    "set_persona_id",
]

log = logging.getLogger(__name__)


def _store() -> Any | None:
    try:
        from corlinman_server import binding_prefs_store  # noqa: PLC0415 — soft dep
    except ImportError:
        return None
    return binding_prefs_store


def get_prefs(binding: ChannelBinding) -> Any | None:
    """Read prefs for ``binding``; ``None`` when the store is unavailable."""
    store = _store()
    if store is None:
        return None
    try:
        return store.get_prefs(
            binding.channel, binding.account, binding.thread, binding.sender
        )
    except Exception as exc:  # noqa: BLE001 — prefs are best-effort
        log.warning("binding_prefs.read_failed err=%s", exc)
        return None


def set_model_override(binding: ChannelBinding, model: str | None) -> Any | None:
    """Set/clear the model override; ``None`` result = store unavailable."""
    store = _store()
    if store is None:
        return None
    try:
        return store.set_model_override(
            binding.channel,
            binding.account,
            binding.thread,
            binding.sender,
            model,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("binding_prefs.write_failed err=%s", exc)
        return None


def set_persona_id(binding: ChannelBinding, persona_id: str | None) -> Any | None:
    """Set/clear the persona override; ``None`` result = store unavailable."""
    store = _store()
    if store is None:
        return None
    try:
        return store.set_persona_id(
            binding.channel,
            binding.account,
            binding.thread,
            binding.sender,
            persona_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("binding_prefs.persona_write_failed err=%s", exc)
        return None


def bump_session_epoch(binding: ChannelBinding) -> Any | None:
    """``/new`` — returns the new prefs, or ``None`` when unavailable."""
    store = _store()
    if store is None:
        return None
    try:
        return store.bump_session_epoch(
            binding.channel,
            binding.account,
            binding.thread,
            binding.sender,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("binding_prefs.bump_failed err=%s", exc)
        return None


def effective_model(binding: ChannelBinding | None, default: str) -> str:
    """The model the binding's turns should use: override or ``default``."""
    if binding is None:
        return default
    prefs = get_prefs(binding)
    override = getattr(prefs, "model_override", None) if prefs else None
    return override if isinstance(override, str) and override else default


def effective_persona_id(
    binding: ChannelBinding | None, default: str | None
) -> str | None:
    """The persona this binding should speak as: override or ``default``."""
    if binding is None:
        return default
    prefs = get_prefs(binding)
    override = getattr(prefs, "persona_id", None) if prefs else None
    return override if isinstance(override, str) and override else default


def effective_session_key(binding: ChannelBinding | None, base_key: str) -> str:
    """Fold the binding's session epoch into ``base_key``.

    Epoch 0 (the default, and the fail-open path) returns ``base_key``
    unchanged so every pre-existing conversation keeps its session key —
    the epoch suffix only appears after the user's first ``/new``.
    """
    if binding is None:
        return base_key
    prefs = get_prefs(binding)
    epoch = int(getattr(prefs, "session_epoch", 0) or 0) if prefs else 0
    return f"{base_key}:e{epoch}" if epoch > 0 else base_key
