"""Cross-channel session commands (/new, /model, /usage) + prefs shims.

These commands must work on EVERY surface (QQ/Telegram/Discord/Slack/
Feishu/web/console), so the tests drive the shared registry directly and
verify the request-builder choke points honour the stored prefs.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from corlinman_channels import binding_prefs
from corlinman_channels.commands import (
    CommandContext,
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
