"""Persona system_prompt injection helpers — shared across all channels.

When a channel turn fires with a bound persona (the per-channel
``[channels.{name}.humanlike]`` block is on AND a ``persona_id`` is
chosen), the inbound chat request gets a leading ``role="system"``
message carrying:

* the persona's ``system_prompt`` body (always), and
* a compact ``## Available emoji`` block listing every emoji asset the
  persona has registered (only when the asset store is wired AND the
  persona owns at least one emoji asset).

The emoji block teaches the agent that it can call ``send_attachment``
with a known absolute path to ship a sticker into the conversation. The
block is omitted when no emoji assets exist (or when the asset store
isn't wired at all) so non-Persona-Studio deployments don't see a
misleading "## Available emoji" header followed by an empty list.

## Why a separate module?

The injection logic is shared by ``handle_one_qq``, ``handle_one_telegram``,
``handle_one_discord``, ``handle_one_slack`` and ``handle_one_feishu`` —
extracting it keeps each per-channel handler thin and centralises the
asset-store look-up + best-effort error handling. It also makes the
emoji-block composer trivially unit-testable without standing up a fake
adapter / chat backend.

## Threshold

The ``## Available emoji`` block kicks in as soon as the persona owns
**at least one** ``kind="emoji"`` asset. There's no upper bound here —
the asset store enforces a per-persona byte cap (200 MiB by default),
and the per-asset byte cap (8 MiB) keeps any individual emoji from
ballooning the system prompt. The block is pure label-and-path text,
not embedded base64, so the token cost stays bounded even for personas
with dozens of emoji slots.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

__all__ = [
    "apply_persona_text_model_binding",
    "compose_persona_emoji_block",
    "inject_persona_if_enabled",
    "persona_model_binding",
    "persona_text_model_override",
]


_log = logging.getLogger(__name__)


def persona_model_binding(persona: Any, kind: str) -> tuple[str | None, str | None]:
    """Return ``(provider, model)`` for one persona model-binding kind."""
    bindings = getattr(persona, "model_bindings", None)
    if not isinstance(bindings, Mapping):
        return None, None
    binding = bindings.get(kind)
    provider: Any = None
    model: Any = None
    if isinstance(binding, Mapping):
        provider = binding.get("provider")
        model = binding.get("model")
    else:
        provider = getattr(binding, "provider", None)
        model = getattr(binding, "model", None)

    clean_provider = provider.strip() if isinstance(provider, str) else None
    clean_model = model.strip() if isinstance(model, str) else None
    return clean_provider or None, clean_model or None


def persona_text_model_override(persona: Any) -> str | None:
    """Return a persona's text-model override, if configured.

    Persona Studio persists model routing as ``model_bindings``:
    ``{"text": {"provider": "...", "model": "..."}, ...}``. The
    current internal chat request only has a ``model`` field, so the
    runtime can apply the selected text model immediately while keeping
    the selected provider stored for provider-aware routing later.
    """
    _provider, model = persona_model_binding(persona, "text")
    return model


def apply_persona_text_model_binding(request: Any, persona: Any) -> None:
    """Apply the persona's text-model override to a mutable request."""
    provider, model = persona_model_binding(persona, "text")
    if model is None:
        return
    try:
        request.model = model
        if provider is not None:
            request.provider_hint = provider
    except Exception as exc:  # noqa: BLE001 — model routing is best-effort
        _log.warning("persona_inject: model override failed: %s", exc)


async def compose_persona_emoji_block(
    persona_id: str,
    asset_store: Any | None,
) -> str | None:
    """Render the ``## Available emoji`` block for ``persona_id``.

    Returns ``None`` when:

    * ``asset_store`` is ``None`` (no Persona Studio asset layer wired —
      e.g. legacy deploys), OR
    * the persona owns no ``kind="emoji"`` assets.

    Returning ``None`` (rather than an empty string with just the
    header) is deliberate: callers append the block verbatim, and a
    bare ``## Available emoji`` header with no entries would mislead
    the model into believing emoji exist that don't.

    On store failure (corrupt sqlite, missing table) the function logs
    a warning and returns ``None`` — the persona injection MUST keep
    working without the emoji extension so a broken asset store can't
    silence the bot.

    The returned block is plain Markdown — channels prepend it to the
    persona's ``system_prompt`` body. Lines look like::

        ## Available emoji
        You can send these by calling `send_attachment` with the listed
        path. Use them sparingly to add character flavour. Example:
        when expressing joy, call `send_attachment` with the `happy`
        emoji's path.

        - happy: /abs/path/to/<sha256>.png
        - angry: /abs/path/to/<sha256>.png

    Paths are absolute (``PersonaAssetStore.path_for(record)`` returns
    an absolute :class:`~pathlib.Path` keyed by the blob sha256).
    """
    if asset_store is None:
        return None
    try:
        records = await asset_store.list(persona_id, kind="emoji")
    except Exception as exc:  # noqa: BLE001 — never let asset I/O kill chat
        _log.warning(
            "persona_inject: emoji list failed persona=%s err=%s",
            persona_id,
            exc,
        )
        return None
    if not records:
        return None

    lines: list[str] = [
        "## Available emoji",
        (
            "You can send these by calling `send_attachment` with the "
            "listed path. Use them sparingly to add character flavour. "
            "Example: when expressing joy, call `send_attachment` with "
            "the `happy` emoji's path."
        ),
        "",
    ]
    for record in records:
        try:
            path = asset_store.path_for(record)
        except Exception as exc:  # noqa: BLE001 — defensive
            _log.warning(
                "persona_inject: path_for failed persona=%s label=%s err=%s",
                persona_id,
                getattr(record, "label", "?"),
                exc,
            )
            continue
        label = getattr(record, "label", "") or ""
        if not label:
            continue
        lines.append(f"- {label}: {path}")
    # Guard against the rare case where every record's path_for raised:
    # treat that as "no usable emoji" and skip the block entirely.
    if len(lines) == 3:  # header + intro + blank, no entries
        return None
    return "\n".join(lines)


async def inject_persona_if_enabled(
    request: Any,
    *,
    humanlike_enabled: bool,
    persona_id: str | None,
    persona_store: Any | None,
    humanlike_resolver: Any | None = None,
    asset_store: Any | None = None,
    channel_name: str = "channel",
) -> None:
    """Prepend a persona ``role="system"`` message to ``request.messages``.

    The injected body is the persona's ``system_prompt`` plus (when
    available) the ``## Available emoji`` block produced by
    :func:`compose_persona_emoji_block`.

    Resolution order:

    1. When ``humanlike_resolver`` is callable, its return value
       ``(enabled, persona_id)`` overrides the static
       ``humanlike_enabled`` / ``persona_id`` arguments. This is what
       lets an admin PUT to ``/admin/channels/{channel}/humanlike``
       take effect on the next inbound message without restarting the
       channel task — the resolver re-reads the live config dict that
       the route mutates.
    2. Otherwise the static fields are used.

    Silently no-ops when any of these hold:

    * the gate is off,
    * ``persona_id`` is None or empty,
    * ``persona_store`` is None,
    * the persona row is missing or has empty ``system_prompt``.

    Best-effort: any store / resolver failure logs a warning and returns
    without touching ``request`` — persona is decorative; chat must keep
    working when it breaks. ``channel_name`` only affects the log line
    so operators can tell which channel's resolver misbehaved.
    """
    enabled = bool(humanlike_enabled)
    resolved_persona_id = persona_id
    if callable(humanlike_resolver):
        try:
            resolved = humanlike_resolver()
            if isinstance(resolved, tuple) and len(resolved) == 2:
                enabled = bool(resolved[0])
                resolved_persona_id = (
                    resolved[1] if isinstance(resolved[1], str) else None
                )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "%s humanlike resolver failed: %s", channel_name, exc
            )
    if not enabled or not resolved_persona_id:
        return
    if persona_store is None:
        return
    try:
        persona = await persona_store.get(resolved_persona_id)
    except Exception as exc:  # noqa: BLE001
        _log.warning("%s persona lookup failed: %s", channel_name, exc)
        return
    if persona is None:
        return
    apply_persona_text_model_binding(request, persona)
    body = getattr(persona, "system_prompt", "") or ""
    if not body.strip():
        return

    emoji_block = await compose_persona_emoji_block(
        resolved_persona_id, asset_store
    )
    if emoji_block:
        # Sandwich the persona body and the emoji block with a horizontal
        # rule so the model sees a clear "your voice" / "your sticker
        # pack" split even when the persona body itself uses ## headers.
        content = body + "\n\n" + emoji_block + "\n\n---\n"
    else:
        content = body + "\n\n---\n"

    sys_msg = SimpleNamespace(role="system", content=content)
    request.messages = [sys_msg, *list(request.messages)]
    request.persona_id = resolved_persona_id
