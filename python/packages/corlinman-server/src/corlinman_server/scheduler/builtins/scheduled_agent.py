"""Shared bounded internal-chat helper for operator-owned scheduled jobs."""

from __future__ import annotations

import asyncio
from typing import Any

from corlinman_server.scheduler.builtins.chat_driver import (
    build_internal_chat_request,
    drive_chat_turn,
    resolve_chat_service,
    resolve_default_model,
    scheduler_context,
)
from corlinman_server.scheduler.builtins.registry import BuiltinContext


async def run_scheduled_agent(
    context: BuiltinContext,
    *,
    system_prompt: str,
    user_turn: str,
    persona_id: str | None = None,
    tools_enabled: bool = True,
    timeout_env: str = "CORLINMAN_SCHEDULED_AGENT_TIMEOUT_SECS",
) -> dict[str, Any]:
    """Run one isolated model turn and return its final text plus audit data."""
    chat = resolve_chat_service(context)
    if chat is None:
        return {"ok": False, "error": "chat_service_unavailable"}
    request = build_internal_chat_request(
        model=resolve_default_model(context),
        session_key=f"scheduler:{context.name or 'job'}:{context.run_id or 'manual'}",
        system_prompt=system_prompt,
        user_turn=user_turn,
        persona_id=persona_id,
        execution_mode=context.execution_mode,
        scheduler_context=scheduler_context(context),
    )
    if request is None:
        return {"ok": False, "error": "internal_chat_request_unavailable"}
    if not tools_enabled:
        request.scheduler_context["tools_disabled"] = "true"
    outcome = await drive_chat_turn(
        chat_service=chat,
        request=request,
        cancel=asyncio.Event(),
        timeout_env=timeout_env,
    )
    if outcome.error:
        return {
            "ok": False,
            "error": outcome.error,
            "duration_ms": outcome.duration_ms,
            "tools_called": outcome.tools_called,
        }
    text = outcome.final_text.strip()
    return {
        "ok": bool(text),
        "text": text,
        "duration_ms": outcome.duration_ms,
        "tools_called": outcome.tools_called,
        "shadow": context.execution_mode == "shadow",
        "delivery_suppressed": context.execution_mode == "shadow",
    }
