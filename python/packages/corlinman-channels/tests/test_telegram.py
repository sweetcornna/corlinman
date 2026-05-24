"""Tests for ``corlinman_channels.telegram``.

Mirrors the unit tests in ``rust/.../telegram/`` (``message.rs``,
``types.rs``, ``service.rs``) and adds an end-to-end test that runs the
long-poll loop against an :class:`httpx.MockTransport`.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from corlinman_channels.common import ConfigError
from corlinman_channels.telegram import (
    Chat,
    Message,
    MessageRoute,
    TelegramAdapter,
    TelegramConfig,
    User,
    binding_from_message,
    classify,
    is_mentioning_bot,
    session_key_for,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def make_message(
    text: str,
    *,
    chat_id: int = 42,
    chat_type: str = "private",
    user_id: int = 77,
    entities: list[dict[str, Any]] | None = None,
    reply_to: dict[str, Any] | None = None,
    message_id: int = 1,
) -> Message:
    raw: dict[str, Any] = {
        "message_id": message_id,
        "from": {"id": user_id, "is_bot": False},
        "chat": {"id": chat_id, "type": chat_type},
        "date": 0,
        "text": text,
    }
    if entities:
        raw["entities"] = entities
    if reply_to:
        raw["reply_to_message"] = reply_to
    return Message.model_validate(raw)


# ---------------------------------------------------------------------------
# Wire-type parsing
# ---------------------------------------------------------------------------


class TestMessageParsing:
    def test_group_message_with_mention_entity(self) -> None:
        m = Message.model_validate({
            "message_id": 42,
            "from": {"id": 555, "is_bot": False, "username": "alice"},
            "chat": {"id": -1001, "type": "supergroup", "title": "hangout"},
            "date": 1_700_000_000,
            "text": "@corlinman_bot hello there",
            "entities": [
                {"type": "mention", "offset": 0, "length": 14},
            ],
        })
        assert m.chat.id == -1001
        assert m.from_ is not None and m.from_.id == 555
        assert len(m.entities) == 1
        assert m.entities[0].entity_type == "mention"

    def test_unknown_entity_type_does_not_fail(self) -> None:
        m = Message.model_validate({
            "message_id": 1,
            "chat": {"id": 10, "type": "private"},
            "date": 1,
            "text": "hello",
            "entities": [{"type": "hashtag", "offset": 0, "length": 5}],
        })
        assert m.entities[0].entity_type == "hashtag"

    def test_largest_photo_picks_by_size(self) -> None:
        m = Message.model_validate({
            "message_id": 1,
            "chat": {"id": 1, "type": "private"},
            "date": 0,
            "photo": [
                {"file_id": "a", "file_size": 100},
                {"file_id": "b", "file_size": 500},
                {"file_id": "c", "file_size": 250},
            ],
        })
        biggest = m.largest_photo()
        assert biggest is not None
        assert biggest.file_id == "b"


# ---------------------------------------------------------------------------
# Mention / classify helpers
# ---------------------------------------------------------------------------


class TestIsMentioningBot:
    def test_mention_entity_with_matching_username(self) -> None:
        m = make_message(
            "@corlinman_bot hello",
            chat_type="supergroup",
            chat_id=-100,
            entities=[{"type": "mention", "offset": 0, "length": 14}],
        )
        assert is_mentioning_bot(m, bot_id=999, bot_username="corlinman_bot")

    def test_mention_entity_with_wrong_username_does_not_match(self) -> None:
        m = make_message(
            "@corlinman_bot hello",
            chat_type="supergroup",
            chat_id=-100,
            entities=[{"type": "mention", "offset": 0, "length": 14}],
        )
        assert not is_mentioning_bot(m, bot_id=999, bot_username="someone_else")

    def test_text_mention_uses_user_id(self) -> None:
        m = Message.model_validate({
            "message_id": 1,
            "from": {"id": 5, "is_bot": False},
            "chat": {"id": 10, "type": "group"},
            "date": 1,
            "text": "hi bot",
            "entities": [{
                "type": "text_mention", "offset": 3, "length": 3,
                "user": {"id": 999, "is_bot": True},
            }],
        })
        assert is_mentioning_bot(m, bot_id=999, bot_username=None)
        assert not is_mentioning_bot(m, bot_id=1, bot_username=None)

    def test_utf16_slice_handles_unicode_offset(self) -> None:
        # "你好 @bot" — Chinese chars are each 1 UTF-16 unit, so @bot
        # starts at offset 3 with length 4.
        m = make_message(
            "你好 @bot",
            chat_type="supergroup",
            chat_id=-100,
            entities=[{"type": "mention", "offset": 3, "length": 4}],
        )
        assert is_mentioning_bot(m, bot_id=999, bot_username="bot")


class TestClassify:
    def test_private_always_responds(self) -> None:
        m = make_message("hi", chat_type="private", chat_id=42)
        assert classify(m, bot_id=999, bot_username="bot") == MessageRoute.PRIVATE

    def test_group_without_mention_ignored(self) -> None:
        m = make_message("hello world", chat_type="supergroup", chat_id=-100)
        assert (
            classify(m, bot_id=999, bot_username="corlinman_bot")
            == MessageRoute.GROUP_IGNORED
        )

    def test_group_entity_mention_addressed(self) -> None:
        m = make_message(
            "@corlinman_bot hello",
            chat_type="supergroup",
            chat_id=-100,
            entities=[{"type": "mention", "offset": 0, "length": 14}],
        )
        assert (
            classify(m, bot_id=999, bot_username="corlinman_bot")
            == MessageRoute.GROUP_ADDRESSED
        )

    def test_group_substring_mention_fallback(self) -> None:
        # Forwarded message that stripped entities — substring fallback kicks in.
        m = make_message(
            "hey @CorlinMan_Bot please help",
            chat_type="supergroup",
            chat_id=-100,
        )
        assert (
            classify(m, bot_id=999, bot_username="corlinman_bot")
            == MessageRoute.GROUP_ADDRESSED
        )

    def test_reply_to_bot_is_addressed(self) -> None:
        m = Message.model_validate({
            "message_id": 2,
            "from": {"id": 77, "is_bot": False},
            "chat": {"id": -100, "type": "supergroup"},
            "date": 0,
            "text": "yes please",
            "reply_to_message": {
                "message_id": 1,
                "from": {"id": 999, "is_bot": True, "username": "corlinman_bot"},
                "chat": {"id": -100, "type": "supergroup"},
                "date": 0,
                "text": "Need anything?",
            },
        })
        assert (
            classify(m, bot_id=999, bot_username="corlinman_bot")
            == MessageRoute.GROUP_ADDRESSED
        )


class TestSessionKey:
    def test_private_uses_user_id(self) -> None:
        m = make_message("hi", chat_type="private", chat_id=42, user_id=42)
        assert session_key_for(m) == "telegram:42:42"

    def test_group_uses_group_suffix(self) -> None:
        m = make_message("hello", chat_type="supergroup", chat_id=-100)
        assert session_key_for(m) == "telegram:-100:group"


class TestBindingFromMessage:
    def test_group_binding(self) -> None:
        m = make_message(
            "hi", chat_type="supergroup", chat_id=-1001, user_id=555
        )
        b = binding_from_message(m, bot_id=999)
        assert b.channel == "telegram"
        assert b.account == "999"
        assert b.thread == "-1001"
        assert b.sender == "555"

    def test_private_binding_uses_chat_id_as_thread(self) -> None:
        m = make_message("hi", chat_type="private", chat_id=77, user_id=77)
        b = binding_from_message(m, bot_id=999)
        assert b.thread == "77"
        assert b.sender == "77"


class TestMessageRoute:
    def test_helpers(self) -> None:
        assert MessageRoute.PRIVATE.should_respond()
        assert MessageRoute.GROUP_ADDRESSED.should_respond()
        assert not MessageRoute.GROUP_IGNORED.should_respond()
        assert not MessageRoute.PRIVATE.is_group()
        assert MessageRoute.GROUP_ADDRESSED.is_group()
        assert MessageRoute.GROUP_IGNORED.is_group()


# ---------------------------------------------------------------------------
# Adapter config
# ---------------------------------------------------------------------------


class TestAdapterConfig:
    def test_empty_token_raises_config_error(self) -> None:
        with pytest.raises(ConfigError):
            TelegramAdapter(TelegramConfig(bot_token=""))


# ---------------------------------------------------------------------------
# End-to-end long-poll tests
# ---------------------------------------------------------------------------


class TestLongPollIntegration:
    async def test_inbound_yields_normalized_event(self, tg_script) -> None:
        tg_script.add_updates([
            {
                "update_id": 1,
                "message": {
                    "message_id": 7,
                    "from": {"id": 77, "is_bot": False, "username": "alice"},
                    "chat": {"id": 77, "type": "private"},
                    "date": 1_700_000_000,
                    "text": "hello bot",
                },
            },
        ])
        adapter = TelegramAdapter(
            TelegramConfig(bot_token="TEST", long_poll_timeout=1),
            http_client=tg_script.client(),
        )
        async with adapter:
            async def first() -> Any:
                async for ev in adapter.inbound():
                    return ev
                return None

            ev = await asyncio.wait_for(first(), timeout=5.0)

        assert ev is not None
        assert ev.channel == "telegram"
        assert ev.binding.account == "999"  # from tg_script.bot_id
        assert ev.binding.thread == "77"
        assert ev.binding.sender == "77"
        assert ev.text == "hello bot"
        assert ev.message_id == "7"
        # Private chats are always implicitly addressed.
        assert ev.mentioned is True

    async def test_allowed_chat_ids_filter(self, tg_script) -> None:
        tg_script.add_updates([
            {
                "update_id": 1,
                "message": {
                    "message_id": 7,
                    "from": {"id": 77, "is_bot": False},
                    "chat": {"id": -100, "type": "supergroup"},
                    "date": 0,
                    "text": "@corlinman_bot hi",
                    "entities": [{"type": "mention", "offset": 0, "length": 14}],
                },
            },
            {
                "update_id": 2,
                "message": {
                    "message_id": 8,
                    "from": {"id": 77, "is_bot": False},
                    "chat": {"id": -200, "type": "supergroup"},
                    "date": 0,
                    "text": "@corlinman_bot hello again",
                    "entities": [{"type": "mention", "offset": 0, "length": 14}],
                },
            },
        ])
        adapter = TelegramAdapter(
            TelegramConfig(
                bot_token="TEST",
                long_poll_timeout=1,
                allowed_chat_ids=[-200],  # only -200 should surface
            ),
            http_client=tg_script.client(),
        )
        async with adapter:
            async def first() -> Any:
                async for ev in adapter.inbound():
                    return ev
                return None

            ev = await asyncio.wait_for(first(), timeout=5.0)

        assert ev is not None
        assert ev.binding.thread == "-200"

    async def test_keyword_filter_drops_unmatched_group_message(self, tg_script) -> None:
        # First update is plain text in group → should be filtered (no
        # mention + no keyword match). Second contains the keyword.
        tg_script.add_updates([
            {
                "update_id": 1,
                "message": {
                    "message_id": 7,
                    "from": {"id": 77, "is_bot": False},
                    "chat": {"id": -100, "type": "supergroup"},
                    "date": 0,
                    "text": "random chatter here",
                },
            },
            {
                "update_id": 2,
                "message": {
                    "message_id": 8,
                    "from": {"id": 77, "is_bot": False},
                    "chat": {"id": -100, "type": "supergroup"},
                    "date": 0,
                    "text": "hey BOT are you there",
                },
            },
        ])
        adapter = TelegramAdapter(
            TelegramConfig(
                bot_token="TEST",
                long_poll_timeout=1,
                keyword_filter=["bot"],
            ),
            http_client=tg_script.client(),
        )
        async with adapter:
            async def first() -> Any:
                async for ev in adapter.inbound():
                    return ev
                return None

            ev = await asyncio.wait_for(first(), timeout=5.0)

        assert ev is not None
        assert ev.text == "hey BOT are you there"

    async def test_empty_text_messages_are_skipped(self, tg_script) -> None:
        tg_script.add_updates([
            {
                "update_id": 1,
                "message": {
                    "message_id": 7,
                    "from": {"id": 77, "is_bot": False},
                    "chat": {"id": 77, "type": "private"},
                    "date": 0,
                    # No text — image-only message, etc.
                },
            },
            {
                "update_id": 2,
                "message": {
                    "message_id": 8,
                    "from": {"id": 77, "is_bot": False},
                    "chat": {"id": 77, "type": "private"},
                    "date": 0,
                    "text": "real text",
                },
            },
        ])
        adapter = TelegramAdapter(
            TelegramConfig(bot_token="TEST", long_poll_timeout=1),
            http_client=tg_script.client(),
        )
        async with adapter:
            async def first() -> Any:
                async for ev in adapter.inbound():
                    return ev
                return None

            ev = await asyncio.wait_for(first(), timeout=5.0)

        assert ev is not None
        assert ev.text == "real text"

    async def test_bot_metadata_is_populated_after_connect(self, tg_script) -> None:
        adapter = TelegramAdapter(
            TelegramConfig(bot_token="TEST", long_poll_timeout=1),
            http_client=tg_script.client(),
        )
        async with adapter:
            assert adapter.bot_id == tg_script.bot_id
            assert adapter.bot_username == tg_script.bot_username


# ---------------------------------------------------------------------------
# User / chat smoke tests
# ---------------------------------------------------------------------------


class TestUserChat:
    def test_chat_is_private_only_for_private_type(self) -> None:
        assert Chat.model_validate({"id": 1, "type": "private"}).is_private()
        assert not Chat.model_validate({"id": 2, "type": "supergroup"}).is_private()

    def test_user_decodes_bare_id(self) -> None:
        u = User.model_validate({"id": 100})
        assert u.id == 100
        assert u.is_bot is False
        assert u.username is None


# ---------------------------------------------------------------------------
# C2 — offset must NOT advance before all updates in a batch are queued.
# ---------------------------------------------------------------------------


class TestOffsetCommitAfterPut:
    """Regression for C2 — Telegram does NOT redeliver past offset, so
    if ``self._offset`` is bumped pre-put and then ``put`` is interrupted
    (cancellation, asyncio.QueueFull, close mid-batch), the un-put
    updates are permanently lost. The fix stages the new offset locally
    and only commits it AFTER every update has been put."""

    @pytest.mark.asyncio
    async def test_offset_not_advanced_when_put_interrupted(
        self, tg_script: Any
    ) -> None:
        """Inject an exception on the second ``put`` of a 3-update
        batch and assert ``self._offset`` did NOT advance past the prior
        value — the next ``getUpdates`` must redo the whole batch."""
        tg_script.add_updates([
            {
                "update_id": 100,
                "message": {
                    "message_id": 1,
                    "from": {"id": 77, "is_bot": False},
                    "chat": {"id": 77, "type": "private"},
                    "date": 0,
                    "text": "msg-100",
                },
            },
            {
                "update_id": 101,
                "message": {
                    "message_id": 2,
                    "from": {"id": 77, "is_bot": False},
                    "chat": {"id": 77, "type": "private"},
                    "date": 0,
                    "text": "msg-101",
                },
            },
            {
                "update_id": 102,
                "message": {
                    "message_id": 3,
                    "from": {"id": 77, "is_bot": False},
                    "chat": {"id": 77, "type": "private"},
                    "date": 0,
                    "text": "msg-102",
                },
            },
        ])
        adapter = TelegramAdapter(
            TelegramConfig(bot_token="TEST", long_poll_timeout=1),
            http_client=tg_script.client(),
        )

        # Resolve bot_id/username then swap the inbound queue for one
        # that raises on the second put — simulates a close/cancel mid-
        # batch where update 100 lands but 101/102 don't.
        me = await adapter._get_me()
        adapter._bot_id = me.id
        adapter._bot_username = me.username

        class _FailingQueue:
            def __init__(self) -> None:
                self.calls = 0
                self.items: list[Any] = []

            async def put(self, item: Any) -> None:
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("simulated interrupt mid-batch")
                self.items.append(item)

        failing = _FailingQueue()
        adapter._inbound_q = failing  # type: ignore[assignment]

        # Snapshot the pre-batch offset.
        prior = adapter._offset

        # Drive one poll iteration directly. The exception inside the
        # batch loop propagates out of _poll_loop — we catch it here.
        with pytest.raises(RuntimeError, match="simulated interrupt"):
            await adapter._poll_loop()

        # Critical: the offset MUST NOT have advanced. The next
        # getUpdates will redeliver all three updates so 101/102 aren't
        # lost.
        assert adapter._offset == prior, (
            f"offset advanced to {adapter._offset} even though only update "
            f"100 was successfully put — 101/102 would be permanently lost"
        )
        # And only one message actually made it onto the queue.
        assert len(failing.items) == 1
        assert failing.items[0].text == "msg-100"

    @pytest.mark.asyncio
    async def test_offset_advances_when_batch_completes(
        self, tg_script: Any
    ) -> None:
        """Happy path: when every update is queued successfully, the
        offset commits to ``last.update_id + 1`` so subsequent polls
        don't reprocess the batch."""
        tg_script.add_updates([
            {
                "update_id": 200,
                "message": {
                    "message_id": 1,
                    "from": {"id": 77, "is_bot": False},
                    "chat": {"id": 77, "type": "private"},
                    "date": 0,
                    "text": "ok",
                },
            },
        ])
        adapter = TelegramAdapter(
            TelegramConfig(bot_token="TEST", long_poll_timeout=1),
            http_client=tg_script.client(),
        )
        async with adapter:
            # Pull one event then close — by then the offset commit ran.
            async def first() -> Any:
                async for ev in adapter.inbound():
                    return ev
                return None

            ev = await asyncio.wait_for(first(), timeout=5.0)
            assert ev is not None
            # Allow the poll loop one more tick so the commit lands.
            await asyncio.sleep(0.05)
            assert adapter._offset == 201


# ---------------------------------------------------------------------------
# callback_query — inline-keyboard press synthesis for ``ask_user`` flows.
# ---------------------------------------------------------------------------


class TestCallbackQuerySynthesis:
    """When an inline-keyboard button (e.g. one rendered by the agent's
    ``ask_user`` tool) is tapped, Telegram pushes a ``callback_query``
    update instead of a ``message``. The adapter must synthesise a
    ``Message`` whose text is the button's ``callback_data`` payload so
    the rest of the inbound pipeline can treat the press identically to
    a typed reply."""

    async def test_callback_query_synthesizes_inbound_event_with_data_as_text(
        self, tg_script
    ) -> None:
        tg_script.add_updates([
            {
                "update_id": 1,
                "callback_query": {
                    "id": "cbq-1",
                    "from": {"id": 77, "is_bot": False, "username": "alice"},
                    "message": {
                        "message_id": 7,
                        "from": {"id": 999, "is_bot": True,
                                 "username": "corlinman_bot"},
                        "chat": {"id": 77, "type": "private"},
                        "date": 1_700_000_000,
                        "text": "Overwrite README.md?",
                    },
                    "data": "yes",
                },
            },
        ])
        adapter = TelegramAdapter(
            TelegramConfig(bot_token="TEST", long_poll_timeout=1),
            http_client=tg_script.client(),
        )
        async with adapter:
            async def first() -> Any:
                async for ev in adapter.inbound():
                    return ev
                return None

            ev = await asyncio.wait_for(first(), timeout=5.0)

        assert ev is not None
        # The callback_data is the user-facing message body now.
        assert ev.text == "yes"
        # Sender is the human who tapped the button (NOT the bot).
        assert ev.binding.sender == "77"
        assert ev.binding.thread == "77"

    async def test_callback_query_in_group_uses_callback_sender(
        self, tg_script
    ) -> None:
        """In a group chat the ``from`` on the embedded message is the
        bot; the synthesised Message must use ``callback_query.from`` as
        the human sender so routing / journal scoping is correct."""
        tg_script.add_updates([
            {
                "update_id": 1,
                "callback_query": {
                    "id": "cbq-2",
                    "from": {"id": 555, "is_bot": False, "username": "carol"},
                    "message": {
                        "message_id": 11,
                        "from": {"id": 999, "is_bot": True,
                                 "username": "corlinman_bot"},
                        "chat": {"id": -1001, "type": "supergroup",
                                 "title": "hangout"},
                        "date": 1_700_000_000,
                        "text": "Pick one.",
                    },
                    "data": "option_b",
                },
            },
        ])
        adapter = TelegramAdapter(
            TelegramConfig(bot_token="TEST", long_poll_timeout=1),
            http_client=tg_script.client(),
        )
        async with adapter:
            async def first() -> Any:
                async for ev in adapter.inbound():
                    return ev
                return None

            ev = await asyncio.wait_for(first(), timeout=5.0)

        assert ev is not None
        assert ev.text == "option_b"
        assert ev.binding.sender == "555"
        assert ev.binding.thread == "-1001"

    async def test_callback_query_missing_data_skipped(
        self, tg_script
    ) -> None:
        """No ``data`` on the callback means nothing to feed back into
        the loop — the synth helper must return None so the update is
        skipped rather than crashing the long-poll loop."""
        tg_script.add_updates([
            {
                "update_id": 1,
                "callback_query": {
                    "id": "cbq-3",
                    "from": {"id": 77, "is_bot": False},
                    "message": {
                        "message_id": 7,
                        "from": {"id": 999, "is_bot": True},
                        "chat": {"id": 77, "type": "private"},
                        "date": 0,
                        "text": "ignore me",
                    },
                    # missing "data"
                },
            },
            {
                "update_id": 2,
                "message": {
                    "message_id": 8,
                    "from": {"id": 77, "is_bot": False},
                    "chat": {"id": 77, "type": "private"},
                    "date": 0,
                    "text": "manual reply",
                },
            },
        ])
        adapter = TelegramAdapter(
            TelegramConfig(bot_token="TEST", long_poll_timeout=1),
            http_client=tg_script.client(),
        )
        async with adapter:
            async def first() -> Any:
                async for ev in adapter.inbound():
                    return ev
                return None

            ev = await asyncio.wait_for(first(), timeout=5.0)

        # The dataless callback was skipped; the next text message is what
        # the iterator yields.
        assert ev is not None
        assert ev.text == "manual reply"
