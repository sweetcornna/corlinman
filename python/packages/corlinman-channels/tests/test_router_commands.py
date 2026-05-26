"""Tests for the W8 slash-command substitution wired into
``ChannelRouter.dispatch``.

Asserts:

* When the routed text matches a registered command, the resulting
  :class:`RoutedRequest.content` is the registry's ``wizard_prelude``
  (the literal command never reaches the agent — it stays on the
  inbox row only).
* When the text does NOT match (plain prose), ``content`` is
  byte-identical to today's behaviour.
* ``enable_commands=False`` opts out, leaving the legacy byte-identical
  behaviour even for text that would otherwise match.
"""

from __future__ import annotations

from corlinman_channels.commands import COMMAND_REGISTRY, match_command
from corlinman_channels.onebot import (
    MessageEvent,
    MessageSegment,
    MessageType,
    Sender,
    TextSegment,
)
from corlinman_channels.router import ChannelRouter


def _group_event(
    raw: str,
    segs: list[MessageSegment],
    gid: int = 9999,
    *,
    user_id: int = 200,
    self_id: int = 100,
    message_id: int = 1,
) -> MessageEvent:
    return MessageEvent(
        self_id=self_id,
        message_type=MessageType.GROUP,
        sub_type="normal",
        group_id=gid,
        user_id=user_id,
        message_id=message_id,
        message=segs,
        raw_message=raw,
        time=1_700_000_000,
        sender=Sender(),
    )


def _persona_prelude() -> str:
    spec = next(s for s in COMMAND_REGISTRY if s.name == "persona")
    return spec.wizard_prelude


def _help_prelude() -> str:
    spec = next(s for s in COMMAND_REGISTRY if s.name == "help")
    return spec.wizard_prelude


# ---------------------------------------------------------------------------
# Command substitution
# ---------------------------------------------------------------------------


class TestCommandSubstitution:
    def test_persona_command_substitutes_content(self) -> None:
        router = ChannelRouter(group_keywords={}, self_ids=[100])
        ev = _group_event("/persona", [TextSegment(text="/persona")])

        req = router.dispatch(ev)

        assert req is not None
        assert req.content == _persona_prelude()
        # The message_id / binding / mention semantics are unaffected;
        # only the agent-facing ``content`` is rewritten.
        assert req.binding.thread == "9999"
        assert req.mentioned is False

    def test_chinese_alias_substitutes(self) -> None:
        router = ChannelRouter(group_keywords={}, self_ids=[100])
        ev = _group_event("/角色", [TextSegment(text="/角色")])

        req = router.dispatch(ev)

        assert req is not None
        assert req.content == _persona_prelude()

    def test_help_command_substitutes(self) -> None:
        router = ChannelRouter(group_keywords={}, self_ids=[100])
        ev = _group_event("/help", [TextSegment(text="/help")])

        req = router.dispatch(ev)

        assert req is not None
        assert req.content == _help_prelude()

    def test_persona_with_args_substitutes(self) -> None:
        router = ChannelRouter(group_keywords={}, self_ids=[100])
        ev = _group_event(
            "/persona edit grantley",
            [TextSegment(text="/persona edit grantley")],
        )

        req = router.dispatch(ev)

        assert req is not None
        # Today the prelude is verbatim regardless of args; sanity-check
        # the matcher actually fired for the prefix form.
        assert match_command("/persona edit grantley") is not None
        assert req.content == _persona_prelude()


# ---------------------------------------------------------------------------
# Non-command path — byte-identical to legacy
# ---------------------------------------------------------------------------


class TestNonCommandUntouched:
    def test_plain_prose_content_unchanged(self) -> None:
        router = ChannelRouter(group_keywords={}, self_ids=[100])
        ev = _group_event(
            "请帮我看看这条消息",
            [TextSegment(text="请帮我看看这条消息")],
        )

        req = router.dispatch(ev)

        assert req is not None
        assert req.content == "请帮我看看这条消息"

    def test_substring_does_not_trigger_substitution(self) -> None:
        # "/persona" appearing inside prose must not be rewritten — the
        # matcher's substring-rejection contract is what makes the agent
        # safe to discuss commands without invoking them.
        router = ChannelRouter(group_keywords={}, self_ids=[100])
        ev = _group_event(
            "please run /persona for me",
            [TextSegment(text="please run /persona for me")],
        )

        req = router.dispatch(ev)

        assert req is not None
        assert req.content == "please run /persona for me"


# ---------------------------------------------------------------------------
# enable_commands opt-out
# ---------------------------------------------------------------------------


class TestEnableCommandsOptOut:
    def test_disabling_preserves_legacy_content(self) -> None:
        router = ChannelRouter(group_keywords={}, self_ids=[100])
        ev = _group_event("/persona", [TextSegment(text="/persona")])

        req = router.dispatch(ev, enable_commands=False)

        assert req is not None
        # Opt-out → byte-identical to pre-W8 behaviour.
        assert req.content == "/persona"
        assert req.content != _persona_prelude()
