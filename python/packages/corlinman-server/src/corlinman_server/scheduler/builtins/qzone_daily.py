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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from corlinman_server.scheduler.builtins.registry import (
    BuiltinContext,
    register_builtin,
)

_logger = logging.getLogger("corlinman_server.scheduler.builtins.qzone_daily")


__all__ = [
    "QZONE_DAILY_BUILTIN_NAME",
    "QZONE_DAILY_DIVERSITY_TAIL",
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


#: B4 anti-formulaic tail — used in place of :data:`QZONE_DAILY_SYSTEM_TAIL`
#: when the ``diversity`` engine is on (the default). Superset of the plain
#: tail (still demands the turn end with ``qzone_publish``) plus the three
#: diversity requirements: (1) topic/scene/opening must differ from every
#: entry in the "最近已发过的说说" block, (2) the "今日灵感种子" block is
#: optional but at least ONE fresh angle is mandatory, (3) a ⚠ life-rhythm
#: nudge takes priority and should be acted on via the persona_life tools so
#: the persona's life actually advances. Deliberately avoids the literal
#: "生活节奏提示" so it doesn't collide with the life-block marker (uses
#: "生活节奏提醒").
QZONE_DAILY_DIVERSITY_TAIL: str = (
    "\n\n---\n"
    "[scheduler·qzone.daily_publish 指令]\n"
    "你正在以这个角色的口吻为 QQ 空间撰写今天的「说说」。\n"
    "本轮对话必须以一次 `qzone_publish` 工具调用结束（不要只回复文本）。\n"
    "如果需要配图，先调用 `image_with_refs` 或 `qzone_publish` 自带的 "
    "`generate` 字段。\n"
    "禁止向用户提问；直接输出。\n"
    "\n"
    "【今天必须不一样，别每天都是那点东西】\n"
    "- 主题、场景、开头句式都必须和上面『最近已发过的说说』里的任何一条不同；"
    "别重复同样的心情、套路和口头禅。\n"
    "- 上面的『今日灵感种子』可用可不用，但今天至少要换一个新的切入点。\n"
    "- 如果上面出现了以 ⚠ 开头的生活节奏提醒，优先按它来写这条说说，"
    "并顺手调用 `persona_life_set_state` / `persona_life_event_seed` "
    "推进自己的生活状态，让接下来的日子真的动起来。"
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
        # gap-fill B4: the diversity engine. ``diversity`` (metadata, default
        # on) gates the inspiration seed (4a) + anti-repeat recent-posts block
        # (4b) + the anti-formulaic tail (4c); ``diversity=False`` rolls the
        # whole thing back to the pre-B4 behavior (no extra blocks, plain
        # tail, no post-log write). Every component is best-effort — a miss
        # simply omits that block.
        diversity = _resolve_diversity(metadata)
        data_dir = _resolve_data_dir(context.app_state)
        seed_block: str | None = None
        recent_posts_block: str | None = None
        if diversity:
            seed_block = await _resolve_seed_block(persona_id, data_dir)
            recent_posts_block = _resolve_recent_posts_block(
                data_dir, persona_id, _resolve_recent_posts_n(metadata)
            )
        system_prompt = _compose_system_prompt(
            persona.system_prompt,
            life_block=life_block,
            seed_block=seed_block,
            recent_posts_block=recent_posts_block,
            diversity=diversity,
        )
        model = _resolve_default_model(context)
        session_key = _build_session_key(persona_id)

        request = _build_internal_chat_request(
            model=model,
            session_key=session_key,
            system_prompt=system_prompt,
            user_turn=prompt_template,
            persona_id=persona_id,
        )
        if request is None:
            return {**base, "ok": False,
                    "error": "internal_chat_request_unavailable"}

        cancel = asyncio.Event()
        result = await _drive_chat_turn(
            chat_service=chat_service,
            request=request,
            cancel=cancel,
            base=base,
        )
        # 4b: record a successful publish into the anti-repeat post-log so the
        # next firing can steer away from it. Diversity-gated + best-effort:
        # ``diversity=False`` keeps no post-log, and a write failure is
        # swallowed (the post already landed — the log is only steering fuel).
        if diversity and isinstance(result, dict) and result.get("ok"):
            _record_post_log(
                data_dir=data_dir,
                persona_id=persona_id,
                job=context.name,
                result=result,
            )
        return result
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
    * any matching ``tool_result`` — this carries the ok/error flag plus
      the tool's parsed result envelope on ``payload_json``. That is
      where the actual ``tid`` / ``qzone_url`` live; we recover them via
      :func:`_harvest_envelope`, falling back to the decoded input args
      (which at least know the intended ``text``).
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

    # ToolResultEvent carries an ``is_error`` flag + ``error_summary``
    # plus the tool's parsed result envelope on ``payload_json``. The
    # publish envelope (``tid``, ``qzone_url``) lives there. Union the
    # decoded input args (intent — has ``text``) with the harvested
    # result so the result's fields win where both are present.
    is_error = bool(_event_field(qzone_result, "is_error", default=False))
    error_summary = _event_field(qzone_result, "error_summary", default="")
    envelope = {**(qzone_envelope or {}), **_harvest_envelope(qzone_result)}

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
        # Published body — harvested from the envelope, else the decoded
        # input args (intent). Surfaced for a future post-log feature.
        "text": envelope.get("text"),
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
    persona_prompt: str,
    *,
    life_block: str | None = None,
    seed_block: str | None = None,
    recent_posts_block: str | None = None,
    diversity: bool = True,
) -> str:
    """Glue the persona body + (optional) runtime life block + (B4 diversity)
    inspiration-seed + recent-posts blocks + the scheduler tail together.

    Reading order: persona body → life block → 今日灵感种子 (4a) → 最近已发过的
    说说 (4b) → the tail. The agent sees "who I am" → "what I've been living"
    → "today's fresh angle" → "what NOT to repeat" → "what to do this turn".

    ``diversity=False`` rolls back to the pre-B4 shape: the seed + recent-posts
    blocks are dropped and the plain :data:`QZONE_DAILY_SYSTEM_TAIL` is used
    instead of :data:`QZONE_DAILY_DIVERSITY_TAIL` (the caller also skips the
    post-log write in that mode).
    """
    base = (persona_prompt or "").rstrip()
    parts = [base]
    if life_block:
        parts.append(life_block.rstrip())
    if diversity:
        if seed_block:
            parts.append(seed_block.rstrip())
        if recent_posts_block:
            parts.append(recent_posts_block.rstrip())
        tail = QZONE_DAILY_DIVERSITY_TAIL
    else:
        tail = QZONE_DAILY_SYSTEM_TAIL
    parts.append(tail)
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
        # gap-fill B2: append the life-rhythm signals (best-effort). Two
        # "节奏" lines, plus one priority nudge line when a threshold trips.
        signals = _life_signals(life, datetime.now(UTC).astimezone())
        dics = signals.get("days_in_current_state")
        if isinstance(dics, int):
            lines.append(f"- 当前状态已持续：{dics} 天")
        dslo = signals.get("days_since_last_outing")
        if isinstance(dslo, int):
            lines.append(f"- 距上次外出：{dslo} 天")
        nudge = signals.get("life_nudge")
        if isinstance(nudge, dict):
            msg = nudge.get("message")
            if isinstance(msg, str) and msg.strip():
                lines.append(f"⚠ 生活节奏提示（优先响应）：{msg.strip()}")
        if lines:
            return "## 我最近的生活（写说说时自然带上，别逐条念）\n" + "\n".join(
                lines
            )
    return None


def _life_signals(life: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Compute life-rhythm signals via the agent-side pure helper.

    Lazy + guarded server→agent import: the layering contract allows the
    server→agent direction, but a degraded boot that excluded
    corlinman-agent (or a future refactor of the pure helper) must never
    crash the scheduler. Returns ``{}`` on any miss so the caller simply
    omits the rhythm lines."""
    try:
        from corlinman_agent.persona.life import (  # noqa: PLC0415
            compute_life_signals,
        )
    except Exception:  # noqa: BLE001 — best-effort; scheduler never raises
        return {}
    try:
        result = compute_life_signals(life, now)
    except Exception:  # noqa: BLE001
        return {}
    return result if isinstance(result, dict) else {}


# ---------------------------------------------------------------------------
# B4 diversity engine — inspiration seed (4a) + anti-repeat post-log (4b)
# ---------------------------------------------------------------------------


#: Anti-repeat post-log sidecar layout. One JSON file per persona under
#: ``<DATA_DIR>/qzone_post_log/<persona_id>.json`` holding the last
#: :data:`_POST_LOG_MAX` published bodies so a firing can steer away from
#: repeating itself. Deliberately a tiny owned sidecar rather than the
#: scheduler history ring (in-memory, string-only), the persona diary (the
#: model may never write it), or the live QZone feed (network + auth
#: dependency): the sidecar is the only source that always carries the actual
#: published text with zero extra deps.
_POST_LOG_DIR: str = "qzone_post_log"
_POST_LOG_MAX: int = 30
_POST_LOG_TEXT_CAP: int = 500
_POST_LOG_VERSION: int = 1

#: Default + clamp range for the ``recent_posts_n`` metadata knob (how many
#: recent bodies to surface in the anti-repeat block).
_RECENT_POSTS_N_DEFAULT: int = 7
_RECENT_POSTS_N_MIN: int = 1
_RECENT_POSTS_N_MAX: int = 14

#: Per-line excerpt cap in the recent-posts prompt block. The stored body is
#: already capped at 500 chars; the prompt only needs the opening 主题/句式 to
#: let the model compare against, so we trim harder here.
_RECENT_POST_EXCERPT_CHARS: int = 120


def _resolve_diversity(metadata: dict[str, Any]) -> bool:
    """Read the ``diversity`` metadata knob (default True).

    ``False`` rolls the whole B4 engine back to pre-B4 behavior (no seed
    block, no recent-posts block, the plain publish tail, and no post-log
    write). Any non-bool value falls back to the default so a duck-typed
    metadata dict never accidentally trips the feature off."""
    raw = metadata.get("diversity", True)
    return raw if isinstance(raw, bool) else True


def _resolve_recent_posts_n(metadata: dict[str, Any]) -> int:
    """Read + clamp the ``recent_posts_n`` metadata knob (1-14, default 7)."""
    raw = metadata.get("recent_posts_n")
    if raw is None or isinstance(raw, bool):
        # ``bool`` is an int subclass — a stray ``true`` must not mean 1.
        return _RECENT_POSTS_N_DEFAULT
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _RECENT_POSTS_N_DEFAULT
    return max(_RECENT_POSTS_N_MIN, min(n, _RECENT_POSTS_N_MAX))


def _valid_persona_slug(persona_id: str) -> bool:
    """True iff ``persona_id`` is a safe filename slug (blocks path traversal).

    Mirrors the agent-side ``persona_life._valid_persona_slug`` rule so the
    post-log path lookup agrees with the seed-library lookup: stripping ``_``
    / ``-`` must leave a non-empty ascii-alphanumeric run, so ``..``, ``/``
    and ``\\`` are all rejected before the id is spliced into a path."""
    if not persona_id:
        return False
    stripped = persona_id.replace("_", "").replace("-", "")
    return bool(stripped) and stripped.isascii() and stripped.isalnum()


def _post_log_path(data_dir: Path | None, persona_id: str) -> Path | None:
    """Resolve ``<DATA_DIR>/qzone_post_log/<persona_id>.json`` or ``None``.

    Returns ``None`` when no data dir is wired or ``persona_id`` fails the
    slug guard — either way the whole post-log feature is skipped for the
    firing (read-side + write-side both funnel through here)."""
    if data_dir is None or not _valid_persona_slug(persona_id):
        return None
    return Path(data_dir) / _POST_LOG_DIR / f"{persona_id}.json"


def _read_post_log(path: Path) -> list[dict[str, Any]]:
    """Read the ``posts`` list from a post-log sidecar.

    Best-effort + total: a missing / unreadable / malformed file yields
    ``[]`` so a corrupt sidecar never blocks the firing."""
    try:
        if not path.is_file():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict):
        return []
    posts = raw.get("posts")
    if not isinstance(posts, list):
        return []
    return [p for p in posts if isinstance(p, dict)]


def _record_post_log(
    *,
    data_dir: Path | None,
    persona_id: str,
    job: str | None,
    result: dict[str, Any],
) -> None:
    """Append one published-post record to the sidecar (atomic, last-30).

    Fully best-effort: a bad slug / missing data dir / unwritable path all
    skip silently. The record's ``text`` comes from the audit dict (#150
    forwards the published body) and is capped at :data:`_POST_LOG_TEXT_CAP`.
    Uses the repo's atomic ``write tmp + replace`` dance so a crash mid-write
    can't truncate the sidecar."""
    path = _post_log_path(data_dir, persona_id)
    if path is None:
        return
    posts = _read_post_log(path)
    text = result.get("text")
    entry: dict[str, Any] = {
        "ts": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        "job": job or "",
        "tid": result.get("tid"),
        "qzone_url": result.get("qzone_url"),
        "text": (text if isinstance(text, str) else "")[:_POST_LOG_TEXT_CAP],
    }
    posts.append(entry)
    posts = posts[-_POST_LOG_MAX:]
    payload = json.dumps(
        {"version": _POST_LOG_VERSION, "posts": posts},
        ensure_ascii=False,
        indent=2,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".new")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return


def _resolve_recent_posts_block(
    data_dir: Path | None, persona_id: str, n: int
) -> str | None:
    """Build the ``## 最近已发过的说说`` anti-repeat block from the sidecar.

    Lists the body excerpts of the last ``n`` posts so the model can steer
    away from repeating a topic / scene / opening句式. ``None`` when the
    sidecar is empty / unavailable (the prompt then omits the block)."""
    path = _post_log_path(data_dir, persona_id)
    if path is None:
        return None
    posts = _read_post_log(path)
    if not posts:
        return None
    lines: list[str] = []
    for post in posts[-n:]:
        text = post.get("text")
        if isinstance(text, str) and text.strip():
            snippet = " ".join(text.strip().split())
            lines.append(f"- {snippet[:_RECENT_POST_EXCERPT_CHARS]}")
    if not lines:
        return None
    return "## 最近已发过的说说（禁止重复主题/场景/句式）\n" + "\n".join(lines)


async def _resolve_seed_block(
    persona_id: str, data_dir: Path | None
) -> str | None:
    """Draw one ``persona_life_event_seed(kind=freeform)`` and render it as a
    ``## 今日灵感种子`` block. Best-effort — a missing agent package / empty
    draw simply omits the block."""
    seed = await _draw_event_seed(persona_id, data_dir)
    if not seed:
        return None
    lines = [f"- {key}：{val}" for key, val in seed.items()]
    return "## 今日灵感种子（至少换一个新切入点）\n" + "\n".join(lines)


async def _draw_event_seed(
    persona_id: str, data_dir: Path | None
) -> dict[str, str]:
    """Call the agent-side ``persona_life_event_seed`` dispatcher (freeform)
    and return the drawn ``{category: cue}`` map.

    Lazy + guarded server→agent import (mirrors :func:`_life_signals` and the
    ``persona_life_advance`` builtin): the layering contract allows the
    server→agent direction, but a degraded boot that excluded corlinman-agent
    must never crash the scheduler. Returns ``{}`` on any miss so the caller
    omits the seed block. No persona-state IO — the draw is a pure sample over
    the seed library, so it's cheap."""
    try:
        from corlinman_agent.persona.life import (  # noqa: PLC0415
            dispatch_persona_life_event_seed,
        )
    except Exception:  # noqa: BLE001 — best-effort; scheduler never raises
        return {}
    try:
        raw = await dispatch_persona_life_event_seed(
            args_json=json.dumps({"kind": "freeform"}),
            persona_id=persona_id,
            data_dir=data_dir,
        )
    except Exception:  # noqa: BLE001
        return {}
    try:
        obj = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(obj, dict) or obj.get("ok") is not True:
        return {}
    seed = obj.get("seed")
    if not isinstance(seed, dict):
        return {}
    out: dict[str, str] = {}
    for key, val in seed.items():
        if isinstance(key, str) and isinstance(val, str) and val.strip():
            out[key] = val.strip()
    return out


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
    persona_id: str | None = None,
) -> Any | None:
    """Construct an :class:`InternalChatRequest` for the scheduler turn.

    Imported lazily so a degraded gateway boot that excluded
    ``corlinman_server.gateway_api`` doesn't crash the registry import.

    ``persona_id`` is forwarded onto the request so the agent servicer
    wires ``ChatStart.extra["persona_id"]`` — without it the scheduler-
    fired turn has no persona binding, so persona-life placeholders and
    ``image_with_refs`` / ``qzone_publish`` persona resolution all fall
    back to requiring an explicit ``persona_id`` arg from the model.
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
        persona_id=persona_id,
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


def _harvest_envelope(tool_result: Any) -> dict[str, Any]:
    """Parse the publish envelope off ``ToolResultEvent.payload_json``.

    The gateway forwards the ``qzone_publish`` tool's parsed result
    envelope (the same dict the agent saw) as a JSON string on the
    event's ``payload_json`` field — added in the tid/qzone_url
    observability fix. Decoding it here is how we recover the actual
    ``tid`` / ``qzone_url`` a successful publish returned.

    Tolerant + total: returns an empty dict when the field is absent,
    blank, or malformed, and accepts ``str`` / ``bytes`` payloads. Never
    raises — scheduler builtins fold every failure into the audit dict,
    never up the stack.
    """
    raw = _event_field(tool_result, "payload_json", default="")
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
