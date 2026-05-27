"""Tests for the W8 trailing-user-message rewrite in the web/admin
chat path.

The substitution lives in
:func:`corlinman_server.gateway.services.chat_bootstrap.rewrite_trailing_user_message`
and is consumed by
:func:`corlinman_server.gateway.routes.chat._build_internal_request`
before the OpenAI body becomes an :class:`InternalChatRequest`.

We synthesize the helper directly here instead of spinning up the full
FastAPI route — the helper is the load-bearing seam, and it's what the
W8 contract calls out:

> Before building the InternalChatRequest, if the **last user message**
> content matches a command, substitute its content with the
> wizard_prelude.

We also lock the trailing-only rule: a literal command sitting in an
older user turn must NOT be retroactively rewritten on every reply, or
the wizard would re-trigger forever.
"""

from __future__ import annotations

from corlinman_channels.commands import COMMAND_REGISTRY
from corlinman_server.gateway.routes.chat import ChatMessage, ChatRequest, _build_internal_request
from corlinman_server.gateway.services.chat_bootstrap import (
    apply_command_substitution,
    rewrite_trailing_user_message,
)
from corlinman_server.gateway_api import Message, Role


def _persona_prelude() -> str:
    spec = next(s for s in COMMAND_REGISTRY if s.name == "persona")
    return spec.wizard_prelude


def _help_prelude() -> str:
    spec = next(s for s in COMMAND_REGISTRY if s.name == "help")
    return spec.wizard_prelude


# ---------------------------------------------------------------------------
# apply_command_substitution — thin wrapper sanity
# ---------------------------------------------------------------------------


class TestApplyCommandSubstitution:
    def test_persona_command_rewrites(self) -> None:
        assert apply_command_substitution("/persona") == _persona_prelude()

    def test_persona_with_args_rewrites(self) -> None:
        assert apply_command_substitution("/persona edit grantley") == _persona_prelude()

    def test_plain_prose_unchanged(self) -> None:
        text = "Can you help me think about Grantley's voice?"
        assert apply_command_substitution(text) == text

    def test_empty_string_unchanged(self) -> None:
        assert apply_command_substitution("") == ""

    def test_handler_only_command_synthesises_relay_prelude(self) -> None:
        """Handler-only commands (/whoami, /status) get a synthetic
        prelude that asks the LLM to relay the handler's output
        verbatim. This keeps the playground functional for these
        commands without needing a separate direct-send surface."""
        out = apply_command_substitution("/whoami")
        # The synthetic prelude opens with the SYSTEM-INSERTED marker
        # and includes the handler's output (which lists the binding
        # fields — playground uses a synthetic 'web' binding).
        assert out.startswith("[SYSTEM-INSERTED]")
        assert "/whoami" in out
        # Synthetic binding's channel is 'playground' / 'web'.
        assert "playground" in out or "web" in out

    def test_handler_only_unknown_command_returns_literal(self) -> None:
        # Commands that don't match anything remain untouched.
        out = apply_command_substitution("/no-such-command")
        assert out == "/no-such-command"


# ---------------------------------------------------------------------------
# rewrite_trailing_user_message — Pydantic Message variant
# ---------------------------------------------------------------------------


class TestRewriteTrailingUserMessage:
    def test_trailing_user_persona_command_rewrites(self) -> None:
        messages = [
            Message(role=Role.SYSTEM, content="you are corlinman"),
            Message(role=Role.USER, content="hi"),
            Message(role=Role.ASSISTANT, content="hello!"),
            Message(role=Role.USER, content="/persona"),
        ]
        out = rewrite_trailing_user_message(messages)
        assert out[-1].content == _persona_prelude()
        # Earlier turns untouched.
        assert out[0].content == "you are corlinman"
        assert out[1].content == "hi"
        assert out[2].content == "hello!"

    def test_older_command_in_history_is_not_rewritten(self) -> None:
        """Locks the trailing-only rule: a /persona sitting in an older
        user turn must NOT be retroactively rewritten on every reply.
        Otherwise the wizard would re-trigger on every assistant turn.
        """
        messages = [
            Message(role=Role.USER, content="/persona"),  # historical
            Message(role=Role.ASSISTANT, content="… wizard finished …"),
            Message(role=Role.USER, content="What's the weather?"),
        ]
        out = rewrite_trailing_user_message(messages)
        # Historical /persona left as-is.
        assert out[0].content == "/persona"
        # Trailing user turn left as-is (no command match).
        assert out[-1].content == "What's the weather?"

    def test_trailing_assistant_message_does_not_match(self) -> None:
        """We only look at the trailing **user** message, not the
        trailing message overall. If the assistant happens to be the
        last turn (a tool-loop intermediate state), the prior user
        turn is what counts.
        """
        messages = [
            Message(role=Role.USER, content="/persona"),
            Message(role=Role.ASSISTANT, content="(streaming…)"),
        ]
        out = rewrite_trailing_user_message(messages)
        # The user turn IS the most recent user turn → rewritten.
        assert out[0].content == _persona_prelude()
        # Assistant turn untouched.
        assert out[1].content == "(streaming…)"

    def test_empty_message_list_returns_empty(self) -> None:
        assert rewrite_trailing_user_message([]) == []

    def test_no_user_message_returns_input(self) -> None:
        messages = [
            Message(role=Role.SYSTEM, content="x"),
            Message(role=Role.ASSISTANT, content="y"),
        ]
        out = rewrite_trailing_user_message(messages)
        assert [m.content for m in out] == ["x", "y"]

    def test_non_matching_trailing_user_preserves_identity(self) -> None:
        """When no command matches, the helper returns a list whose
        elements are the original objects (no defensive copy)."""
        last = Message(role=Role.USER, content="plain talk")
        messages = [last]
        out = rewrite_trailing_user_message(messages)
        assert out[0] is last

    def test_help_command_rewrites(self) -> None:
        messages = [Message(role=Role.USER, content="/help")]
        out = rewrite_trailing_user_message(messages)
        assert out[0].content == _help_prelude()


# ---------------------------------------------------------------------------
# End-to-end via _build_internal_request (the actual call site)
# ---------------------------------------------------------------------------


class TestBuildInternalRequestSubstitution:
    def test_trailing_persona_command_substituted_in_internal_request(self) -> None:
        req = ChatRequest(
            model="claude-sonnet-4-7",
            messages=[
                ChatMessage(role="system", content="sys"),
                ChatMessage(role="user", content="/persona"),
            ],
        )
        internal = _build_internal_request(req, session_key="sess-1")
        assert internal.messages[-1].content == _persona_prelude()
        assert internal.messages[-1].role == Role.USER
        assert internal.messages[0].content == "sys"
        # session_key + model wiring preserved.
        assert internal.session_key == "sess-1"
        assert internal.model == "claude-sonnet-4-7"

    def test_trailing_plain_prose_passes_through_internal_request(self) -> None:
        req = ChatRequest(
            model="claude-sonnet-4-7",
            messages=[
                ChatMessage(role="user", content="hello there"),
            ],
        )
        internal = _build_internal_request(req, session_key=None)
        assert internal.messages[-1].content == "hello there"
        # Empty session_key normalises to "".
        assert internal.session_key == ""
