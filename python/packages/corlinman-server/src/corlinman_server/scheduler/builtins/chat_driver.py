"""Channel-neutral internal-chat driver for scheduler builtins.

Provides the shared plumbing that turns a scheduler firing into one bounded
internal agent-chat turn. QZone builtins and out-of-repository private job
plugins consume the same implementation:

* metadata / handle resolution off the :class:`BuiltinContext`
  (:func:`resolve_metadata`, :func:`resolve_chat_service`,
  :func:`resolve_or_open_persona_stores`, :func:`resolve_data_dir`,
  :func:`resolve_default_model`);
* :class:`InternalChatRequest` construction
  (:func:`build_internal_chat_request`, :func:`build_session_key`);
* the bounded event-stream consume loop (:func:`drive_chat_turn`),
  which records every ``tool_call`` / ``tool_result`` into a
  :class:`ChatDriveOutcome` so each builtin can harvest the tool(s) it
  cares about and shape its own audit dict;
* tolerant event introspection (:func:`event_kind`, :func:`event_field`,
  :func:`decode_json_args`, :func:`harvest_envelope`) plus the small
  coercers and the persona-slug guard both sidecars rely on.

Behavioral contract: everything here is *total* — no helper raises, the
drive folds run/consume/timeout/error-event failures into the outcome's
``error`` discriminator, and the callers translate that into their
audit-dict vocabulary. The extraction is semantics-preserving for
``qzone.daily_publish``: its wrapper reproduces the exact pre-extraction
audit dict shapes (see ``qzone_daily._drive_chat_turn``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from corlinman_server.scheduler.builtins.registry import BuiltinContext

_logger = logging.getLogger("corlinman_server.scheduler.builtins.chat_driver")


__all__ = [
    "ChatDriveOutcome",
    "ToolCallRecord",
    "ToolResultRecord",
    "build_internal_chat_request",
    "build_session_key",
    "coerce_optional_str",
    "coerce_str",
    "decode_json_args",
    "drive_chat_turn",
    "elapsed_ms",
    "event_field",
    "event_kind",
    "harvest_envelope",
    "resolve_chat_service",
    "resolve_data_dir",
    "resolve_default_model",
    "resolve_drive_timeout",
    "resolve_metadata",
    "resolve_or_open_persona_stores",
    "scheduler_context",
    "valid_persona_slug",
]


# ---------------------------------------------------------------------------
# Metadata / handle resolution
# ---------------------------------------------------------------------------


def resolve_metadata(
    context: BuiltinContext, *, direct_attr: str
) -> dict[str, Any]:
    """Pull the job's metadata dict off the context.

    Convention: scheduler firings stash the job's ``metadata`` (the
    free-form ``[[scheduler.jobs]].metadata`` table from TOML) onto
    ``context.app_state.scheduler_job_metadata[context.name]`` so the
    builtin can recover it without re-parsing the config. Tests pass
    the dict directly via ``context.app_state.<direct_attr>`` (e.g.
    ``qzone_daily_metadata`` / ``qzone_reply_metadata``) so the test
    surface stays small.

    Returns an empty dict when nothing is found — the action then
    surfaces a clean ``missing_*`` operator error.
    """
    if isinstance(context.metadata, dict):
        return dict(context.metadata)

    app_state = context.app_state
    if app_state is None:
        return {}

    # Backward-compatible production seam for config-derived jobs that do not
    # yet carry metadata on JobSpec. Runtime jobs use the immutable snapshot.
    # gateway boot wiring per-job metadata wins over the simpler test
    # seam below (the test seam is meant for "one qzone job in this
    # process" environments, not for cohabiting with the per-job map).
    table = getattr(app_state, "scheduler_job_metadata", None)
    if isinstance(table, dict) and context.name:
        per_job = table.get(context.name)
        if isinstance(per_job, dict) and per_job:
            return per_job

    # Direct test seam — a plain dict park. Useful for single-job test
    # harnesses that don't want to spin up the per-job map.
    direct = getattr(app_state, direct_attr, None)
    if isinstance(direct, dict) and direct:
        return direct

    # Fallback — the runtime scheduler store keeps job metadata on a
    # dataclass with ``.metadata``; resolve by name.
    jobs = getattr(app_state, "scheduler_jobs", None)
    if isinstance(jobs, list) and context.name:
        for job in jobs:
            if getattr(job, "name", None) == context.name:
                meta = getattr(job, "metadata", None)
                if isinstance(meta, dict):
                    return meta
    return {}


def resolve_chat_service(context: BuiltinContext) -> Any | None:
    """Find the live ``ChatService`` on the AppState bundle.

    The gateway lifecycle parks the constructed service on
    ``state.chat`` (see ``gateway/services/chat_bootstrap.py``); the
    fallback probes ``state.extras["chat"]`` for tests that build a
    degraded AppState.
    """
    app_state = context.app_state
    if app_state is None:
        return None
    for owner in (
        app_state,
        getattr(app_state, "corlinman_state", None),
        getattr(app_state, "corlinman", None),
    ):
        if owner is None:
            continue
        chat = getattr(owner, "chat", None)
        if chat is not None:
            return chat
        extras = getattr(owner, "extras", None)
        if isinstance(extras, dict) and extras.get("chat") is not None:
            return extras["chat"]
    return None


async def resolve_or_open_persona_stores(
    context: BuiltinContext,
) -> tuple[Any, Any, list[Any]] | None:
    """Return ``(persona_store, asset_store, owned)`` or ``None``.

    Tries three resolution strategies in order:

    1. Live handle on ``app_state.persona_store`` — the canonical path
       once the lifecycle wired the open store.
    2. Admin_a state on ``app_state.admin_a_state.persona_store`` —
       fallback for the wiring that parks the handle on the admin
       state bundle.
    3. Fresh open against ``<DATA_DIR>/personas.sqlite``. Returned
       handles are recorded in ``owned`` so the caller closes them on
       the ``finally`` branch.

    Returns ``None`` only when every probe + open fails — the action
    then surfaces ``persona_store_unavailable``.
    """
    app_state = context.app_state
    persona_store = _probe_persona_store(app_state)
    asset_store = _probe_asset_store(app_state)
    owned: list[Any] = []

    if persona_store is not None:
        return persona_store, asset_store, owned

    # Fresh-open fallback. Cheap on a typical deployment because the
    # store is just a single aiosqlite connection.
    data_dir = resolve_data_dir(app_state)
    if data_dir is None:
        return None
    try:
        from corlinman_server.persona import PersonaAssetStore, PersonaStore
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "scheduler.builtin.chat_driver.persona_import_failed",
            extra={"error": repr(exc)},
        )
        return None
    try:
        ps = await PersonaStore.open(data_dir / "personas.sqlite")
        owned.append(ps)
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "scheduler.builtin.chat_driver.persona_open_failed",
            extra={"error": repr(exc)},
        )
        return None
    if asset_store is None:
        try:
            asset_store = await PersonaAssetStore.open(
                data_dir / "persona_assets.sqlite",
                data_dir / "personas",
            )
            owned.append(asset_store)
        except Exception as exc:  # noqa: BLE001 — asset store is optional
            _logger.warning(
                "scheduler.builtin.chat_driver.asset_open_failed",
                extra={"error": repr(exc)},
            )
            asset_store = None
    return ps, asset_store, owned


def _probe_persona_store(app_state: Any | None) -> Any | None:
    if app_state is None:
        return None
    store = getattr(app_state, "persona_store", None)
    if store is not None:
        return store
    admin_a = getattr(app_state, "admin_a_state", None)
    if admin_a is not None:
        store = getattr(admin_a, "persona_store", None)
        if store is not None:
            return store
    extras = getattr(app_state, "extras", None)
    if isinstance(extras, dict):
        return extras.get("persona_store")
    return None


def _probe_asset_store(app_state: Any | None) -> Any | None:
    if app_state is None:
        return None
    store = getattr(app_state, "persona_asset_store", None)
    if store is not None:
        return store
    admin_a = getattr(app_state, "admin_a_state", None)
    if admin_a is not None:
        store = getattr(admin_a, "persona_asset_store", None)
        if store is not None:
            return store
    extras = getattr(app_state, "extras", None)
    if isinstance(extras, dict):
        return extras.get("persona_asset_store")
    return None


def resolve_data_dir(app_state: Any | None) -> Path | None:
    if app_state is not None:
        dd = getattr(app_state, "data_dir", None)
        if dd is not None:
            return Path(dd)
    env = os.environ.get("CORLINMAN_DATA_DIR")
    if env:
        return Path(env)
    return None


def scheduler_context(context: BuiltinContext) -> dict[str, str]:
    """Return the trusted source/occurrence identity for an internal turn."""
    return {
        "source_system": context.source_system or "corlinman",
        "source_job_id": context.source_job_id or context.name or "scheduled-job",
        "occurrence_key": context.occurrence_key
        or f"manual:{context.run_id or 'unknown'}",
    }


def resolve_default_model(context: BuiltinContext) -> str:
    """Pick the model to drive the scheduled chat with.

    Resolution chain:

    1. ``app_state.scheduler_default_model`` — explicit override set by
       the scheduler lifecycle (or tests).
    2. ``cfg["models"]["default"]`` — matches the channels-runtime
       resolution, so a scheduled QZone turn uses the same model the
       bot replies with in QQ.
    3. Empty string — the gateway then falls back to its own default;
       the rest of the pipeline tolerates the empty value.
    """
    app_state = context.app_state
    for owner in (
        app_state,
        getattr(app_state, "corlinman_state", None),
        getattr(app_state, "corlinman", None),
    ):
        if owner is None:
            continue
        explicit = getattr(owner, "scheduler_default_model", None)
        if isinstance(explicit, str) and explicit:
            return explicit
        cfg = getattr(owner, "config", None)
        if isinstance(cfg, dict):
            models_cfg = cfg.get("models")
            if isinstance(models_cfg, dict):
                model = models_cfg.get("default")
                if isinstance(model, str) and model:
                    return model
    return ""


# ---------------------------------------------------------------------------
# Request construction
# ---------------------------------------------------------------------------


def build_session_key(persona_id: str) -> str:
    """Per-firing scheduler-scoped key.

    Including a fresh uuid means consecutive firings don't accidentally
    inherit the previous turn's per-session memory / approvals.
    """
    return f"scheduler:qzone:{persona_id}:{uuid.uuid4().hex[:8]}"


def build_internal_chat_request(
    *,
    model: str,
    session_key: str,
    system_prompt: str,
    user_turn: str,
    persona_id: str | None = None,
    execution_mode: str = "live",
    scheduler_context: dict[str, str] | None = None,
) -> Any | None:
    """Construct an :class:`InternalChatRequest` for the scheduler turn.

    Imported lazily so a degraded gateway boot that excluded
    ``corlinman_server.gateway_api`` doesn't crash the registry import.

    ``persona_id`` is forwarded onto the request so the agent servicer
    wires ``ChatStart.extra["persona_id"]`` — without it the scheduler-
    fired turn has no persona binding, so persona-life placeholders and
    the qzone tools' persona resolution all fall back to requiring an
    explicit ``persona_id`` arg from the model.
    """
    try:
        from corlinman_server.gateway_api.types import (
            InternalChatRequest,
            Message,
            Role,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        _logger.warning(
            "scheduler.builtin.chat_driver.gateway_api_import_failed",
            extra={"error": repr(exc)},
        )
        return None

    return InternalChatRequest(
        model=model,
        messages=[
            Message(role=Role.SYSTEM, content=system_prompt),
            Message(role=Role.USER, content=user_turn),
        ],
        session_key=session_key,
        stream=False,
        max_tokens=None,
        temperature=None,
        attachments=[],
        binding=None,
        persona_id=persona_id,
        scheduler_context={
            **(scheduler_context or {}),
            "execution_mode": execution_mode,
        },
    )


def resolve_drive_timeout(env_var: str, *, default: float = 300.0) -> float:
    """Per-firing chat-drive timeout in seconds, read from ``env_var``."""
    raw = os.environ.get(env_var, "")
    try:
        return max(10.0, float(raw)) if raw else default
    except ValueError:
        return default


def elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


# ---------------------------------------------------------------------------
# Chat-stream drive
# ---------------------------------------------------------------------------


@dataclass
class ToolCallRecord:
    """One ``tool_call`` event, with its args decoded best-effort."""

    plugin: str
    tool: str
    call_id: str
    args: dict[str, Any] | None


@dataclass
class ToolResultRecord:
    """One ``tool_result`` event, with its envelope decoded best-effort."""

    tool: str
    call_id: str
    is_error: bool
    error_summary: str
    envelope: dict[str, Any]


@dataclass
class ChatDriveOutcome:
    """What happened while consuming one ``chat_service.run`` stream.

    ``error`` is the drive-level discriminator (``None`` = the stream
    reached a terminal ``DoneEvent``):

    * ``"run_failed"`` — ``chat_service.run`` itself raised
      (``message`` carries the repr'd cause; no events were consumed).
    * ``"timeout"`` — the bounded consume hit its deadline; ``cancel``
      was set so the backend can tear the turn down.
    * ``"consume_failed"`` — the stream raised mid-iteration
      (``message`` carries the cause).
    * ``"chat_error"`` — an ``ErrorEvent`` came off the stream
      (``chat_error_reason`` / ``chat_error_message`` carry the wrapped
      error).

    ``calls`` / ``results`` record every tool interaction in stream
    order so callers can harvest whichever tool(s) they care about.
    ``final_text`` concatenates user-visible token deltas while excluding
    reasoning chunks.
    """

    error: str | None = None
    message: str = ""
    chat_error_reason: str = ""
    chat_error_message: str = ""
    tools_called: list[str] = field(default_factory=list)
    calls: list[ToolCallRecord] = field(default_factory=list)
    results: list[ToolResultRecord] = field(default_factory=list)
    final_text: str = ""
    finish_reason: str | None = None
    duration_ms: int = 0


async def drive_chat_turn(
    *,
    chat_service: Any,
    request: Any,
    cancel: asyncio.Event,
    timeout_env: str,
    timeout_default: float = 300.0,
) -> ChatDriveOutcome:
    """Consume the ``chat_service.run`` event stream into a
    :class:`ChatDriveOutcome`.

    The stream is allowed to emit any number of intermediate
    ``tool_call`` / ``token_delta`` events; every ``tool_call`` /
    ``tool_result`` is recorded (args / envelope decoded tolerantly) and
    the terminal ``DoneEvent`` / ``ErrorEvent`` stops the loop.

    The drive is bounded by the ``timeout_env`` environment knob
    (default ``timeout_default`` seconds) so a hung backend can't park
    the scheduler tick loop indefinitely; on timeout ``cancel`` is set.
    Never raises — every failure folds into ``outcome.error``.
    """
    started_at = time.monotonic()
    timeout_secs = resolve_drive_timeout(timeout_env, default=timeout_default)
    outcome = ChatDriveOutcome()

    try:
        stream = chat_service.run(request, cancel)
    except Exception as exc:  # noqa: BLE001 — surface via the outcome
        _logger.exception("scheduler.builtin.chat_driver.run_failed")
        outcome.error = "run_failed"
        outcome.message = str(exc)
        outcome.duration_ms = elapsed_ms(started_at)
        return outcome

    deadline = time.monotonic() + timeout_secs

    async def _consume() -> None:
        async for event in stream:
            kind = event_kind(event)
            if kind == "token_delta":
                if not bool(event_field(event, "is_reasoning", default=False)):
                    outcome.final_text += str(
                        event_field(event, "text", default="") or ""
                    )
            elif kind == "tool_call":
                plugin = str(event_field(event, "plugin", default="") or "")
                tool = str(event_field(event, "tool", default="") or "")
                call_id = str(event_field(event, "call_id", default="") or "")
                outcome.tools_called.append(
                    f"{plugin}.{tool}" if plugin else tool
                )
                outcome.calls.append(
                    ToolCallRecord(
                        plugin=plugin,
                        tool=tool,
                        call_id=call_id,
                        args=decode_json_args(
                            event_field(event, "args_json", default=b"")
                        ),
                    )
                )
            elif kind == "tool_result":
                outcome.results.append(
                    ToolResultRecord(
                        tool=str(event_field(event, "tool", default="") or ""),
                        call_id=str(
                            event_field(event, "call_id", default="") or ""
                        ),
                        is_error=bool(
                            event_field(event, "is_error", default=False)
                        ),
                        error_summary=str(
                            event_field(event, "error_summary", default="")
                            or ""
                        ),
                        envelope=harvest_envelope(event),
                    )
                )
            elif kind == "done":
                outcome.finish_reason = event_field(
                    event, "finish_reason", default=""
                )
                return
            elif kind == "error":
                inner = event_field(event, "error", default=None)
                outcome.error = "chat_error"
                outcome.chat_error_reason = str(
                    getattr(inner, "reason", "unknown")
                )
                outcome.chat_error_message = str(getattr(inner, "message", ""))
                return

    try:
        await asyncio.wait_for(
            _consume(), timeout=max(1.0, deadline - time.monotonic())
        )
    except TimeoutError:
        cancel.set()
        outcome.error = "timeout"
    except Exception as exc:  # noqa: BLE001 — defensive
        _logger.exception("scheduler.builtin.chat_driver.consume_failed")
        outcome.error = "consume_failed"
        outcome.message = str(exc)
    outcome.duration_ms = elapsed_ms(started_at)
    return outcome


# ---------------------------------------------------------------------------
# Event-introspection helpers — tolerate both pydantic / dataclass shapes
# ---------------------------------------------------------------------------


def event_kind(event: Any) -> str:
    """Return the discriminator for an InternalChatEvent.

    The dataclass shapes carry a ``kind`` literal field; older / mocked
    shapes may use ``isinstance`` matches. We probe a small ladder so
    test stubs can use the simplest dict-like shape.
    """
    kind = getattr(event, "kind", None)
    if isinstance(kind, str):
        return kind
    cls_name = type(event).__name__
    return {
        "TokenDeltaEvent": "token_delta",
        "ToolCallEvent": "tool_call",
        "ToolResultEvent": "tool_result",
        "DoneEvent": "done",
        "ErrorEvent": "error",
    }.get(cls_name, cls_name.lower())


def event_field(event: Any, name: str, *, default: Any = None) -> Any:
    """Read ``name`` off an event, falling back to ``default``.

    Tolerates both attribute access (dataclasses, pydantic models) and
    item access (dict stubs) so tests can use whichever shape is
    cheapest.
    """
    val = getattr(event, name, None)
    if val is not None:
        return val
    if isinstance(event, dict) and name in event:
        return event[name]
    return default


def decode_json_args(args_json: Any) -> dict[str, Any] | None:
    """Decode the ``args_json`` payload on a ``ToolCallEvent``.

    Used as a fallback for a tool's result envelope when the
    ``ToolResultEvent`` carries no payload — we at least know what the
    agent intended to send.
    """
    if not args_json:
        return None
    raw: str
    if isinstance(args_json, (bytes, bytearray)):
        try:
            raw = bytes(args_json).decode("utf-8")
        except UnicodeDecodeError:
            return None
    else:
        raw = str(args_json)
    try:
        obj = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def harvest_envelope(tool_result: Any) -> dict[str, Any]:
    """Parse the result envelope off ``ToolResultEvent.payload_json``.

    The gateway forwards the tool's parsed result envelope (the same
    dict the agent saw) as a JSON string on the event's ``payload_json``
    field — added in the tid/qzone_url observability fix. Decoding it
    here is how we recover the actual fields a successful tool call
    returned.

    Tolerant + total: returns an empty dict when the field is absent,
    blank, or malformed, and accepts ``str`` / ``bytes`` payloads. Never
    raises — scheduler builtins fold every failure into the audit dict,
    never up the stack.
    """
    raw = event_field(tool_result, "payload_json", default="")
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            return {}
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return {}
    return obj if isinstance(obj, dict) else {}


# ---------------------------------------------------------------------------
# Small shared coercers / guards
# ---------------------------------------------------------------------------


def coerce_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def coerce_optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def valid_persona_slug(persona_id: str) -> bool:
    """True iff ``persona_id`` is a safe filename slug (blocks path traversal).

    Mirrors the agent-side ``persona_life._valid_persona_slug`` rule so the
    sidecar path lookups agree with the seed-library lookup: stripping ``_``
    / ``-`` must leave a non-empty ascii-alphanumeric run, so ``..``, ``/``
    and ``\\`` are all rejected before the id is spliced into a path."""
    if not persona_id:
        return False
    stripped = persona_id.replace("_", "").replace("-", "")
    return bool(stripped) and stripped.isascii() and stripped.isalnum()
