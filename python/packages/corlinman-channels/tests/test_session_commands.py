"""Cross-channel session commands (/new, /model, /usage) + prefs shims.

These commands must work on EVERY surface (QQ/Telegram/Discord/Slack/
Feishu/web/console), so the tests drive the shared registry directly and
verify the request-builder choke points honour the stored prefs.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_channels import binding_prefs
from corlinman_channels.commands import (
    CommandContext,
    _collect_session_turns,
    _run_command_handler_sync,
    match_command_with_args,
    run_command_handler,
)
from corlinman_channels.common import ChannelBinding

BINDING = ChannelBinding(
    channel="telegram", account="bot", thread="chat1", sender="u1"
)


@pytest.fixture(autouse=True)
def _data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the server-side prefs store at a throwaway data dir."""
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    return tmp_path


def _ctx(raw: str) -> CommandContext:
    match = match_command_with_args(raw)
    assert match is not None, f"no spec matched {raw!r}"
    spec, args_text = match
    return CommandContext(
        spec=spec,
        raw_text=raw,
        args_text=args_text,
        binding=BINDING,
        is_admin=True,
    )


# ── registry presence ──────────────────────────────────────────────────


@pytest.mark.parametrize("alias", ["/new", "/model", "/usage", "/新会话", "/模型"])
def test_session_commands_registered(alias: str) -> None:
    assert match_command_with_args(alias) is not None


# ── /new ───────────────────────────────────────────────────────────────


async def test_new_bumps_epoch_and_changes_session_key() -> None:
    base = BINDING.session_key()
    assert binding_prefs.effective_session_key(BINDING, base) == base

    result = await run_command_handler(_ctx("/new").spec, _ctx("/new"))
    assert result.reply is not None and "新会话" in result.reply

    assert binding_prefs.effective_session_key(BINDING, base) == f"{base}:e1"

    await run_command_handler(_ctx("/new").spec, _ctx("/new"))
    assert binding_prefs.effective_session_key(BINDING, base) == f"{base}:e2"


async def test_new_preserves_bound_persona() -> None:
    binding_prefs.set_persona_id(BINDING, "alice")

    result = await run_command_handler(_ctx("/new").spec, _ctx("/new"))

    assert result.reply is not None and "新会话" in result.reply
    assert binding_prefs.effective_persona_id(BINDING, "grantley") == "alice"


async def test_use_default_persona_clears_bound_persona() -> None:
    binding_prefs.set_persona_id(BINDING, "alice")

    result = await run_command_handler(
        _ctx("/use-default-persona").spec,
        _ctx("/use-default-persona"),
    )

    assert result.reply is not None and "grantley" in result.reply
    assert binding_prefs.effective_persona_id(BINDING, "grantley") == "grantley"


# ── /model ─────────────────────────────────────────────────────────────


async def test_model_set_show_clear() -> None:
    assert binding_prefs.effective_model(BINDING, "default-m") == "default-m"

    set_result = await run_command_handler(
        _ctx("/model my-alias").spec, _ctx("/model my-alias")
    )
    assert set_result.reply is not None and "my-alias" in set_result.reply
    assert binding_prefs.effective_model(BINDING, "default-m") == "my-alias"

    show = await run_command_handler(_ctx("/model").spec, _ctx("/model"))
    assert show.reply is not None and "my-alias" in show.reply

    clear = await run_command_handler(
        _ctx("/model default").spec, _ctx("/model default")
    )
    assert clear.reply is not None
    assert binding_prefs.effective_model(BINDING, "default-m") == "default-m"


# ── /usage ─────────────────────────────────────────────────────────────


async def test_usage_with_no_journal_is_polite() -> None:
    result = await run_command_handler(_ctx("/usage").spec, _ctx("/usage"))
    assert result.reply is not None
    assert "没有任何记录" in result.reply or "不可用" in result.reply


# ── /new on synthetic surfaces (playground / console) ─────────────────


def _surface_ctx(raw: str, channel: str) -> CommandContext:
    match = match_command_with_args(raw)
    assert match is not None, f"no spec matched {raw!r}"
    spec, args_text = match
    return CommandContext(
        spec=spec,
        raw_text=raw,
        args_text=args_text,
        binding=ChannelBinding(
            channel=channel, account="web", thread="web", sender="web"
        ),
        is_admin=True,
    )


async def test_new_on_playground_is_honest() -> None:
    """The HTTP chat route derives its session from body/header, not the
    synthetic playground binding — /new must say so instead of claiming
    a new session started."""
    ctx = _surface_ctx("/new", "playground")
    result = await run_command_handler(ctx.spec, ctx)
    assert result.reply is not None
    assert "新对话" in result.reply  # points at the web new-chat button
    assert "已开启新会话" not in result.reply
    # No epoch bump happened for the synthetic binding.
    prefs = binding_prefs.get_prefs(ctx.binding)
    epoch = int(getattr(prefs, "session_epoch", 0) or 0) if prefs else 0
    assert epoch == 0


async def test_new_on_console_points_at_local_new() -> None:
    ctx = _surface_ctx("/new", "console")
    result = await run_command_handler(ctx.spec, ctx)
    assert result.reply is not None
    assert "控制台" in result.reply
    assert "已开启新会话" not in result.reply


# ── sync-surface dispatch (_run_command_handler_sync) ─────────────────


def test_sync_surface_runs_async_usage_without_loop() -> None:
    """An async handler (/usage) invoked from a loop-free sync surface
    (the web playground's command-substitution path) must execute via
    asyncio.run instead of refusing."""
    ctx = _ctx("/usage")
    result = _run_command_handler_sync(ctx.spec, ctx)
    assert result.reply is not None
    assert "async surface" not in result.reply
    # Fresh data dir → no journal → the handler's polite empty reply
    # (or the soft-dep advisory when corlinman-server is absent).
    assert "没有任何记录" in result.reply or "不可用" in result.reply


async def test_sync_surface_refuses_async_handler_when_loop_running() -> None:
    """With a loop running on this thread, blocking on the coroutine
    would deadlock it — the refusal must be preserved."""
    ctx = _ctx("/usage")
    result = _run_command_handler_sync(ctx.spec, ctx)
    assert result.reply is not None
    assert "async surface" in result.reply


# ── /usage pagination (_collect_session_turns) ─────────────────────────


class _FakeJournal:
    """list_session_turns stub: serves pre-baked pages, records calls."""

    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self._pages = list(pages)
        self.calls: list[tuple[str, int, str | None]] = []

    async def list_session_turns(
        self,
        session_key: str,
        *,
        limit: int = 50,
        before_turn_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append((session_key, limit, before_turn_id))
        if not self._pages:
            return []
        return self._pages.pop(0)


def _rows(start_id: int, n: int) -> list[dict[str, Any]]:
    """``n`` fake turn rows with turn_id descending from ``start_id``."""
    return [
        {"turn_id": start_id - i, "tool_call_count": 1}
        for i in range(n)
    ]


async def test_usage_pagination_follows_cursor_past_first_page() -> None:
    journal = _FakeJournal([_rows(1000, 200), _rows(800, 50)])
    rows, capped = await _collect_session_turns(
        journal, "s", page_size=200, max_pages=25
    )
    assert len(rows) == 250
    assert capped is False
    # Second call threads the cursor (the oldest row of page one).
    assert journal.calls[0] == ("s", 200, None)
    assert journal.calls[1] == ("s", 200, "801")


async def test_usage_pagination_caps_total_pages() -> None:
    journal = _FakeJournal([_rows(1000 - 10 * i, 10) for i in range(99)])
    rows, capped = await _collect_session_turns(
        journal, "s", page_size=10, max_pages=3
    )
    assert len(rows) == 30
    assert capped is True
    assert len(journal.calls) == 3


# ── shared text-channel command dispatch (Discord path) ────────────────


async def test_discord_command_dispatch_replies_via_sender() -> None:
    """A handler command on the Discord inbound path must run the
    handler and ship the reply through the DiscordSender, skipping the
    agent turn — same wiring run_discord_channel uses (mirrors the
    Telegram test in test_telegram.py::test_dispatch_command_handler_replies).
    """
    import functools
    import json

    import httpx
    from corlinman_channels.common import InboundEvent
    from corlinman_channels.discord import DiscordSender
    from corlinman_channels.service import (
        _discord_send_command_reply,
        _try_dispatch_text_command,
    )

    sent_payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages"):
            sent_payloads.append(json.loads(request.content.decode()))
            return httpx.Response(200, json={"id": "999"})
        return httpx.Response(404, json={})

    binding = ChannelBinding(
        channel="discord", account="bot", thread="chan1", sender="u1"
    )
    ev = InboundEvent(
        channel="discord", binding=binding, text="/whoami", message_id="123"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as cli:
        sender = DiscordSender(cli, "TEST")
        handled = await _try_dispatch_text_command(
            ev,
            functools.partial(_discord_send_command_reply, sender, ev),
            channel_label="discord",
        )
        # Plain prose must fall through to the agent path untouched.
        prose = InboundEvent(
            channel="discord", binding=binding, text="hello", message_id="124"
        )
        passthrough = await _try_dispatch_text_command(
            prose,
            functools.partial(_discord_send_command_reply, sender, prose),
            channel_label="discord",
        )
    assert handled is True
    assert passthrough is False
    assert len(sent_payloads) == 1
    body = sent_payloads[0]["content"]
    # /whoami echoes the binding fields.
    assert "discord" in body and "chan1" in body
    # First chunk is threaded onto the inbound message.
    assert sent_payloads[0]["message_reference"] == {"message_id": "123"}


# ── request-builder choke points ───────────────────────────────────────


def test_qq_builder_honours_prefs() -> None:
    from corlinman_channels.service import _build_internal_request

    binding_prefs.set_model_override(BINDING, "override-m")
    binding_prefs.bump_session_epoch(BINDING)

    req = SimpleNamespace(
        content="hello",
        session_key=BINDING.session_key(),
        binding=BINDING,
        sender_name=None,
        reply_to_text=None,
    )
    event = SimpleNamespace(message=[])
    built = _build_internal_request(req, event, "default-m")
    assert built.model == "override-m"
    assert built.session_key == f"{BINDING.session_key()}:e1"


def test_text_builder_honours_prefs() -> None:
    from corlinman_channels.service import _build_text_channel_request

    binding_prefs.set_model_override(BINDING, "override-m")

    inbound = SimpleNamespace(
        text="hi",
        binding=BINDING,
        attachments=[],
        sender_name=None,
        reply_to_text=None,
    )
    built = _build_text_channel_request(inbound, "default-m")
    assert built.model == "override-m"
    assert built.session_key == BINDING.session_key()  # epoch 0 → unchanged


def test_builders_fail_open_without_prefs_row() -> None:
    from corlinman_channels.service import _build_text_channel_request

    fresh = ChannelBinding(
        channel="discord", account="b", thread="t", sender="s"
    )
    inbound = SimpleNamespace(
        text="hi",
        binding=fresh,
        attachments=[],
        sender_name=None,
        reply_to_text=None,
    )
    built = _build_text_channel_request(inbound, "default-m")
    assert built.model == "default-m"
    assert built.session_key == fresh.session_key()
