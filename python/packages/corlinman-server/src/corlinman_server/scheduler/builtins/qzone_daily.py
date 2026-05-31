"""``qzone.daily_publish`` — W6 of ``docs/PLAN_PERSONA_STUDIO.md``.

Drives a one-turn agent chat under a persona's voice and asserts the
turn ends by calling the ``qzone_publish`` tool (W5). The job metadata
carries:

* ``persona_id`` — required, resolves the persona row + asset pack.
* ``prompt_template`` — required, the user-turn instruction (rendered
  verbatim today; future expansion may template ``{{date}}``-style
  fragments).
* ``qq_account`` — optional, informational. Echoed into the history
  payload so operators with multiple bound QQ accounts can match the
  firing to the publisher.

End-to-end flow
---------------

1. Resolve the persona + asset stores. Reuses the live handles parked
   on ``app_state.persona_store`` / ``app_state.persona_asset_store``
   (the entrypoint wires both onto AdminState; this builtin probes
   AppState ``extras`` and admin_a state as a fallback so degraded
   boots that didn't park the handle don't crash). Falls back to
   opening fresh handles against ``<DATA_DIR>/{personas.sqlite,
   persona_assets.sqlite, personas/}`` when no live handle is in
   reach — useful for tests + first-tick recovery before lifecycle
   wiring is complete.
2. Compose the system prompt: ``persona.system_prompt`` + a short tail
   that instructs the agent to end the turn by calling
   ``qzone_publish`` (not by replying with prose). The tail is the
   single load-bearing string in this module — the agent's reasoning
   loop reads the system prompt as ground truth, so the wording here
   IS the contract for "end with a tool call, not a text turn".
3. Build an :class:`InternalChatRequest` with that system prompt + the
   ``prompt_template`` as the user turn. ``session_key`` is scoped
   to the scheduler so memory / approval traces don't bleed across
   the cron firing boundary (every firing starts fresh).
4. Drive ``chat_service.run`` and walk the event stream. We collect
   the first ``qzone_publish`` ``tool_result`` (the one that lands the
   ``tid`` + ``qzone_url`` envelope). The chat is allowed to emit any
   number of intermediate tool calls (``image_with_refs`` is the
   common case) before it lands on ``qzone_publish``.
5. Return an audit dict: ``{ok, tid?, qzone_url?, error?, persona_id,
   qq_account?, duration_ms}``.

Failure surfaces (all return a dict rather than raise — the registry
wraps a raised exception, but the per-failure ``error`` field is
hand-curated so the admin history shows a code rather than a Python
repr):

* ``error="chat_service_unavailable"`` — ``app_state.chat`` is None.
* ``error="persona_store_unavailable"`` — couldn't open the persona
  store (data dir unwritable, etc.).
* ``error="persona_not_found"`` — ``persona_id`` is not in the store.
* ``error="qzone_not_called"`` — the agent finished its turn without
  calling ``qzone_publish``. The audit dict carries
  ``tools_called`` so an operator can spot a model that's stuck in a
  text-only mode.
* ``error="qzone_failed"`` — the ``qzone_publish`` tool returned an
  ``ok=false`` envelope. The audit dict carries ``inner_error`` /
  ``inner_message`` from the tool envelope.
* ``error="chat_error"`` — an :class:`ErrorEvent` came off the
  stream. The audit dict carries the wrapped reason / message.

The dict shape is intentionally JSON-serialisable so the scheduler
history persistence layer can stamp it into ``scheduler_runs`` without
extra encoding.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, cast

from corlinman_server.scheduler.builtins.registry import (
    BuiltinContext,
    register_builtin,
)

_logger = logging.getLogger("corlinman_server.scheduler.builtins.qzone_daily")


__all__ = [
    "QZONE_DAILY_BUILTIN_NAME",
    "QZONE_DAILY_SYSTEM_TAIL",
    "_qzone_daily_publish_action",
]


#: Registered name; the admin UI filters on this for the "QZone daily"
#: tab and the scheduler dispatcher resolves it via the registry.
QZONE_DAILY_BUILTIN_NAME: str = "qzone.daily_publish"


#: Wire-stable tool name we expect the agent to call. Imported lazily
#: from the agent package would force a heavy dependency on this thin
#: scheduler module; the constant is duplicated here on purpose.
_QZONE_PUBLISH_TOOL: str = "qzone_publish"


#: Tail appended to the persona's system_prompt so the agent knows the
#: turn MUST end with a ``qzone_publish`` tool call rather than a prose
#: reply. Plain Chinese mirrors the persona voice and is short enough
#: not to dilute the persona prompt's signal.
QZONE_DAILY_SYSTEM_TAIL: str = (
    "\n\n---\n"
    "[scheduler·qzone.daily_publish 指令]\n"
    "你正在以这个角色的口吻为 QQ 空间撰写今天的「说说」。\n"
    "本轮对话必须以一次 `qzone_publish` 工具调用结束（不要只回复文本）。\n"
    "如果需要配图，先调用 `image_with_refs` 或 `qzone_publish` 自带的 "
    "`generate` 字段。\n"
    "禁止向用户提问；直接输出。"
)


# ---------------------------------------------------------------------------
# Action entry point
# ---------------------------------------------------------------------------


async def _qzone_daily_publish_action(context: BuiltinContext) -> dict[str, Any]:
    """Scheduler builtin entrypoint — drive one daily-QZone agent turn.

    Reads the job metadata off ``context`` (see module docstring for
    the keys). Returns an audit dict shaped for direct persistence to
    the scheduler history. Never raises — every failure path folds
    into the dict.
    """
    metadata = _resolve_metadata(context)
    persona_id = _coerce_str(metadata.get("persona_id"))
    prompt_template = _coerce_str(metadata.get("prompt_template"))
    qq_account = _coerce_optional_str(metadata.get("qq_account"))

    base: dict[str, Any] = {
        "persona_id": persona_id,
        "qq_account": qq_account,
    }

    if not persona_id:
        return {**base, "ok": False, "error": "missing_persona_id"}
    if not prompt_template:
        return {**base, "ok": False, "error": "missing_prompt_template"}

    chat_service = _resolve_chat_service(context)
    if chat_service is None:
        return {**base, "ok": False, "error": "chat_service_unavailable"}

    store_bundle = await _resolve_or_open_persona_stores(context)
    if store_bundle is None:
        return {**base, "ok": False, "error": "persona_store_unavailable"}
    persona_store, _asset_store, owned_handles = store_bundle

    try:
        try:
            persona = await persona_store.get(persona_id)
        except Exception as exc:  # noqa: BLE001 — never raise out of a builtin
            _logger.warning(
                "scheduler.builtin.qzone_daily.persona_get_failed",
                extra={"persona_id": persona_id, "error": repr(exc)},
            )
            return {**base, "ok": False, "error": "persona_store_failed",
                    "message": str(exc)}

        if persona is None:
            return {**base, "ok": False, "error": "persona_not_found"}

        # gap-fill v1.15: bind the persona's *runtime* life-state + recent
        # diary into the system prompt so the daily 说说 reflects what the
        # persona has actually been "living". Read-only + best-effort — a
        # missing life block just composes the bare persona prompt.
        life_block = await _resolve_life_block(context, persona_id)
        system_prompt = _compose_system_prompt(
            persona.system_prompt, life_block=life_block
        )
        model = _resolve_default_model(context)
        session_key = _build_session_key(persona_id)

        request = _build_internal_chat_request(
            model=model,
            session_key=session_key,
            system_prompt=system_prompt,
            user_turn=prompt_template,
        )
        if request is None:
            return {**base, "ok": False,
                    "error": "internal_chat_request_unavailable"}

        cancel = asyncio.Event()
        return await _drive_chat_turn(
            chat_service=chat_service,
            request=request,
            cancel=cancel,
            base=base,
        )
    finally:
        # Close any handles we opened ourselves (fallback path) — never
        # touch live handles parked on the AppState bundle.
        for handle in owned_handles:
            with contextlib.suppress(Exception):
                await handle.close()


# ---------------------------------------------------------------------------
# Chat-stream drive
# ---------------------------------------------------------------------------


async def _drive_chat_turn(
    *,
    chat_service: Any,
    request: Any,
    cancel: asyncio.Event,
    base: dict[str, Any],
) -> dict[str, Any]:
    """Consume the ``chat_service.run`` event stream, harvest the
    ``qzone_publish`` envelope, and shape the audit dict.

    The stream is allowed to emit any number of intermediate
    ``tool_call`` / ``token_delta`` events; we only care about:

    * the first ``qzone_publish`` ``tool_call`` — record its call_id so
      we know which ``tool_result`` corresponds (the agent may call
      multiple tools before / after).
    * any matching ``tool_result`` — this carries the ok/error flag.
      The ``qzone_publish`` tool also emits a textual envelope through
      the chat reply path that we do NOT see here; the ``ToolResultEvent``
      only carries the success/error flag + a short summary. To get the
      actual ``tid`` / ``qzone_url``, we re-invoke the tool's dispatcher
      shape via a sidecar — see :func:`_extract_qzone_envelope`.
    * the terminal ``DoneEvent`` / ``ErrorEvent`` — stops the loop.

    We bound the drive with a generous timeout (``CORLINMAN_QZONE_DAILY_
    TIMEOUT_SECS``, default 300s) so a hung backend can't park the
    scheduler tick loop indefinitely.
    """
    started_at = time.monotonic()
    timeout_secs = _resolve_drive_timeout()

    qzone_call_id: str | None = None
    qzone_result: Any | None = None
    qzone_envelope: dict[str, Any] | None = None
    tools_called: list[str] = []
    chat_error: dict[str, str] | None = None
    finish_reason: str | None = None

    try:
        stream = chat_service.run(request, cancel)
    except Exception as exc:  # noqa: BLE001 — surface as audit dict
        _logger.exception("scheduler.builtin.qzone_daily.run_failed")
        return {
            **base,
            "ok": False,
            "error": "chat_service_failed",
            "message": str(exc),
            "duration_ms": _elapsed_ms(started_at),
        }

    deadline = time.monotonic() + timeout_secs

    async def _consume() -> None:
        nonlocal qzone_call_id, qzone_result, qzone_envelope
        nonlocal finish_reason, chat_error
        async for event in stream:
            kind = _event_kind(event)
            if kind == "tool_call":
                plugin = _event_field(event, "plugin", default="")
                tool = _event_field(event, "tool", default="")
                call_id = _event_field(event, "call_id", default="")
                tools_called.append(f"{plugin}.{tool}" if plugin else tool)
                if tool == _QZONE_PUBLISH_TOOL and qzone_call_id is None:
                    qzone_call_id = str(call_id) if call_id else ""
                    # Capture the raw args bytes so we can fall back to
                    # decoding the agent's intended payload if the
                    # tool_result doesn't expose the publish envelope.
                    qzone_envelope = qzone_envelope or _decode_qzone_args(
                        _event_field(event, "args_json", default=b"")
                    )
            elif kind == "tool_result":
                tool = _event_field(event, "tool", default="")
                if tool == _QZONE_PUBLISH_TOOL and qzone_result is None:
                    qzone_result = event
            elif kind == "done":
                finish_reason = _event_field(event, "finish_reason", default="")
                return
            elif kind == "error":
                inner = _event_field(event, "error", default=None)
                reason = getattr(inner, "reason", "unknown")
                message = getattr(inner, "message", "")
                chat_error = {"reason": str(reason), "message": str(message)}
                return

    try:
        await asyncio.wait_for(_consume(), timeout=max(1.0, deadline - time.monotonic()))
    except TimeoutError:
        cancel.set()
        return {
            **base,
            "ok": False,
            "error": "chat_timeout",
            "tools_called": tools_called,
            "duration_ms": _elapsed_ms(started_at),
        }
    except Exception as exc:  # noqa: BLE001 — defensive
        _logger.exception("scheduler.builtin.qzone_daily.consume_failed")
        return {
            **base,
            "ok": False,
            "error": "chat_service_failed",
            "message": str(exc),
            "tools_called": tools_called,
            "duration_ms": _elapsed_ms(started_at),
        }

    if chat_error is not None:
        return {
            **base,
            "ok": False,
            "error": "chat_error",
            "chat_error_reason": chat_error["reason"],
            "chat_error_message": chat_error["message"],
            "tools_called": tools_called,
            "duration_ms": _elapsed_ms(started_at),
        }

    if qzone_call_id is None:
        return {
            **base,
            "ok": False,
            "error": "qzone_not_called",
            "tools_called": tools_called,
            "finish_reason": finish_reason,
            "duration_ms": _elapsed_ms(started_at),
        }

    if qzone_result is None:
        return {
            **base,
            "ok": False,
            "error": "qzone_no_result",
            "tools_called": tools_called,
            "finish_reason": finish_reason,
            "duration_ms": _elapsed_ms(started_at),
        }

    # ToolResultEvent carries an ``is_error`` flag + ``error_summary``,
    # not the full envelope. The publish envelope (``tid``, ``qzone_url``)
    # lives on the tool result text the agent sees; some implementations
    # park it on a sidecar attribute (e.g. ``payload`` / ``payload_json``)
    # so we probe a small set of conventional fields before falling back
    # to the recorded args.
    is_error = bool(_event_field(qzone_result, "is_error", default=False))
    error_summary = _event_field(qzone_result, "error_summary", default="")
    envelope = _harvest_envelope(qzone_result) or qzone_envelope or {}

    if is_error or (envelope and envelope.get("ok") is False):
        return {
            **base,
            "ok": False,
            "error": "qzone_failed",
            "inner_error": envelope.get("error") if envelope else error_summary,
            "inner_message": envelope.get("message") if envelope else error_summary,
            "tools_called": tools_called,
            "finish_reason": finish_reason,
            "duration_ms": _elapsed_ms(started_at),
        }

    return {
        **base,
        "ok": True,
        "tid": envelope.get("tid"),
        "qzone_url": envelope.get("qzone_url"),
        "uin": envelope.get("uin"),
        "images": envelope.get("images"),
        "generated": envelope.get("generated"),
        "tools_called": tools_called,
        "finish_reason": finish_reason,
        "duration_ms": _elapsed_ms(started_at),
    }


# ---------------------------------------------------------------------------
# Helpers — metadata / handle resolution / event coercion
# ---------------------------------------------------------------------------


def _resolve_metadata(context: BuiltinContext) -> dict[str, Any]:
    """Pull the job's metadata dict off the context.

    Convention: scheduler firings stash the job's ``metadata`` (the
    free-form ``[[scheduler.jobs]].metadata`` table from TOML) onto
    ``context.app_state.scheduler_job_metadata[context.name]`` so the
    builtin can recover it without re-parsing the config. Tests pass
    the dict directly via ``context.app_state.qzone_daily_metadata``
    so the test surface stays small.

    Returns an empty dict when nothing is found — the action then
    surfaces ``missing_persona_id`` / ``missing_prompt_template`` for
    a clean operator error message.
    """
    app_state = context.app_state
    if app_state is None:
        return {}

    # Production seam — a per-job map keyed by name. Checked first so a
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
    direct = getattr(app_state, "qzone_daily_metadata", None)
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


def _resolve_chat_service(context: BuiltinContext) -> Any | None:
    """Find the live ``ChatService`` on the AppState bundle.

    The gateway lifecycle parks the constructed service on
    ``state.chat`` (see ``gateway/services/chat_bootstrap.py``); the
    fallback probes ``state.extras["chat"]`` for tests that build a
    degraded AppState.
    """
    app_state = context.app_state
    if app_state is None:
        return None
    chat = getattr(app_state, "chat", None)
    if chat is not None:
        return chat
    extras = getattr(app_state, "extras", None)
    if isinstance(extras, dict):
        return extras.get("chat")
    return None


async def _resolve_or_open_persona_stores(
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
    data_dir = _resolve_data_dir(app_state)
    if data_dir is None:
        return None
    try:
        from corlinman_server.persona import PersonaAssetStore, PersonaStore
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "scheduler.builtin.qzone_daily.persona_import_failed",
            extra={"error": repr(exc)},
        )
        return None
    try:
        ps = await PersonaStore.open(data_dir / "personas.sqlite")
        owned.append(ps)
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "scheduler.builtin.qzone_daily.persona_open_failed",
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
                "scheduler.builtin.qzone_daily.asset_open_failed",
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


def _resolve_data_dir(app_state: Any | None) -> Path | None:
    if app_state is not None:
        dd = getattr(app_state, "data_dir", None)
        if dd is not None:
            return Path(dd)
    env = os.environ.get("CORLINMAN_DATA_DIR")
    if env:
        return Path(env)
    return None


def _compose_system_prompt(
    persona_prompt: str, *, life_block: str | None = None
) -> str:
    """Glue the persona body + (optional) runtime life block + the
    scheduler tail together.

    The life block is inserted *between* the persona body and the
    scheduler instruction tail so the agent sees "who I am" → "what I've
    been living" → "what to do this turn" in reading order.
    """
    base = (persona_prompt or "").rstrip()
    parts = [base]
    if life_block:
        parts.append(life_block.rstrip())
    parts.append(QZONE_DAILY_SYSTEM_TAIL)
    return "".join(
        # Two blank lines between major blocks; the tail already starts
        # with its own ``\n\n---`` so no extra separator is needed there.
        (f"\n\n{p}" if i and not p.startswith("\n") else p)
        for i, p in enumerate(parts)
    )


async def _resolve_life_block(
    context: BuiltinContext, persona_id: str
) -> str | None:
    """Build a short ``## 我最近的生活`` block from the persona's runtime
    life-state + recent diary tail.

    Resolution order (all best-effort, ``None`` on any miss):

    1. ``app_state.persona_resolver`` — the C2-wired read-only resolver
       over ``agent_state.sqlite``; we read the flat ``life_*`` /
       ``mood`` placeholder keys it exposes.
    2. ``app_state.extras["persona_state_store"]`` /
       ``app_state.corlinman_persona_state_store`` — the open
       :class:`corlinman_persona.store.PersonaStore`; we read the row's
       ``state_json`` (``life.current`` + ``diary`` tail) directly.

    Returns a Chinese system-prompt fragment, or ``None`` when nothing is
    available (the daily post then composes from the bare persona body).
    """
    app_state = context.app_state
    if app_state is None or not persona_id:
        return None

    # Strategy 1: the C2 persona_resolver (flat placeholder keys).
    resolver = getattr(app_state, "persona_resolver", None)
    if resolver is not None and hasattr(resolver, "resolve"):
        try:
            mood = await resolver.resolve("mood", persona_id)
            location = await resolver.resolve("life_location", persona_id)
            activity = await resolver.resolve("life_activity", persona_id)
            state = await resolver.resolve("life_state", persona_id)
            companions = await resolver.resolve("life_companions", persona_id)
            arc = await resolver.resolve("life_story_arc", persona_id)
        except Exception as exc:  # noqa: BLE001 — never break the firing
            _logger.warning(
                "scheduler.builtin.qzone_daily.resolver_failed",
                extra={"persona_id": persona_id, "error": repr(exc)},
            )
        else:
            rows = [
                ("此刻心情", mood),
                ("现在在做", activity),
                ("人在哪", location),
                ("身边有谁", companions),
                ("状态", state),
                ("当前剧情线", arc),
            ]
            resolver_lines = [f"- {label}：{val}" for label, val in rows if val]
            if resolver_lines:
                return "## 我最近的生活（写说说时自然带上，别逐条念）\n" + "\n".join(
                    resolver_lines
                )

    # Strategy 2: read the open runtime persona-state store directly so we
    # can also surface a recent diary tail (the resolver doesn't expose it).
    store = None
    extras = getattr(app_state, "extras", None)
    if isinstance(extras, dict):
        store = extras.get("persona_state_store")
    if store is None:
        store = getattr(app_state, "corlinman_persona_state_store", None)
    if store is not None and hasattr(store, "get"):
        try:
            row = await store.get(persona_id)
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "scheduler.builtin.qzone_daily.state_store_failed",
                extra={"persona_id": persona_id, "error": repr(exc)},
            )
            return None
        if row is None:
            return None
        sj = getattr(row, "state_json", None)
        if not isinstance(sj, dict):
            return None
        raw_life = sj.get("life")
        life = cast(dict[str, Any], raw_life) if isinstance(raw_life, dict) else {}
        raw_current = life.get("current")
        current = cast(dict[str, Any], raw_current) if isinstance(raw_current, dict) else {}
        raw_diary = sj.get("diary")
        diary = cast(list[Any], raw_diary) if isinstance(raw_diary, list) else []
        lines: list[str] = []
        for label, key in (
            ("此刻心情", "mood"),
            ("现在在做", "activity"),
            ("人在哪", "location"),
            ("状态", "state"),
        ):
            val = current.get(key)
            if isinstance(val, str) and val.strip():
                lines.append(f"- {label}：{val.strip()}")
        diary_tail = [d for d in diary[-3:] if d]
        if diary_tail:
            entries = []
            for d in diary_tail:
                if isinstance(d, dict):
                    text = d.get("text") or d.get("entry") or ""
                elif isinstance(d, str):
                    text = d
                else:
                    text = str(d)
                if text:
                    entries.append(f"  · {str(text)[:120]}")
            if entries:
                lines.append("- 最近日记：\n" + "\n".join(entries))
        if lines:
            return "## 我最近的生活（写说说时自然带上，别逐条念）\n" + "\n".join(
                lines
            )
    return None


def _build_session_key(persona_id: str) -> str:
    """Per-firing scheduler-scoped key.

    Including a fresh uuid means consecutive firings don't accidentally
    inherit the previous turn's per-session memory / approvals.
    """
    return f"scheduler:qzone:{persona_id}:{uuid.uuid4().hex[:8]}"


def _resolve_default_model(context: BuiltinContext) -> str:
    """Pick the model to drive the scheduled chat with.

    Resolution chain:

    1. ``app_state.scheduler_default_model`` — explicit override set by
       the scheduler lifecycle (or tests).
    2. ``cfg["models"]["default"]`` — matches the channels-runtime
       resolution, so a daily QZone post uses the same model the bot
       replies with in QQ.
    3. Empty string — the gateway then falls back to its own default;
       the rest of the pipeline tolerates the empty value.
    """
    app_state = context.app_state
    if app_state is not None:
        explicit = getattr(app_state, "scheduler_default_model", None)
        if isinstance(explicit, str) and explicit:
            return explicit
        cfg = getattr(app_state, "config", None)
        if isinstance(cfg, dict):
            models_cfg = cfg.get("models")
            if isinstance(models_cfg, dict):
                model = models_cfg.get("default")
                if isinstance(model, str) and model:
                    return model
    return ""


def _build_internal_chat_request(
    *,
    model: str,
    session_key: str,
    system_prompt: str,
    user_turn: str,
) -> Any | None:
    """Construct an :class:`InternalChatRequest` for the scheduler turn.

    Imported lazily so a degraded gateway boot that excluded
    ``corlinman_server.gateway_api`` doesn't crash the registry import.
    """
    try:
        from corlinman_server.gateway_api.types import (
            InternalChatRequest,
            Message,
            Role,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        _logger.warning(
            "scheduler.builtin.qzone_daily.gateway_api_import_failed",
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
    )


def _resolve_drive_timeout() -> float:
    """Per-firing chat-drive timeout in seconds."""
    raw = os.environ.get("CORLINMAN_QZONE_DAILY_TIMEOUT_SECS", "")
    try:
        return max(10.0, float(raw)) if raw else 300.0
    except ValueError:
        return 300.0


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


# ---------------------------------------------------------------------------
# Event-introspection helpers — tolerate both pydantic / dataclass shapes
# ---------------------------------------------------------------------------


def _event_kind(event: Any) -> str:
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


def _event_field(event: Any, name: str, *, default: Any = None) -> Any:
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


def _decode_qzone_args(args_json: Any) -> dict[str, Any] | None:
    """Decode the ``args_json`` payload on a ``ToolCallEvent``.

    Used as a fallback for the ``qzone_url`` envelope when the
    ``ToolResultEvent`` carries no payload — we at least know what the
    agent intended to publish.
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


def _harvest_envelope(tool_result: Any) -> dict[str, Any] | None:
    """Probe the ``ToolResultEvent`` for the publish envelope.

    The dataclass shape doesn't carry an explicit ``payload`` slot, but
    test doubles + future result shapes may park the parsed JSON
    envelope on a sidecar attribute. Probe a small list of conventional
    field names; return ``None`` when none of them is present.
    """
    for attr in ("payload", "payload_json", "result", "envelope"):
        val = getattr(tool_result, attr, None)
        if val is None and isinstance(tool_result, dict):
            val = tool_result.get(attr)
        if isinstance(val, dict):
            return val
        if isinstance(val, (bytes, str)):
            raw = val.decode("utf-8") if isinstance(val, bytes) else val
            try:
                obj = json.loads(raw)
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(obj, dict):
                return obj
    return None


def _coerce_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _coerce_optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


# Register at import time. ``__init__.py`` imports this module so any
# ``import corlinman_server.scheduler.builtins`` populates the registry.
register_builtin(QZONE_DAILY_BUILTIN_NAME, _qzone_daily_publish_action)
