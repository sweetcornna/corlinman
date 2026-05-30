"""Tests for the corlinman command system's direct-handler extension
to the slash-command registry.

Covers:

* :class:`CommandSpec` invariants (validate_registry, register_command
  collisions).
* :func:`run_command_handler` flow including admin gating and async
  auto-await.
* :func:`apply_command_prelude` behaviour for handler-only specs.
* Auto-generated ``/help`` content reflects newly registered runtime
  commands.
* ``/whoami`` and ``/status`` happy paths.
"""

from __future__ import annotations

import asyncio

import pytest
from corlinman_channels.commands import (
    COMMAND_REGISTRY,
    CommandContext,
    CommandResult,
    CommandSpec,
    all_specs,
    apply_command_prelude,
    is_command_admin,
    match_command,
    match_command_with_args,
    register_command,
    run_command_handler,
    runtime_registry,
    validate_registry,
)
from corlinman_channels.common import ChannelBinding


@pytest.fixture(autouse=True)
def _reset_runtime_registry():
    """Snapshot + restore the runtime registry around every test so a
    failure mid-suite doesn't pollute downstream cases."""
    snapshot = list(runtime_registry)
    yield
    runtime_registry.clear()
    runtime_registry.extend(snapshot)


def _binding() -> ChannelBinding:
    return ChannelBinding.qq_private(self_id=1, user_id=42)


def _ctx(spec: CommandSpec, *, is_admin: bool = False) -> CommandContext:
    return CommandContext(
        spec=spec,
        raw_text=spec.aliases[0] if spec.aliases else f"/{spec.name}",
        args_text="",
        binding=_binding(),
        is_admin=is_admin,
    )


# ---------------------------------------------------------------------------
# CommandSpec invariants
# ---------------------------------------------------------------------------


class TestRegistryInvariants:
    def test_validate_registry_accepts_builtins(self) -> None:
        validate_registry()

    def test_validate_rejects_spec_with_neither_path(self) -> None:
        bogus = CommandSpec(name="bogus", aliases=("/bogus",), summary="x")
        with pytest.raises(ValueError, match="at least one"):
            validate_registry((*COMMAND_REGISTRY, bogus))

    def test_validate_rejects_duplicate_name(self) -> None:
        dup = CommandSpec(
            name="help",
            aliases=("/foo",),
            summary="x",
            handler=lambda c: CommandResult(reply="x"),
        )
        with pytest.raises(ValueError, match="duplicate command name"):
            validate_registry((*COMMAND_REGISTRY, dup))

    def test_validate_rejects_duplicate_alias(self) -> None:
        dup = CommandSpec(
            name="other",
            aliases=("/help",),
            summary="x",
            handler=lambda c: CommandResult(reply="x"),
        )
        with pytest.raises(ValueError, match="duplicate command alias"):
            validate_registry((*COMMAND_REGISTRY, dup))


class TestRegisterCommand:
    def test_register_new_command_visible_to_matcher(self) -> None:
        spec = CommandSpec(
            name="ping",
            aliases=("/ping",),
            summary="Sanity ping",
            handler=lambda c: CommandResult(reply="pong"),
        )
        register_command(spec)
        assert match_command("/ping") is spec

    def test_register_rejects_name_collision_with_builtin(self) -> None:
        spec = CommandSpec(
            name="help",
            aliases=("/help-clone",),
            summary="x",
            handler=lambda c: CommandResult(reply="x"),
        )
        with pytest.raises(ValueError, match="already registered"):
            register_command(spec)

    def test_register_rejects_alias_collision_with_builtin(self) -> None:
        spec = CommandSpec(
            name="other",
            aliases=("/help",),
            summary="x",
            handler=lambda c: CommandResult(reply="x"),
        )
        with pytest.raises(ValueError, match="already registered"):
            register_command(spec)

    def test_register_rejects_spec_with_neither_path(self) -> None:
        spec = CommandSpec(name="orphan", aliases=("/orphan",), summary="x")
        with pytest.raises(ValueError, match="at least one"):
            register_command(spec)

    def test_all_specs_includes_runtime_specs(self) -> None:
        spec = CommandSpec(
            name="ping",
            aliases=("/ping",),
            summary="x",
            handler=lambda c: CommandResult(reply="pong"),
        )
        register_command(spec)
        assert spec in all_specs()


# ---------------------------------------------------------------------------
# Matching & args
# ---------------------------------------------------------------------------


class TestMatchCommandWithArgs:
    def test_returns_empty_args_on_bare_alias(self) -> None:
        match = match_command_with_args("/help")
        assert match is not None
        spec, args = match
        assert spec.name == "help"
        assert args == ""

    def test_extracts_args_after_alias(self) -> None:
        match = match_command_with_args("/persona edit grantley")
        assert match is not None
        spec, args = match
        assert spec.name == "persona"
        assert args == "edit grantley"

    def test_strips_args_leading_whitespace(self) -> None:
        match = match_command_with_args("/persona      edit")
        assert match is not None
        _spec, args = match
        assert args == "edit"


# ---------------------------------------------------------------------------
# apply_command_prelude behaviour
# ---------------------------------------------------------------------------


class TestApplyCommandPreludeHandlerOnly:
    def test_handler_only_spec_returns_none_prelude(self) -> None:
        whoami = match_command("/whoami")
        assert whoami is not None
        assert whoami.wizard_prelude is None
        # apply_command_prelude returns None when no prelude is set;
        # callers (chat_bootstrap) treat that as "no rewrite".
        assert apply_command_prelude("/whoami", whoami) is None

    def test_dual_path_spec_returns_prelude(self) -> None:
        # /help carries both a handler AND a prelude — the prelude path
        # still works for playground callers that prefer LLM relay.
        helpspec = match_command("/help")
        assert helpspec is not None
        assert helpspec.handler is not None
        assert helpspec.wizard_prelude is not None
        out = apply_command_prelude("/help", helpspec)
        assert out == helpspec.wizard_prelude


# ---------------------------------------------------------------------------
# run_command_handler
# ---------------------------------------------------------------------------


class TestRunCommandHandler:
    async def test_sync_handler_invoked(self) -> None:
        spec = CommandSpec(
            name="ping",
            aliases=("/ping",),
            summary="x",
            handler=lambda c: CommandResult(reply="pong"),
        )
        res = await run_command_handler(spec, _ctx(spec))
        assert res.reply == "pong"

    async def test_async_handler_awaited(self) -> None:
        async def handler(c: CommandContext) -> CommandResult:
            await asyncio.sleep(0)
            return CommandResult(reply="async-pong")

        spec = CommandSpec(
            name="ping",
            aliases=("/ping",),
            summary="x",
            handler=handler,
        )
        res = await run_command_handler(spec, _ctx(spec))
        assert res.reply == "async-pong"

    async def test_admin_only_denied_for_non_admin(self) -> None:
        spec = CommandSpec(
            name="secret",
            aliases=("/secret",),
            summary="x",
            admin_only=True,
            handler=lambda c: CommandResult(reply="never reached"),
        )
        res = await run_command_handler(spec, _ctx(spec, is_admin=False))
        assert res.reply is not None
        assert "admin-only" in res.reply
        assert res.ephemeral is True

    async def test_admin_only_passes_for_admin(self) -> None:
        spec = CommandSpec(
            name="secret",
            aliases=("/secret",),
            summary="x",
            admin_only=True,
            handler=lambda c: CommandResult(reply="ok"),
        )
        res = await run_command_handler(spec, _ctx(spec, is_admin=True))
        assert res.reply == "ok"

    async def test_handler_must_return_command_result(self) -> None:
        spec = CommandSpec(
            name="bad",
            aliases=("/bad",),
            summary="x",
            handler=lambda c: "this is not a CommandResult",  # type: ignore[return-value, arg-type]
        )
        with pytest.raises(TypeError, match="CommandResult"):
            await run_command_handler(spec, _ctx(spec))


# ---------------------------------------------------------------------------
# Built-in handler behaviour
# ---------------------------------------------------------------------------


class TestBuiltinHandlers:
    async def test_help_lists_builtin_commands(self) -> None:
        spec = match_command("/help")
        assert spec is not None
        assert spec.handler is not None
        res = await run_command_handler(spec, _ctx(spec))
        assert res.reply is not None
        # All built-in primary aliases appear.
        for builtin in COMMAND_REGISTRY:
            primary = builtin.aliases[0] if builtin.aliases else builtin.name
            assert primary in res.reply

    async def test_help_includes_runtime_registered_commands(self) -> None:
        new_spec = CommandSpec(
            name="metrics",
            aliases=("/metrics",),
            summary="Show internal metrics",
            category="Info",
            handler=lambda c: CommandResult(reply="42"),
        )
        register_command(new_spec)
        helpspec = match_command("/help")
        assert helpspec is not None
        res = await run_command_handler(helpspec, _ctx(helpspec))
        assert res.reply is not None
        assert "/metrics" in res.reply
        assert "Show internal metrics" in res.reply

    async def test_help_hides_admin_commands_from_non_admin(self) -> None:
        register_command(
            CommandSpec(
                name="kill",
                aliases=("/kill",),
                summary="Force-stop the bot",
                category="Admin",
                admin_only=True,
                handler=lambda c: CommandResult(reply="x"),
            )
        )
        helpspec = match_command("/help")
        assert helpspec is not None
        res = await run_command_handler(helpspec, _ctx(helpspec, is_admin=False))
        assert res.reply is not None
        assert "/kill" not in res.reply

    async def test_help_shows_admin_commands_to_admin(self) -> None:
        register_command(
            CommandSpec(
                name="kill",
                aliases=("/kill",),
                summary="Force-stop the bot",
                category="Admin",
                admin_only=True,
                handler=lambda c: CommandResult(reply="x"),
            )
        )
        helpspec = match_command("/help")
        assert helpspec is not None
        res = await run_command_handler(helpspec, _ctx(helpspec, is_admin=True))
        assert res.reply is not None
        assert "/kill" in res.reply

    async def test_whoami_returns_binding_fields(self) -> None:
        spec = match_command("/whoami")
        assert spec is not None
        b = ChannelBinding.qq_group(self_id=1, group_id=999, user_id=42)
        ctx = CommandContext(
            spec=spec,
            raw_text="/whoami",
            args_text="",
            binding=b,
            is_admin=False,
        )
        res = await run_command_handler(spec, ctx)
        assert res.reply is not None
        assert "qq" in res.reply
        assert "999" in res.reply
        assert "42" in res.reply
        assert b.session_key() in res.reply

    async def test_status_returns_something(self) -> None:
        spec = match_command("/status")
        assert spec is not None
        res = await run_command_handler(spec, _ctx(spec))
        assert res.reply is not None
        assert len(res.reply) > 0


# ---------------------------------------------------------------------------
# /status shareable-link enrichment
# ---------------------------------------------------------------------------


class TestStatusLink:
    """``/status`` appends the caller's signed status-card link only when
    the feature is configured (``service.configure_status_links``).

    The link helper lives in :mod:`corlinman_channels.service` and is
    driven by module-level globals; we reset them to defaults in teardown
    so feature state doesn't leak into sibling tests."""

    @pytest.fixture(autouse=True)
    def _reset_status_links(self):
        from corlinman_channels.service import configure_status_links

        yield
        # Disable the feature (defaults) so we don't leak into siblings.
        configure_status_links()

    async def test_feature_off_has_no_link(self) -> None:
        from corlinman_channels.service import configure_status_links

        configure_status_links()  # defaults: disabled
        spec = match_command("/status")
        assert spec is not None
        res = await run_command_handler(spec, _ctx(spec))
        assert res.reply is not None
        assert "corlinman online" in res.reply
        assert "/status/" not in res.reply

    async def test_feature_on_appends_caller_link(self) -> None:
        from corlinman_channels.service import configure_status_links

        configure_status_links(
            public_url="https://x",
            enabled=True,
            minter=lambda sk: "TOK",
        )
        spec = match_command("/status")
        assert spec is not None
        res = await run_command_handler(spec, _ctx(spec))
        assert res.reply is not None
        assert "corlinman online" in res.reply
        assert "https://x/status/TOK" in res.reply

    def _revoke_ctx(self, *, is_admin: bool) -> CommandContext:
        spec = match_command("/status")
        assert spec is not None
        return CommandContext(
            spec=spec,
            raw_text="/status revoke",
            args_text="revoke",
            binding=_binding(),
            is_admin=is_admin,
        )

    async def test_revoke_requires_admin(self) -> None:
        spec = match_command("/status")
        assert spec is not None
        res = await run_command_handler(spec, self._revoke_ctx(is_admin=False))
        assert res.reply is not None
        assert "管理员" in res.reply
        # A non-admin revoke must not produce a status link or pretend success.
        assert "已吊销" not in res.reply

    async def test_revoke_admin_bumps_epoch(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
        from corlinman_server.gateway.status_revocation import current_epoch

        before = current_epoch(tmp_path, _binding().session_key())
        spec = match_command("/status")
        assert spec is not None
        res = await run_command_handler(spec, self._revoke_ctx(is_admin=True))
        assert res.reply is not None
        assert "已吊销" in res.reply
        after = current_epoch(tmp_path, _binding().session_key())
        assert after == before + 1


# ---------------------------------------------------------------------------
# Admin gate via env var
# ---------------------------------------------------------------------------


class TestIsCommandAdmin:
    def test_unset_env_allows_everyone(self, monkeypatch) -> None:
        monkeypatch.delenv("CORLINMAN_COMMAND_ADMINS", raising=False)
        assert is_command_admin(_binding()) is True

    def test_empty_env_allows_everyone(self, monkeypatch) -> None:
        monkeypatch.setenv("CORLINMAN_COMMAND_ADMINS", "  ")
        assert is_command_admin(_binding()) is True

    def test_sender_in_list_returns_true(self, monkeypatch) -> None:
        monkeypatch.setenv(
            "CORLINMAN_COMMAND_ADMINS", "qq:42,telegram:7"
        )
        assert is_command_admin(_binding()) is True

    def test_sender_not_in_list_returns_false(self, monkeypatch) -> None:
        monkeypatch.setenv("CORLINMAN_COMMAND_ADMINS", "qq:7,telegram:99")
        assert is_command_admin(_binding()) is False

    def test_handles_whitespace_in_list(self, monkeypatch) -> None:
        monkeypatch.setenv(
            "CORLINMAN_COMMAND_ADMINS", "  qq:42 , telegram:7 "
        )
        assert is_command_admin(_binding()) is True
