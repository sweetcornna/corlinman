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
    "FIRST_CHAT_TIP_TEXT",
    "MessageLike",
    "apply_command_substitution",
    "bootstrap",
    "build_chat_service",
    "maybe_prepend_first_chat_tip",
    "rewrite_trailing_user_message",
]


# W3 first-run-wizard contract D3 — one-time advisory shown on the
# *first* user message in a channel/thread, pointing the user at the
# ``/sethome`` slash command. Centralised so the tests + the entrypoint
# restart broadcast read the same literal.
FIRST_CHAT_TIP_TEXT: str = (
    "💡 提示：使用 /sethome 命令可将当前窗口设为主聊天窗口，"
    "重启等系统提醒只发到主窗口。"
)

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
    """Return the wizard prelude (or a synthetic relay prelude) when
    ``content`` matches a registered slash command, else return
    ``content`` unchanged.

    Two cases:

    * Spec has a ``wizard_prelude`` (whether or not it also has a
      handler) → return the prelude. The LLM produces the reply.
    * Spec has only a ``handler`` → invoke the handler in-process and
      wrap its reply in a synthetic prelude that asks the LLM to relay
      the canned text verbatim. This keeps the playground functional
      for handler-only commands without requiring a separate
      direct-send path on the web surface. Sync handlers run inline;
      async handlers (e.g. ``/usage``) are driven to completion via
      ``asyncio.run`` when no event loop is running on this thread, and
      only fall back to a polite "(requires an async surface)" message
      when a loop IS running (blocking would deadlock it) — see
      :func:`corlinman_channels.commands._run_command_handler_sync`.

    Thin wrapper around the channels-side helpers so the chat-route
    handler doesn't import the channels package directly (keeps the
    dependency direction obvious in code review).
    """
    # Lazy import: avoids pulling the channels stack into module-load
    # time for gateway tests that monkey-patch the chat router.
    from corlinman_channels.commands import (  # noqa: PLC0415 — lazy by design
        CommandContext,
        _run_command_handler_sync,
        apply_command_prelude,
        is_command_admin,
        match_command_with_args,
    )
    from corlinman_channels.common import (  # noqa: PLC0415
        ChannelBinding,
    )

    match = match_command_with_args(content)
    if match is None:
        return content
    spec, args_text = match

    if spec.wizard_prelude is not None:
        # Prefer the prelude — the LLM produces the reply, matching the
        # legacy behaviour for /persona and other wizard flows.
        # ``corlinman_channels`` ships no py.typed marker, so the call is
        # seen as untyped; the function's own signature returns ``str``.
        prelude: str = apply_command_prelude(content, spec)
        return prelude

    if spec.handler is None:
        # Should be unreachable thanks to validate_registry, but degrade
        # safely if a future spec slips through.
        return content

    # Handler-only spec: run it in-process and ask the LLM to relay.
    # The playground does not have a separate direct-send surface, so
    # synthesising a prelude is the cleanest way to keep these commands
    # functional on the web without a deeper integration. The synthetic
    # binding makes per-binding handlers stay polite (channel="playground"
    # signals to handlers that they're running in a non-channel context).
    synthetic_binding = ChannelBinding(
        channel="playground",
        account="web",
        thread="web",
        sender="web",
    )
    ctx = CommandContext(
        spec=spec,
        raw_text=content,
        args_text=args_text,
        binding=synthetic_binding,
        is_admin=is_command_admin(synthetic_binding),
    )
    try:
        result = _run_command_handler_sync(spec, ctx)
    except Exception as exc:  # noqa: BLE001 — degrade to literal text
        log.warning(
            "chat_bootstrap.handler_failed cmd=%s err=%s", spec.name, exc
        )
        return content
    reply = (result.reply or "").strip()
    if not reply:
        return content
    return (
        "[SYSTEM-INSERTED] The user invoked "
        f"{spec.aliases[0] if spec.aliases else '/' + spec.name}. "
        "Reply with the following text verbatim, without any extra "
        "commentary:\n\n" + reply
    )


def maybe_prepend_first_chat_tip(
    messages: Sequence[_T],
    *,
    user_id: str | None,
    channel: str | None,
    thread: str | None,
) -> list[_T]:
    """Prepend the one-time ``/sethome`` tip to ``messages`` when this
    is the user's first turn in ``(channel, thread)``.

    W3 first-run-wizard contract D3. Returns a new list:

    * No-op (returns ``list(messages)``) when any of ``user_id`` /
      ``channel`` / ``thread`` is falsy — the surface didn't hand us
      enough context to scope the flag (the HTTP /v1/chat/completions
      route doesn't carry a binding; channel adapters do).
    * No-op when there is more than one user-role message in the
      window. We treat ``turn_count <= 1`` as "this is the first
      turn" — counting the user messages already on the request is
      the cheapest stand-in for ``SessionSummary.turn_count`` we can
      get without dragging the session journal into this hot path.
    * No-op when the home-channel store already has a
      ``first_chat_tips_shown`` row for this triple, or when the
      store cannot be imported (standalone deploys / tests).
    * On the first eligible turn: prepends a system-role message
      carrying :data:`FIRST_CHAT_TIP_TEXT`, stamps the
      ``first_chat_tips_shown`` row, and returns the longer list.

    The prepended message is constructed as a tiny dataclass-shaped
    object with ``role="system"`` + ``content``. The downstream
    chat-request builder (:func:`ChatRequest` / the channels-side
    ``SimpleNamespace`` request) reads only those two fields, so the
    stub fits both surfaces without an explicit type import.
    """
    materialised: list[_T] = list(messages)
    if not user_id or not channel or not thread:
        return materialised

    user_count = 0
    for m in materialised:
        role = getattr(m, "role", None)
        if role is None:
            continue
        role_str = str(role)
        role_value = getattr(role, "value", None)
        if role_str in ("user", "Role.user") or (
            role_value is not None and str(role_value) == "user"
        ):
            user_count += 1
    # ``turn_count <= 1`` from the spec — we only fire the tip when
    # this request has exactly the opening user message (or none yet).
    # A retry sequence that already carries a full back-and-forth has
    # turn_count >= 2 and skips the tip.
    if user_count > 1:
        return materialised

    # Lazy import: the store lives in the server top-level. A missing
    # module degrades to a silent skip — channel tests that don't have
    # the server package available still import this module.
    try:
        from corlinman_server import home_channel_store  # noqa: PLC0415
    except ImportError as exc:
        log.warning("chat_bootstrap.home_channel_store_missing err=%s", exc)
        return materialised

    try:
        if home_channel_store.was_tip_shown(user_id, channel, thread):
            return materialised
    except Exception as exc:  # noqa: BLE001 — degrade silently on store errors
        log.warning("chat_bootstrap.was_tip_shown_failed err=%s", exc)
        return materialised

    # Stamp the flag BEFORE prepending so a follow-up retry on the
    # same turn doesn't double-inject if the LLM call fails partway
    # through. The user gets the tip exactly once per (channel,
    # thread) even on a retry storm.
    try:
        home_channel_store.mark_tip_shown(user_id, channel, thread)
    except Exception as exc:  # noqa: BLE001
        log.warning("chat_bootstrap.mark_tip_shown_failed err=%s", exc)
        return materialised

    # Build the system-message stub. Using ``types.SimpleNamespace``
    # mirrors the shape ``corlinman_channels.service._build_internal_request``
    # already uses, and the HTTP ``ChatMessage`` (Pydantic) duck-types
    # the same two fields. The list type is preserved by keeping
    # ``_T`` opaque — the caller's downstream consumer just iterates.
    from types import SimpleNamespace  # noqa: PLC0415

    tip_msg = SimpleNamespace(role="system", content=FIRST_CHAT_TIP_TEXT)
    log.info(
        "chat_bootstrap.first_chat_tip_prepended channel=%s thread=%s",
        channel,
        thread,
    )
    return [tip_msg, *materialised]  # type: ignore[list-item]


def rewrite_trailing_user_message(
    messages: Sequence[_T],
    *,
    user_id: str | None = None,
    channel: str | None = None,
    thread: str | None = None,
) -> list[_T]:
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

    Binding-context kwargs (``user_id`` / ``channel`` / ``thread``)
    are forwarded to :func:`maybe_prepend_first_chat_tip` so a channel
    adapter that has the home-channel context handy can opt into the
    one-time first-chat tip in the same call. The HTTP
    ``/v1/chat/completions`` route does not carry a binding and
    therefore leaves all three as ``None`` — the tip helper degrades
    to a no-op in that case (matches the contract: the tip is a
    channel-side affordance, not a web-API artefact).
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
        if hasattr(role, "value") and str(role.value) == "user":
            target_index = i
            break

    if target_index >= 0:
        original = result[target_index]
        content = getattr(original, "content", "") or ""
        rewritten_content = apply_command_substitution(content)
        if rewritten_content != content:
            # Substitute. Try Pydantic's ``model_copy(update=...)``
            # first (preserves immutability semantics on frozen
            # models); fall back to a shallow ``copy`` + attribute
            # set for plain dataclasses / objects.
            try:
                copied = original.model_copy(  # type: ignore[attr-defined]
                    update={"content": rewritten_content}
                )
                result[target_index] = copied
                log.info(
                    "chat.command_substituted index=%d "
                    "original_len=%d prelude_len=%d",
                    target_index,
                    len(content),
                    len(rewritten_content),
                )
            except AttributeError:
                import copy as _copy

                copied = _copy.copy(original)
                try:
                    copied.content = rewritten_content  # type: ignore[attr-defined]
                    result[target_index] = copied
                    log.info(
                        "chat.command_substituted index=%d "
                        "original_len=%d prelude_len=%d",
                        target_index,
                        len(content),
                        len(rewritten_content),
                    )
                except AttributeError:
                    # Object doesn't expose a settable ``content`` —
                    # give up on the rewrite but still let the
                    # first-chat tip hook run below.
                    pass

    # W3 first-run tip — single call site for the home-channel
    # advisory. No-op when the caller didn't pass binding context
    # (the HTTP route case) or when the user has already seen the
    # tip in this thread.
    return maybe_prepend_first_chat_tip(
        result, user_id=user_id, channel=channel, thread=thread
    )


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
