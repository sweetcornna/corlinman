"""CMP-07-parity — Discord/Slack/QQ-official/WeChat runners must bootstrap
command extensions.

``register_commands_from_dir`` / ``register_skill_command`` / ``$ARGUMENTS``
were wired for Telegram / QQ-OneBot / Feishu via
``service.bootstrap_command_extensions()`` in v1.15.1, but the run_* paths for
Discord, Slack, QQ-official and WeChat-official never triggered it — so a
``commands/foo.md`` was invokable on Telegram but not on these 4 channels.

Each of these runners constructs its adapter near the top of the run path
(``async with adapter:`` for Discord/Slack/QQ-official; bare construction for
the webhook-driven WeChat adapter). The bootstrap call is therefore wired into
each adapter's ``__init__`` so it fires exactly once per channel start and is
idempotent (the helper guards on ``_COMMAND_EXTENSIONS_LOADED``).

Acceptance: after the adapter is constructed, ``bootstrap_command_extensions``
has been invoked (so the dir/skill commands get registered).
"""

from __future__ import annotations

import pytest
from corlinman_channels import service
from corlinman_channels.discord import DiscordAdapter, DiscordConfig
from corlinman_channels.qq_official import QqOfficialAdapter, QqOfficialConfig
from corlinman_channels.slack import SlackAdapter, SlackConfig
from corlinman_channels.wechat_official import (
    WeChatOfficialAdapter,
    WeChatOfficialConfig,
)


@pytest.fixture
def bootstrap_spy(monkeypatch: pytest.MonkeyPatch):
    """Replace ``service.bootstrap_command_extensions`` with a counting spy.

    The runners use a deferred ``from corlinman_channels.service import
    bootstrap_command_extensions`` (to dodge the service<->channel import
    cycle), so monkeypatching the attribute on ``service`` is observed by the
    deferred import.
    """
    calls = {"n": 0}

    def _spy() -> None:
        calls["n"] += 1

    monkeypatch.setattr(service, "bootstrap_command_extensions", _spy)
    return calls


def test_discord_adapter_bootstraps_commands(bootstrap_spy) -> None:
    DiscordAdapter(DiscordConfig(bot_token="x"))
    assert bootstrap_spy["n"] == 1


def test_slack_adapter_bootstraps_commands(bootstrap_spy) -> None:
    SlackAdapter(SlackConfig(app_token="a", bot_token="b"))
    assert bootstrap_spy["n"] == 1


def test_qq_official_adapter_bootstraps_commands(bootstrap_spy) -> None:
    QqOfficialAdapter(QqOfficialConfig(app_id="a", app_secret="s"))
    assert bootstrap_spy["n"] == 1


def test_wechat_official_adapter_bootstraps_commands(bootstrap_spy) -> None:
    WeChatOfficialAdapter(
        WeChatOfficialConfig(app_id="a", app_secret="s", token="t")
    )
    assert bootstrap_spy["n"] == 1
