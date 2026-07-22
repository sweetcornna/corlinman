"""Effect-safe Telegram delivery for scheduled builtins."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from corlinman_server.scheduler.builtins.registry import BuiltinContext


def _resolve_attr(context: BuiltinContext, name: str) -> Any | None:
    for owner in (context.app_state, context.admin_state):
        if owner is None:
            continue
        value = getattr(owner, name, None)
        if value is not None:
            return value
    app_state = context.app_state
    admin_a = getattr(app_state, "corlinman_admin_a_state", None)
    return getattr(admin_a, name, None) if admin_a is not None else None


def _source_identity(context: BuiltinContext) -> tuple[str, str, str]:
    return (
        context.source_system or "corlinman",
        context.source_job_id or context.name or "scheduled-job",
        context.occurrence_key or f"manual:{context.run_id or 'unknown'}",
    )


async def _reserve_effect(
    context: BuiltinContext,
    *,
    effect_kind: str,
    effect_target: str,
) -> tuple[Any | None, Any | None, dict[str, Any] | None]:
    store = _resolve_attr(context, "scheduler_store")
    if store is None:
        return None, None, {"ok": False, "error": "scheduler_store_unavailable"}
    source_system, source_job_id, occurrence_key = _source_identity(context)
    try:
        effect = await store.prepare_effect(
            source_system=source_system,
            source_job_id=source_job_id,
            occurrence_key=occurrence_key,
            effect_kind=effect_kind,
            effect_target=effect_target,
        )
    except Exception as exc:
        return store, None, {
            "ok": False,
            "error": "effect_reservation_blocked",
            "error_type": type(exc).__name__,
        }
    return store, effect, None


async def _finish_effect(
    store: Any,
    effect: Any,
    *,
    state: str,
    receipt: object = None,
    error_code: str | None = None,
) -> bool:
    try:
        await store.complete_effect(
            effect.id,
            state=state,
            receipt=receipt,
            error_code=error_code,
        )
    except Exception:
        return False
    return True


async def deliver_telegram_text(
    context: BuiltinContext,
    *,
    text: str,
    chat_id: int,
    message_thread_id: int | None = None,
) -> dict[str, Any]:
    """Send one text effect, reserving and completing its durable receipt."""
    target = f"chat:{chat_id}:topic:{message_thread_id or 0}"
    if context.execution_mode == "shadow":
        return {
            "ok": True,
            "shadow": True,
            "delivery_suppressed": True,
            "effect_kind": "telegram.message",
            "effect_target": target,
            "text_chars": len(text),
        }

    sender = _resolve_attr(context, "telegram_sender")
    if sender is None:
        return {"ok": False, "error": "telegram_sender_unavailable"}
    store, effect, error = await _reserve_effect(
        context,
        effect_kind="telegram.message",
        effect_target=target,
    )
    if error is not None:
        return error
    assert store is not None and effect is not None

    try:
        message_id = await sender.send_message(
            chat_id,
            text,
            message_thread_id=message_thread_id,
        )
    except Exception as exc:
        # Telegram transport errors are ambiguous: the API may have accepted
        # the request (or an earlier MSG_BREAK bubble) before the response was
        # lost. Block replay until an operator reconciles the remote chat.
        await _finish_effect(
            store,
            effect,
            state="unknown",
            error_code=type(exc).__name__,
        )
        return {
            "ok": False,
            "error": "telegram_send_failed",
            "error_type": type(exc).__name__,
        }

    if not await _finish_effect(
        store,
        effect,
        state="sent",
        receipt={"message_id": int(message_id)},
    ):
        # The message may already be public. Never invite an automatic resend.
        return {
            "ok": False,
            "error": "effect_receipt_unknown",
            "message_id": int(message_id),
        }
    return {
        "ok": True,
        "message_id": int(message_id),
        "effect_kind": "telegram.message",
        "effect_target": target,
    }


async def deliver_telegram_photo(
    context: BuiltinContext,
    *,
    path: Path,
    chat_id: int,
    caption: str | None = None,
    message_thread_id: int | None = None,
) -> dict[str, Any]:
    """Send one local photo with the same effect receipt contract as text."""
    target = f"chat:{chat_id}:topic:{message_thread_id or 0}"
    if context.execution_mode == "shadow":
        return {
            "ok": True,
            "shadow": True,
            "delivery_suppressed": True,
            "effect_kind": "telegram.photo",
            "effect_target": target,
            "caption_chars": len(caption or ""),
        }
    sender = _resolve_attr(context, "telegram_sender")
    if sender is None:
        return {"ok": False, "error": "telegram_sender_unavailable"}
    store, effect, error = await _reserve_effect(
        context,
        effect_kind="telegram.photo",
        effect_target=target,
    )
    if error is not None:
        return error
    assert store is not None and effect is not None
    try:
        from corlinman_channels.telegram_send import PhotoSource

        message_id = await sender.send_photo(
            chat_id,
            PhotoSource.Path(path),
            caption=caption,
            message_thread_id=message_thread_id,
        )
    except Exception as exc:
        # Telegram transport errors are ambiguous: the API may have accepted
        # the request (or an earlier MSG_BREAK bubble) before the response was
        # lost. Block replay until an operator reconciles the remote chat.
        await _finish_effect(
            store,
            effect,
            state="unknown",
            error_code=type(exc).__name__,
        )
        return {
            "ok": False,
            "error": "telegram_send_failed",
            "error_type": type(exc).__name__,
        }
    if not await _finish_effect(
        store,
        effect,
        state="sent",
        receipt={"message_id": int(message_id)},
    ):
        return {
            "ok": False,
            "error": "effect_receipt_unknown",
            "message_id": int(message_id),
        }
    return {
        "ok": True,
        "message_id": int(message_id),
        "effect_kind": "telegram.photo",
        "effect_target": target,
    }
