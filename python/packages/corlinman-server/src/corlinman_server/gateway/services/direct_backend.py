"""``DirectProviderBackend`` — a fast-path :class:`ChatBackend`.

Parcel **P2** of the Python-port runtime-completion plan
(``docs/PLAN_PORT_COMPLETION.md`` §3, Wave 1). See
``docs/contracts/runtime-wiring.md`` §4 for the contract this module
implements.

Where :class:`~corlinman_server.gateway.services.chat_service.GrpcAgentChatBackend`
dials the Python *agent* over gRPC (the full path — tools, skills,
memory), :class:`DirectProviderBackend` skips the agent entirely and
calls :mod:`corlinman_providers` **directly**:

* it takes the :class:`agent_pb2.ChatStart` the :class:`ChatService`
  builds from an :class:`InternalChatRequest`;
* resolves the requested model against a
  :class:`corlinman_providers.registry.ProviderRegistry` (the handle P1
  attaches to ``AppState.provider_registry``);
* calls the resolved provider's ``chat_stream(...)`` async generator;
* translates each :class:`corlinman_providers.base.ProviderChunk` into
  an :class:`agent_pb2.ServerFrame` (``token`` / ``tool_call`` /
  ``done`` / ``error``).

This is a **stateless, no-tools** backend: it never runs the reasoning
loop, never executes a tool, never touches session memory. ``tool_call``
chunks *are* surfaced as ``ServerFrame.tool_call`` frames so the
OpenAI-shaped SSE renderer can emit them, but the provider stream just
ends after them (the model decided to call a tool and stopped) — there
is no second turn. The full agent path (P4 ``GrpcAgentChatBackend``)
is what closes the tool loop.

The translation runs inside an :func:`asyncio.create_task` so the
``(tx, rx)`` pair the :class:`ChatService` consumes has the same shape
as the gRPC backend's: ``rx`` is an async iterator of frames, ``tx`` is
an :class:`asyncio.Queue` the service can push ``ClientFrame``s into.
``DirectProviderBackend`` has no tools, so it only *drains* ``tx`` for
a ``cancel`` frame — a ``cancel`` cancels the in-flight provider stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from corlinman_grpc._generated.corlinman.v1 import (
    agent_pb2,
    common_pb2,
)

__all__ = [
    "DirectProviderBackend",
]

log = logging.getLogger(__name__)


# ─── Provider-id → OpenAI failover reason ────────────────────────────
#
# ``corlinman_providers.failover.CorlinmanError`` subclasses carry a
# stable lowercase ``reason`` (see ``failover.py``). ``ServerFrame.error``
# wants a ``common_pb2.FailoverReason`` enum value, so we map the
# adapter-side string back onto the proto enum. Anything we don't
# recognise falls through to ``UNKNOWN`` — a future error class can't
# crash the frame translation.
_REASON_TO_PROTO: dict[str, common_pb2.FailoverReason.ValueType] = {
    "billing": common_pb2.BILLING,
    "rate_limit": common_pb2.RATE_LIMIT,
    "auth": common_pb2.AUTH,
    "auth_permanent": common_pb2.AUTH_PERMANENT,
    "timeout": common_pb2.TIMEOUT,
    "model_not_found": common_pb2.MODEL_NOT_FOUND,
    "format": common_pb2.FORMAT,
    "context_overflow": common_pb2.CONTEXT_OVERFLOW,
    "overloaded": common_pb2.OVERLOADED,
    "unknown": common_pb2.UNKNOWN,
    "unspecified": common_pb2.FAILOVER_REASON_UNSPECIFIED,
}


def _reason_to_proto(reason: str | None) -> common_pb2.FailoverReason.ValueType:
    """Map a ``CorlinmanError.reason`` string onto the proto enum.

    Unknown / missing reasons collapse to ``UNKNOWN`` so an exception
    that isn't a typed :class:`CorlinmanError` still produces a valid
    terminal ``error`` frame.
    """
    if not reason:
        return common_pb2.UNKNOWN
    return _REASON_TO_PROTO.get(reason, common_pb2.UNKNOWN)


def _error_reason_of(exc: BaseException) -> str | None:
    """Best-effort extract of a ``reason`` discriminant off an exception.

    :class:`corlinman_providers.failover.CorlinmanError` subclasses
    expose a ``reason`` attribute; anything else returns ``None`` and
    the caller defaults to ``UNKNOWN``.
    """
    reason = getattr(exc, "reason", None)
    if isinstance(reason, str) and reason:
        return reason
    return None


# ─── Backend ──────────────────────────────────────────────────────────


class DirectProviderBackend:
    """A :class:`ChatBackend` that calls :mod:`corlinman_providers` directly.

    Construct it with a built
    :class:`corlinman_providers.registry.ProviderRegistry` and, optionally,
    the model-config block (``config["models"]``) so model aliases
    declared in ``config.toml`` resolve. :meth:`start` opens one provider
    stream per call.

    The registry is the same object P1 attaches to
    ``AppState.provider_registry``; :func:`bootstrap` reads it from there.
    """

    def __init__(
        self,
        registry: Any,
        *,
        models_config: dict[str, Any] | None = None,
    ) -> None:
        """:param registry: a built
            :class:`corlinman_providers.registry.ProviderRegistry`.
        :param models_config: the ``[models]`` table from ``config.toml``
            (``{"default": ..., "aliases": {alias: {provider, model,
            params}}}``). Used to resolve aliases. ``None`` ⇒ no aliases,
            every model id is treated as a raw upstream id (the registry's
            ``MODEL_PREFIX_DEFAULTS`` / configured-provider scan still
            applies).
        """
        self._registry = registry
        self._models_config = models_config or {}

    # -- ChatBackend protocol -------------------------------------------

    async def start(
        self,
        start: agent_pb2.ChatStart,
    ) -> tuple[asyncio.Queue[Any], AsyncIterator[agent_pb2.ServerFrame]]:
        """Open a provider stream for ``start`` and return ``(tx, rx)``.

        Mirrors the :class:`ChatBackend` contract: ``rx`` is an async
        iterator of :class:`agent_pb2.ServerFrame`, ``tx`` is a queue the
        :class:`ChatService` pushes :class:`agent_pb2.ClientFrame`s into.
        This backend only honours a ``cancel`` frame on ``tx``; a
        ``tool_result`` is accepted but ignored (no tool loop here).

        Resolution failures (unknown model, missing provider) do **not**
        raise out of ``start`` — they are surfaced as a terminal
        ``error`` frame on ``rx`` so the :class:`ChatService` renders a
        clean upstream error instead of a 500.
        """
        tx: asyncio.Queue[Any] = asyncio.Queue()
        rx: asyncio.Queue[agent_pb2.ServerFrame | None] = asyncio.Queue()

        worker = asyncio.create_task(self._pump(start, tx, rx))
        return tx, _QueueFrameIter(rx, worker)

    # -- internal: provider stream → frame queue ------------------------

    async def _pump(
        self,
        start: agent_pb2.ChatStart,
        tx: asyncio.Queue[Any],
        rx: asyncio.Queue[agent_pb2.ServerFrame | None],
    ) -> None:
        """Drive the provider stream, pushing translated frames onto ``rx``.

        Always terminates ``rx`` with a sentinel ``None`` (clean end of
        stream) so :class:`_QueueFrameIter` raises ``StopAsyncIteration``.
        A terminal ``done`` / ``error`` frame is pushed before the
        sentinel. Any unexpected exception becomes a terminal ``error``
        frame — the loop never lets an exception escape into the void.
        """
        cancel_task = asyncio.create_task(_watch_cancel(tx))
        try:
            try:
                provider, upstream_model, params = self._resolve(start.model)
            except Exception as exc:  # noqa: BLE001 — surface as error frame
                log.info(
                    "direct_backend.resolve_failed model=%s err=%s",
                    start.model,
                    exc,
                )
                await rx.put(_error_frame(exc))
                return

            messages = _messages_from_proto(start.messages)
            temperature, max_tokens = _sampling_from_proto(start, params)

            stream = provider.chat_stream(
                model=upstream_model,
                messages=messages,
                tools=None,
                temperature=temperature,
                max_tokens=max_tokens,
                extra=_extra_params(params),
            )

            stream_task = asyncio.ensure_future(
                self._consume(stream, rx),
            )
            done, _pending = await asyncio.wait(
                {stream_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done and stream_task not in done:
                # A ``cancel`` frame arrived before the provider stream
                # finished — cancel the in-flight call and emit a
                # terminal error so the service surfaces "cancelled".
                stream_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await stream_task
                await rx.put(
                    agent_pb2.ServerFrame(
                        error=common_pb2.ErrorInfo(
                            reason=common_pb2.UNKNOWN,
                            message="cancelled",
                        ),
                    ),
                )
            else:
                # Stream finished on its own — re-await to surface any
                # exception ``_consume`` may have raised.
                exc = stream_task.exception()
                if exc is not None:
                    log.info("direct_backend.stream_error err=%s", exc)
                    await rx.put(_error_frame(exc))
        except Exception as exc:  # noqa: BLE001 — last-ditch guard
            log.warning("direct_backend.pump_failed err=%s", exc)
            with contextlib.suppress(Exception):
                await rx.put(_error_frame(exc))
        finally:
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_task
            # Terminate the iterator no matter how we got here.
            await rx.put(None)

    async def _consume(
        self,
        stream: AsyncIterator[Any],
        rx: asyncio.Queue[agent_pb2.ServerFrame | None],
    ) -> None:
        """Translate each :class:`ProviderChunk` into a ``ServerFrame``.

        ``ProviderChunk`` kinds map as follows:

        * ``token`` → ``ServerFrame.token`` (``TokenDelta``).
        * ``tool_call_start`` → opens an arg buffer; deferred until
          ``tool_call_end`` so the emitted ``ToolCall`` carries the full
          argument JSON (the OpenAI-shaped SSE renderer wants complete
          arguments, not fragments).
        * ``tool_call_delta`` → appended to the open call's arg buffer.
        * ``tool_call_end`` → flushes a single ``ServerFrame.tool_call``.
        * ``done`` → ``ServerFrame.done`` (``Done``) with the normalised
          ``finish_reason``.

        A provider that ends without a ``done`` chunk still gets a
        synthesised ``done`` frame so the service always sees a terminal.
        """
        # tool_call_id → [tool_name, [arg_fragment, ...]]. Fragments are
        # appended to a list and ``''.join``ed once at finalization so the
        # emitted frame has the whole argument JSON in one shot — appending
        # is O(1) per delta (vs. ``str +=``, which recopies the whole
        # accumulated buffer each time → O(K^2) over K bytes / M deltas).
        open_calls: dict[str, list[Any]] = {}
        call_order: list[str] = []
        seq = 0
        saw_done = False

        async for chunk in stream:
            kind = getattr(chunk, "kind", None)

            if kind == "token":
                text = getattr(chunk, "text", None) or ""
                if text:
                    await rx.put(
                        agent_pb2.ServerFrame(
                            token=agent_pb2.TokenDelta(text=text, seq=seq),
                        ),
                    )
                    seq += 1
                continue

            if kind == "tool_call_start":
                call_id = getattr(chunk, "tool_call_id", None) or f"call_{seq}"
                name = getattr(chunk, "tool_name", None) or ""
                if call_id not in open_calls:
                    open_calls[call_id] = [name, []]
                    call_order.append(call_id)
                else:
                    # Late name on an already-open call.
                    if name:
                        open_calls[call_id][0] = name
                args0 = getattr(chunk, "arguments_delta", None)
                if args0:
                    open_calls[call_id][1].append(args0)
                continue

            if kind == "tool_call_delta":
                call_id = getattr(chunk, "tool_call_id", None)
                frag = getattr(chunk, "arguments_delta", None) or ""
                if call_id is None:
                    continue
                if call_id not in open_calls:
                    open_calls[call_id] = ["", []]
                    call_order.append(call_id)
                open_calls[call_id][1].append(frag)
                continue

            if kind == "tool_call_end":
                call_id = getattr(chunk, "tool_call_id", None)
                if call_id is None or call_id not in open_calls:
                    continue
                name, frags = open_calls.pop(call_id)
                if call_id in call_order:
                    call_order.remove(call_id)
                await rx.put(
                    _tool_call_frame(call_id, name, "".join(frags), seq),
                )
                seq += 1
                continue

            if kind == "done":
                # Flush any tool calls still open (provider ended the
                # stream without an explicit ``tool_call_end``).
                for call_id in call_order:
                    name, frags = open_calls[call_id]
                    await rx.put(
                        _tool_call_frame(call_id, name, "".join(frags), seq)
                    )
                    seq += 1
                open_calls.clear()
                call_order.clear()
                finish = getattr(chunk, "finish_reason", None) or "stop"
                await rx.put(
                    agent_pb2.ServerFrame(
                        done=agent_pb2.Done(finish_reason=finish),
                    ),
                )
                saw_done = True
                break

            # Unknown chunk kind — skip defensively.
            log.debug("direct_backend.unknown_chunk_kind kind=%s", kind)

        if not saw_done:
            # Provider stream ended without a ``done`` chunk — flush any
            # dangling tool calls and synthesise a terminal frame so the
            # ChatService never hangs waiting for one.
            for call_id in call_order:
                name, frags = open_calls[call_id]
                await rx.put(
                    _tool_call_frame(call_id, name, "".join(frags), seq)
                )
                seq += 1
            await rx.put(
                agent_pb2.ServerFrame(
                    done=agent_pb2.Done(
                        finish_reason="tool_calls" if call_order else "stop",
                    ),
                ),
            )

    # -- internal: model resolution -------------------------------------

    def _resolve(self, model: str) -> tuple[Any, str, dict[str, Any]]:
        """Resolve ``model`` to ``(provider, upstream_model, params)``.

        Delegates to :meth:`ProviderRegistry.resolve`, passing the
        ``[models.aliases]`` table so an alias declared in ``config.toml``
        routes to its provider + upstream model. A bare model id falls
        through the registry's configured-provider / prefix-default
        scan. Raises :class:`KeyError` when nothing matches — the caller
        turns that into a terminal ``error`` frame.
        """
        if self._registry is None:
            raise RuntimeError("no ProviderRegistry wired")
        aliases = _alias_entries(self._models_config)
        return self._registry.resolve(model, aliases=aliases)


# ─── Helpers ──────────────────────────────────────────────────────────


def _alias_entries(models_config: dict[str, Any]) -> dict[str, Any]:
    """Build the ``{alias: AliasEntry}`` map :meth:`ProviderRegistry.resolve`
    expects from the raw ``[models.aliases]`` config table.

    Each raw alias dict (``{provider, model, params}``) is coerced into a
    :class:`corlinman_providers.specs.AliasEntry`. A malformed entry is
    skipped with a warning rather than crashing resolution. Returns an
    empty dict when no aliases are configured.
    """
    raw = models_config.get("aliases")
    if not isinstance(raw, dict) or not raw:
        return {}
    try:
        from corlinman_providers.specs import AliasEntry
    except Exception:  # pragma: no cover — providers always importable
        return {}
    out: dict[str, Any] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        try:
            out[str(name)] = AliasEntry(**entry)
        except Exception as exc:  # noqa: BLE001 — skip a bad alias, keep the rest
            log.warning(
                "direct_backend.bad_alias alias=%s err=%s", name, exc
            )
    return out


def _messages_from_proto(
    messages: Any,
) -> list[dict[str, str]]:
    """Convert protobuf :class:`common_pb2.Message`s to provider dicts.

    Providers' ``chat_stream`` accepts either dicts or objects with
    ``role`` / ``content`` attributes (see
    :func:`corlinman_providers.openai_provider._normalise_message`); a
    dict is the simplest, vendor-agnostic shape. The proto ``Role`` enum
    is lowered to the OpenAI string discriminant.
    """
    out: list[dict[str, str]] = []
    for m in messages:
        msg: dict[str, str] = {
            "role": _role_to_str(m.role),
            "content": m.content or "",
        }
        if m.name:
            msg["name"] = m.name
        if m.tool_call_id:
            msg["tool_call_id"] = m.tool_call_id
        out.append(msg)
    return out


def _role_to_str(role: int) -> str:
    """Lower a proto :class:`common_pb2.Role` value to the OpenAI string."""
    if role == common_pb2.USER:
        return "user"
    if role == common_pb2.ASSISTANT:
        return "assistant"
    if role == common_pb2.SYSTEM:
        return "system"
    if role == common_pb2.TOOL:
        return "tool"
    return "user"


def _sampling_from_proto(
    start: agent_pb2.ChatStart,
    params: dict[str, Any],
) -> tuple[float | None, int | None]:
    """Pick the sampling knobs for the provider call.

    The :class:`ChatService` stamps ``temperature`` / ``max_tokens`` onto
    :class:`agent_pb2.ChatStart` from the :class:`InternalChatRequest`,
    but proto scalars have no "unset" — ``temperature`` defaults to
    ``0.0`` and ``max_tokens`` to ``0``. We treat ``0`` as "not set" and
    fall back to any provider/alias-level ``params`` default, so an
    operator's ``[models.aliases.*.params]`` block still applies.
    """
    temperature: float | None = None
    if start.temperature:
        temperature = float(start.temperature)
    elif "temperature" in params:
        with contextlib.suppress(TypeError, ValueError):
            temperature = float(params["temperature"])

    max_tokens: int | None = None
    if start.max_tokens:
        max_tokens = int(start.max_tokens)
    elif params.get("max_tokens"):
        with contextlib.suppress(TypeError, ValueError):
            max_tokens = int(params["max_tokens"])

    return temperature, max_tokens


def _extra_params(params: dict[str, Any]) -> dict[str, Any] | None:
    """Forward non-sampling provider params as the adapter ``extra`` map.

    ``temperature`` / ``max_tokens`` are passed as first-class kwargs, so
    they're stripped here to avoid a double-set. Everything else (e.g.
    ``top_p``, ``reasoning_effort``) flows through ``extra`` — the
    adapters merge it straight into the vendor request kwargs.
    """
    if not params:
        return None
    extra = {
        k: v
        for k, v in params.items()
        if k not in ("temperature", "max_tokens")
    }
    return extra or None


def _tool_call_frame(
    call_id: str,
    tool_name: str,
    args_json: str,
    seq: int,
) -> agent_pb2.ServerFrame:
    """Build a ``ServerFrame.tool_call`` from a buffered provider call.

    The OpenAI-standard ``tool_name`` is a flat string (no ``plugin``
    namespace); ``ToolCall`` proto splits ``plugin`` / ``tool``. There is
    no namespace in the direct provider path, so ``plugin`` is left empty
    and the whole name lands in ``tool`` — the SSE renderer
    (``routes/chat.py:_tool_call_envelope``) only reads ``event.tool``.
    """
    return agent_pb2.ServerFrame(
        tool_call=agent_pb2.ToolCall(
            call_id=call_id,
            plugin="",
            tool=tool_name or "",
            args_json=(args_json or "{}").encode("utf-8"),
            seq=seq,
        ),
    )


def _error_frame(exc: BaseException) -> agent_pb2.ServerFrame:
    """Build a terminal ``ServerFrame.error`` from an exception."""
    return agent_pb2.ServerFrame(
        error=common_pb2.ErrorInfo(
            reason=_reason_to_proto(_error_reason_of(exc)),
            message=str(exc) or exc.__class__.__name__,
        ),
    )


async def _watch_cancel(tx: asyncio.Queue[Any]) -> None:
    """Block until a ``cancel`` :class:`ClientFrame` arrives on ``tx``.

    The :class:`ChatService` pushes ``ClientFrame``s (``tool_result`` /
    ``cancel``) onto the backend's ``tx`` queue. The direct path has no
    tools, so ``tool_result`` frames are drained and dropped; only a
    ``cancel`` resolves this coroutine, which the pump uses to abort the
    in-flight provider stream.
    """
    while True:
        frame = await tx.get()
        which = None
        if isinstance(frame, agent_pb2.ClientFrame):
            which = frame.WhichOneof("kind")
        if which == "cancel":
            return
        # ``tool_result`` / ``start`` / unknown — ignore and keep waiting.


class _QueueFrameIter:
    """Async iterator over a frame queue fed by :meth:`DirectProviderBackend._pump`.

    A sentinel ``None`` on the queue marks clean end-of-stream and raises
    :class:`StopAsyncIteration` — the same termination signal
    :class:`ChatService._next_frame` expects from the gRPC backend's
    iterator. Holds the pump task so it can be cancelled if the consumer
    abandons the iterator early.
    """

    def __init__(
        self,
        rx: asyncio.Queue[agent_pb2.ServerFrame | None],
        worker: asyncio.Task[Any],
    ) -> None:
        self._rx = rx
        self._worker = worker
        self._closed = False

    def __aiter__(self) -> _QueueFrameIter:
        return self

    async def __anext__(self) -> agent_pb2.ServerFrame:
        if self._closed:
            raise StopAsyncIteration
        frame = await self._rx.get()
        if frame is None:
            self._closed = True
            # Pump has finished; reap it so a stray exception surfaces
            # in logs rather than as an "exception never retrieved".
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._worker
            raise StopAsyncIteration
        return frame
