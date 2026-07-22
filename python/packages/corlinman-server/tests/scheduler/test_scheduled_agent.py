from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from corlinman_server.gateway_api import DoneEvent, TokenDeltaEvent
from corlinman_server.scheduler.builtins.chat_driver import (
    drive_chat_turn,
    resolve_chat_service,
    resolve_default_model,
)
from corlinman_server.scheduler.builtins.registry import BuiltinContext, resolve_data_dir
from corlinman_server.scheduler.builtins.scheduled_agent import run_scheduled_agent


class _ChatService:
    def __init__(self, events: list[Any]) -> None:
        self.events = events
        self.requests: list[Any] = []

    def run(self, request: Any, cancel: asyncio.Event):
        self.requests.append(request)

        async def _stream():
            for event in self.events:
                yield event

        return _stream()


def test_scheduler_resolvers_follow_starlette_state_bridge(tmp_path) -> None:
    chat = object()
    core = SimpleNamespace(
        chat=chat,
        data_dir=tmp_path,
        config={"models": {"default": "live-model"}},
    )
    context = BuiltinContext(
        app_state=SimpleNamespace(corlinman_state=core),
        name="private.summary",
    )
    assert resolve_chat_service(context) is chat
    assert resolve_data_dir(context) == tmp_path
    assert resolve_default_model(context) == "live-model"


async def test_drive_chat_turn_collects_only_visible_text() -> None:
    chat = _ChatService(
        [
            TokenDeltaEvent(text="内部推理", is_reasoning=True),
            TokenDeltaEvent(text="最终"),
            TokenDeltaEvent(text="答案"),
            DoneEvent(finish_reason="stop", usage=None),
        ]
    )
    outcome = await drive_chat_turn(
        chat_service=chat,
        request=object(),
        cancel=asyncio.Event(),
        timeout_env="CORLINMAN_TEST_SCHEDULED_AGENT_TIMEOUT",
    )
    assert outcome.error is None
    assert outcome.final_text == "最终答案"


async def test_scheduled_agent_returns_final_text_and_shadow_marker() -> None:
    chat = _ChatService(
        [
            TokenDeltaEvent(text="  今日摘要  "),
            DoneEvent(finish_reason="stop", usage=None),
        ]
    )
    context = BuiltinContext(
        app_state=SimpleNamespace(chat=chat, scheduler_default_model="test-model"),
        name="private.summary",
        run_id="run-1",
        execution_mode="shadow",
    )
    result = await run_scheduled_agent(
        context,
        system_prompt="system",
        user_turn="summarize",
    )
    assert result == {
        "ok": True,
        "text": "今日摘要",
        "duration_ms": result["duration_ms"],
        "tools_called": [],
        "shadow": True,
        "delivery_suppressed": True,
    }
    assert chat.requests[0].session_key == "scheduler:private.summary:run-1"
    assert chat.requests[0].scheduler_context == {
        "source_system": "corlinman",
        "source_job_id": "private.summary",
        "occurrence_key": "manual:run-1",
        "execution_mode": "shadow",
    }
