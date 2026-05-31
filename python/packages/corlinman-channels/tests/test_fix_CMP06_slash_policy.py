"""CMP-06 — SlashAccessPolicy must actually be enforced on dispatch.

The policy types (tier_for / allows, DM_ONLY, default_tier) exist and are
exported but no dispatch path calls ``allows()``. Every path gates only on
``spec.admin_only``.

Acceptance:
* A handler command can be locked to admins via a policy
  (``default_tier=ALLOWLIST`` or a per-name ALLOWLIST/ADMIN tier) and a
  non-admin caller is refused without the handler ever running.
* A DM_ONLY command is refused in a group chat (``is_dm=False``).
* With no policy / PUBLIC tier the historical allow-by-default holds.
"""

from __future__ import annotations

import pytest
from corlinman_channels.commands import (
    CommandContext,
    CommandResult,
    CommandSpec,
    SlashAccessPolicy,
    SlashAccessTier,
    run_command_handler,
)
from corlinman_channels.common import ChannelBinding
from corlinman_channels.onebot import (
    MessageEvent,
    MessageType,
    Sender,
    TextSegment,
)
from corlinman_channels.router import ChannelRouter

_RAN: list[str] = []


def _handler(ctx: CommandContext) -> CommandResult:
    _RAN.append(ctx.spec.name)
    return CommandResult(reply="ran")


def _spec(name: str = "secret", admin_only: bool = False) -> CommandSpec:
    return CommandSpec(
        name=name,
        aliases=(f"/{name}",),
        summary="x",
        admin_only=admin_only,
        handler=_handler,
    )


def _binding() -> ChannelBinding:
    return ChannelBinding(channel="qq", account="1", thread="2", sender="3")


@pytest.mark.asyncio
async def test_allowlist_policy_refuses_non_admin_handler() -> None:
    _RAN.clear()
    spec = _spec()
    ctx = CommandContext(
        spec=spec,
        raw_text="/secret",
        args_text="",
        binding=_binding(),
        is_admin=False,
    )
    policy = SlashAccessPolicy(default_tier=SlashAccessTier.ALLOWLIST)
    result = await run_command_handler(spec, ctx, policy=policy, is_dm=False)
    # Before the fix the policy is ignored and the handler runs.
    assert "secret" not in _RAN
    assert (result.reply is not None and "secret" in result.reply.lower()) or (
        result.reply is not None
    )
    assert result.ephemeral is True


@pytest.mark.asyncio
async def test_dm_only_policy_refuses_group() -> None:
    _RAN.clear()
    spec = _spec(name="dmcmd")
    ctx = CommandContext(
        spec=spec,
        raw_text="/dmcmd",
        args_text="",
        binding=_binding(),
        is_admin=True,  # admin, but DM_ONLY must still refuse in a group
    )
    policy = SlashAccessPolicy(
        tiers={"dmcmd": SlashAccessTier.DM_ONLY}
    )
    result = await run_command_handler(spec, ctx, policy=policy, is_dm=False)
    assert "dmcmd" not in _RAN
    assert result.ephemeral is True


@pytest.mark.asyncio
async def test_dm_only_policy_allows_in_dm() -> None:
    _RAN.clear()
    spec = _spec(name="dmcmd2")
    ctx = CommandContext(
        spec=spec,
        raw_text="/dmcmd2",
        args_text="",
        binding=_binding(),
        is_admin=False,
    )
    policy = SlashAccessPolicy(tiers={"dmcmd2": SlashAccessTier.DM_ONLY})
    result = await run_command_handler(spec, ctx, policy=policy, is_dm=True)
    assert _RAN == ["dmcmd2"]
    assert result.reply == "ran"


@pytest.mark.asyncio
async def test_no_policy_allows_by_default() -> None:
    _RAN.clear()
    spec = _spec(name="pub")
    ctx = CommandContext(
        spec=spec,
        raw_text="/pub",
        args_text="",
        binding=_binding(),
        is_admin=False,
    )
    result = await run_command_handler(spec, ctx)
    assert _RAN == ["pub"]
    assert result.reply == "ran"


# ---------------------------------------------------------------------------
# Router-side enforcement for prelude-only commands
# ---------------------------------------------------------------------------


def _group_event(raw: str, gid: int = 9999) -> MessageEvent:
    return MessageEvent(
        self_id=100,
        message_type=MessageType.GROUP,
        sub_type="normal",
        group_id=gid,
        user_id=200,
        message_id=1,
        message=[TextSegment(text=raw)],
        raw_message=raw,
        time=1_700_000_000,
        sender=Sender(),
    )


def test_router_denies_prelude_command_via_policy() -> None:
    # /persona is a prelude-only command. A DM_ONLY policy in a group chat
    # must NOT rewrite content to the prelude — it should surface a refusal
    # notice and leave the agent turn unstarted.
    policy = SlashAccessPolicy(tiers={"persona": SlashAccessTier.DM_ONLY})
    router = ChannelRouter(group_keywords={}, self_ids=[100])
    ev = _group_event("/persona")
    req = router.dispatch(ev, slash_policy=policy)
    assert req is not None
    # Persona prelude must NOT be present — the command was denied.
    assert "SYSTEM-INSERTED" not in (req.content or "")
    # A refusal notice is surfaced and no command_spec is carried forward.
    assert req.command_refused is True
