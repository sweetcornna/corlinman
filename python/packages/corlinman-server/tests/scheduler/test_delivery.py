from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from corlinman_server.scheduler import SchedulerStore
from corlinman_server.scheduler.builtins.delivery import (
    deliver_telegram_photo,
    deliver_telegram_text,
)
from corlinman_server.scheduler.builtins.registry import BuiltinContext


class _TelegramSender:
    def __init__(self, *, message_id: int = 77, error: Exception | None = None) -> None:
        self.message_id = message_id
        self.error = error
        self.messages: list[tuple[int, str, int | None]] = []
        self.photos: list[tuple[int, Any, str | None, int | None]] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        message_thread_id: int | None = None,
    ) -> int:
        self.messages.append((chat_id, text, message_thread_id))
        if self.error is not None:
            raise self.error
        return self.message_id

    async def send_photo(
        self,
        chat_id: int,
        source: Any,
        *,
        caption: str | None = None,
        message_thread_id: int | None = None,
    ) -> int:
        self.photos.append((chat_id, source, caption, message_thread_id))
        if self.error is not None:
            raise self.error
        return self.message_id


def _context(
    store: Any,
    sender: _TelegramSender,
    *,
    execution_mode: str = "live",
) -> BuiltinContext:
    return BuiltinContext(
        app_state=SimpleNamespace(
            scheduler_store=store,
            telegram_sender=sender,
        ),
        name="operator.job",
        run_id="run-1",
        execution_mode=execution_mode,
        occurrence_key="external:job-1:1234",
        source_system="external",
        source_job_id="job-1",
    )


async def test_deliver_text_records_sent_receipt_and_blocks_duplicate(
    tmp_path: Path,
) -> None:
    store = await SchedulerStore.open(tmp_path / "scheduler.sqlite")
    sender = _TelegramSender(message_id=88)
    context = _context(store, sender)
    try:
        result = await deliver_telegram_text(
            context,
            text="hello",
            chat_id=123,
            message_thread_id=9,
        )
        assert result == {
            "ok": True,
            "message_id": 88,
            "effect_kind": "telegram.message",
            "effect_target": "chat:123:topic:9",
        }
        effect = await store.get_effect(
            source_system="external",
            source_job_id="job-1",
            occurrence_key="external:job-1:1234",
            effect_kind="telegram.message",
            effect_target="chat:123:topic:9",
        )
        assert effect is not None
        assert effect.state == "sent"
        assert effect.receipt_json == {"message_id": 88}

        duplicate = await deliver_telegram_text(
            context,
            text="hello again",
            chat_id=123,
            message_thread_id=9,
        )
        assert duplicate["ok"] is False
        assert duplicate["error"] == "effect_reservation_blocked"
        assert len(sender.messages) == 1
    finally:
        await store.close()


async def test_deliver_photo_records_receipt_and_topic(tmp_path: Path) -> None:
    store = await SchedulerStore.open(tmp_path / "scheduler.sqlite")
    sender = _TelegramSender(message_id=91)
    context = _context(store, sender)
    image = tmp_path / "daily.png"
    image.write_bytes(b"png")
    try:
        result = await deliver_telegram_photo(
            context,
            path=image,
            chat_id=456,
            caption="agenda",
            message_thread_id=17,
        )
        assert result["ok"] is True
        assert result["message_id"] == 91
        assert sender.photos[0][0] == 456
        assert sender.photos[0][1].path == image
        assert sender.photos[0][2:] == ("agenda", 17)
        effect = await store.get_effect(
            source_system="external",
            source_job_id="job-1",
            occurrence_key="external:job-1:1234",
            effect_kind="telegram.photo",
            effect_target="chat:456:topic:17",
        )
        assert effect is not None
        assert effect.state == "sent"
        assert effect.receipt_json == {"message_id": 91}
    finally:
        await store.close()


async def test_deliver_text_marks_transport_failure_unknown(tmp_path: Path) -> None:
    store = await SchedulerStore.open(tmp_path / "scheduler.sqlite")
    sender = _TelegramSender(error=RuntimeError("network detail"))
    context = _context(store, sender)
    try:
        result = await deliver_telegram_text(context, text="hello", chat_id=123)
        assert result == {
            "ok": False,
            "error": "telegram_send_failed",
            "error_type": "RuntimeError",
        }
        effect = await store.get_effect(
            source_system="external",
            source_job_id="job-1",
            occurrence_key="external:job-1:1234",
            effect_kind="telegram.message",
            effect_target="chat:123:topic:0",
        )
        assert effect is not None
        assert effect.state == "unknown"
        assert effect.error_code == "RuntimeError"
        assert effect.receipt_json is None
    finally:
        await store.close()


class _ReceiptFailingStore:
    def __init__(self, inner: SchedulerStore) -> None:
        self.inner = inner

    async def prepare_effect(self, **kwargs: Any) -> Any:
        return await self.inner.prepare_effect(**kwargs)

    async def complete_effect(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("sqlite unavailable")


async def test_deliver_text_reports_receipt_unknown_after_public_send(
    tmp_path: Path,
) -> None:
    inner = await SchedulerStore.open(tmp_path / "scheduler.sqlite")
    sender = _TelegramSender(message_id=99)
    context = _context(_ReceiptFailingStore(inner), sender)
    try:
        result = await deliver_telegram_text(context, text="hello", chat_id=123)
        assert result == {
            "ok": False,
            "error": "effect_receipt_unknown",
            "message_id": 99,
        }
        effect = await inner.get_effect(
            source_system="external",
            source_job_id="job-1",
            occurrence_key="external:job-1:1234",
            effect_kind="telegram.message",
            effect_target="chat:123:topic:0",
        )
        assert effect is not None
        assert effect.state == "prepared"
    finally:
        await inner.close()


class _ExplodingStore:
    async def prepare_effect(self, **_kwargs: Any) -> Any:
        raise AssertionError("shadow delivery must not reserve an effect")


async def test_shadow_delivery_suppresses_transport_and_reservation() -> None:
    sender = _TelegramSender()
    context = _context(_ExplodingStore(), sender, execution_mode="shadow")

    text = await deliver_telegram_text(
        context,
        text="planned",
        chat_id=123,
        message_thread_id=5,
    )
    photo = await deliver_telegram_photo(
        context,
        path=Path("/does/not/need/to/exist.png"),
        chat_id=123,
        caption="planned photo",
        message_thread_id=5,
    )

    assert text == {
        "ok": True,
        "shadow": True,
        "delivery_suppressed": True,
        "effect_kind": "telegram.message",
        "effect_target": "chat:123:topic:5",
        "text_chars": 7,
    }
    assert photo == {
        "ok": True,
        "shadow": True,
        "delivery_suppressed": True,
        "effect_kind": "telegram.photo",
        "effect_target": "chat:123:topic:5",
        "caption_chars": 13,
    }
    assert sender.messages == []
    assert sender.photos == []
