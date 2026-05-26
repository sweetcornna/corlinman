"""``corlinman_server.gateway.services`` sibling-``bootstrap`` seam.

Parcel **P2** of the Python-port runtime-completion plan
(``docs/PLAN_PORT_COMPLETION.md`` §3, Wave 1). See
``docs/contracts/runtime-wiring.md`` §2 for the seam contract.

The gateway lifespan (``gateway/lifecycle/entrypoint.py``) iterates a
fixed list of sibling modules and, for each, calls an optional
``bootstrap(state)``. ``corlinman_server.gateway.services`` is one of
those siblings; this module is the body of its ``bootstrap``. It is
re-exported from ``gateway/services/__init__.py`` as the package's
``bootstrap`` symbol so the entrypoint seam's
``getattr(services_module, "bootstrap")`` resolves to it. (The module
is named ``chat_bootstrap`` rather than ``bootstrap`` so the dotted
submodule name does not collide with that re-exported function.)

``bootstrap(state)`` here is the **P2 chat-service** wiring: it builds a
:class:`~corlinman_server.gateway.services.chat_service.ChatService`
around a :class:`~corlinman_server.gateway.services.direct_backend.DirectProviderBackend`
and attaches it to ``state.chat``. P3 (channel runtime) extends this
seam — see :func:`bootstrap` for the documented extension point.

Why the direct backend?
-----------------------

Two backends implement the ``ChatBackend`` protocol:

* :class:`DirectProviderBackend` (P2) — calls :mod:`corlinman_providers`
  straight, no agent, no tools. Fast path; this is what
  :func:`bootstrap` wires by default.
* ``GrpcAgentChatBackend`` (P4) — dials the full Python agent over
  gRPC (tools / skills / memory).

The gateway picks one per deployment. Until P4's gRPC agent server is
running, the direct backend is the only one that yields a real
completion, so :func:`bootstrap` selects it whenever
``state.provider_registry`` is populated.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Any, TypeVar

__all__ = [
    "MessageLike",
    "apply_command_substitution",
    "bootstrap",
    "build_chat_service",
    "rewrite_trailing_user_message",
]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Slash-command substitution (W8 — Persona Studio)
# ---------------------------------------------------------------------------
#
# The chat-message-assembly path runs once per HTTP request — see
# ``gateway/routes/chat.py::_build_internal_request``. When the trailing
# user message is a literal slash command (``/persona``, ``/help``, …) we
# rewrite the agent-facing ``content`` to the registry's
# ``wizard_prelude`` before the agent ever sees the request. Channels
# go through :func:`corlinman_channels.router.ChannelRouter.dispatch` for
# the same effect; both surfaces share the registry in
# :mod:`corlinman_channels.commands` so adding a new command is a single
# edit.
#
# Why only the trailing user message?
# -----------------------------------
# Commands are intentionally *invocation events* — when the user types
# ``/persona`` mid-conversation, they're saying "start the wizard now".
# A literal ``/persona`` sitting in an older turn of the same session is
# part of the conversation history (perhaps the agent was explaining the
# command, perhaps the user typed it earlier and was satisfied) and must
# NOT be retroactively rewritten on every subsequent turn — that would
# corrupt the transcript and re-trigger the wizard on every reply. So:
# rewrite the last user message only; leave all prior turns untouched.


class MessageLike:
    """Structural marker for the ``message.content`` rewrite seam.

    Any object exposing a mutable ``role`` and ``content`` attribute
    satisfies the contract — both
    :class:`corlinman_server.gateway_api.types.Message` (Pydantic v2,
    mutable by default) and
    :class:`corlinman_server.gateway.routes.chat.ChatMessage` (also
    Pydantic v2) fit. Declared as a runtime class with no body so we can
    use it as a documentation anchor without forcing isinstance() guards.
    """


_T = TypeVar("_T")


def apply_command_substitution(content: str) -> str:
    """Return the wizard prelude if ``content`` matches a registered
    slash command, else return ``content`` unchanged.

    Thin wrapper around
    :func:`corlinman_channels.commands.match_command` +
    :func:`corlinman_channels.commands.apply_command_prelude` so the
    chat-route handler doesn't import the channels package directly
    (keeps the dependency direction obvious in code review).
    """
    # Lazy import: avoids pulling the channels stack into module-load
    # time for gateway tests that monkey-patch the chat router.
    from corlinman_channels.commands import apply_command_prelude, match_command

    spec = match_command(content)
    if spec is None:
        return content
    return apply_command_prelude(content, spec)


def rewrite_trailing_user_message(messages: Sequence[_T]) -> list[_T]:
    """Return a new list with the trailing user message's content
    rewritten via :func:`apply_command_substitution`, if it matches.

    ``messages`` is treated as ordered oldest-first (the OpenAI request
    convention). The function:

    * Returns an empty list if ``messages`` is empty.
    * Walks backward to find the first ``role == "user"`` message; if
      none found, returns the input as a new list (no rewrite).
    * If the trailing user message's content does NOT match a command,
      returns a new list with the original objects (no copy / rewrite).
    * If it does match, the matched message is replaced in-place on the
      returned list with a copy whose ``content`` is the wizard prelude.
      The original object is left untouched so the caller's reference
      (and any audit trail it keeps) is preserved.

    Important: we **only** consider the trailing user message — see the
    module docstring for why retroactive history rewrites would corrupt
    the transcript.
    """
    # Realise the iterable into a list once so we can do reverse scans
    # and return a fresh container (the caller may mutate / append).
    result: list[_T] = list(messages)
    if not result:
        return result

    # Locate the last user-role message. Walking from the end keeps the
    # cost O(1) for the typical case where the trailing message IS the
    # user turn (the OpenAI convention).
    target_index = -1
    for i in range(len(result) - 1, -1, -1):
        role = getattr(result[i], "role", None)
        # Role may be a StrEnum (corlinman_server.gateway_api.types.Role)
        # or a plain str (gateway.routes.chat.ChatMessage); compare by
        # str() value to cover both.
        if role is None:
            continue
        if str(role) in ("user", "Role.user"):
            target_index = i
            break
        # ``Role.USER.value`` is the string ``"user"`` so ``str(role)``
        # yields ``"user"`` on the StrEnum, but be defensive about the
        # ``Role.<NAME>`` repr form some test stubs use.
        if hasattr(role, "value") and str(getattr(role, "value")) == "user":
            target_index = i
            break

    if target_index < 0:
        return result

    original = result[target_index]
    content = getattr(original, "content", "") or ""
    rewritten_content = apply_command_substitution(content)
    if rewritten_content == content:
        return result

    # Substitute. Try Pydantic's ``model_copy(update=...)`` first
    # (preserves immutability semantics on frozen models); fall back to a
    # shallow ``copy`` + attribute set for plain dataclasses / objects.
    try:
        copied = original.model_copy(update={"content": rewritten_content})  # type: ignore[attr-defined]
    except AttributeError:
        import copy as _copy

        copied = _copy.copy(original)
        try:
            copied.content = rewritten_content  # type: ignore[attr-defined]
        except AttributeError:
            # Object doesn't expose a settable ``content`` — give up and
            # return the original list unchanged rather than crash.
            return result

    result[target_index] = copied
    log.info(
        "chat.command_substituted index=%d original_len=%d prelude_len=%d",
        target_index,
        len(content),
        len(rewritten_content),
    )
    return result


# Keep an ``Iterable`` import alive for future expansion paths; the
# helper above narrows on ``Sequence`` for the index-walk, but the
# public seam may grow an ``Iterable`` accepting variant for streamed
# message sources.
_ = Iterable


def build_chat_service(state: Any) -> Any | None:
    """Build a :class:`ChatService` over a :class:`DirectProviderBackend`.

    Reads ``state.provider_registry`` (the handle P1 attaches) and
    ``state.config["models"]`` (for alias resolution). Returns the built
    :class:`ChatService`, or ``None`` when no provider registry is wired
    (degraded mode — ``/v1/chat/completions`` then keeps its 501).

    Split out from :func:`bootstrap` so tests (and P3's channel wiring)
    can build the service without going through the full sibling seam.
    """
    registry = getattr(state, "provider_registry", None)
    if registry is None:
        log.warning(
            "services.bootstrap.no_provider_registry; "
            "ChatService not wired, /v1/chat/completions stays degraded",
        )
        return None

    # Lazy imports — keep this module importable even if a sibling
    # package is mid-port. A failure here logs + degrades, never crashes
    # the gateway boot (contract §2.1 "gate, never crash").
    try:
        from corlinman_server.gateway.services.chat_service import ChatService
        from corlinman_server.gateway.services.direct_backend import (
            DirectProviderBackend,
        )
    except Exception as exc:  # noqa: BLE001 — degrade, don't crash boot
        log.warning("services.bootstrap.import_failed err=%s", exc)
        return None

    cfg = getattr(state, "config", None) or {}
    models_config = cfg.get("models") if isinstance(cfg, dict) else None
    if not isinstance(models_config, dict):
        models_config = {}

    backend = DirectProviderBackend(registry, models_config=models_config)
    return ChatService(backend)


def bootstrap(state: Any) -> None:
    """Sibling ``bootstrap`` hook — attach the chat service to ``state``.

    Called once during the gateway lifespan (``entrypoint.py``), *after*
    the ``providers`` sibling has populated ``state.provider_registry``
    (the seam order is load-bearing — see contract §2). Mutates ``state``
    in place:

    * ``state.chat`` ← a :class:`ChatService` wrapping a
      :class:`DirectProviderBackend`, or left ``None`` when no provider
      registry is available (degraded mode).

    Returns ``None`` — the chat service owns no background tasks. (P3's
    channel runtime, layered into this same seam, *does* return
    ``asyncio.Task``s; when P3 lands it should call :func:`build_chat_service`
    / reuse ``state.chat`` here and return its channel tasks. The
    entrypoint already accepts ``None`` | ``Awaitable`` | ``list[Task]``,
    so extending the return type is forward-compatible.)
    """
    if getattr(state, "chat", None) is not None:
        # Idempotent: another wiring path (a test, a P4 deployment that
        # pre-built a gRPC-backed service) already populated it. Don't
        # clobber it with the direct backend.
        log.info("services.bootstrap.chat_already_wired; skipping")
        return None

    service = build_chat_service(state)
    if service is None:
        return None

    state.chat = service
    log.info("services.bootstrap.chat_wired backend=DirectProviderBackend")
    return None
