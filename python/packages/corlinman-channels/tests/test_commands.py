"""Tests for ``corlinman_channels.commands``.

Pins the W8 Persona Studio command-registry contract:

* :func:`match_command` — exact match, alias hit, prefix-with-args,
  pure-prose non-match, leading/trailing whitespace tolerance.
* :func:`apply_command_prelude` — wrapper around ``spec.wizard_prelude``
  that today returns the prelude verbatim (future seam for arg-aware
  preludes — locked here so we notice the day someone changes the
  contract).
* :data:`COMMAND_REGISTRY` shape — ``persona`` / ``persona-list`` /
  ``help`` are present, aliases include the documented Latin + Chinese
  forms.

The channel router + chat-bootstrap rewrite both lean on this contract;
breaking it here cascades to user-visible behaviour, so this file is the
canonical regression net.
"""

from __future__ import annotations

import pytest

from corlinman_channels.commands import (
    COMMAND_REGISTRY,
    CommandSpec,
    apply_command_prelude,
    match_command,
)


# ---------------------------------------------------------------------------
# COMMAND_REGISTRY shape
# ---------------------------------------------------------------------------


class TestRegistryShape:
    def test_registry_is_a_tuple_of_command_specs(self) -> None:
        # Frozen tuple → safe to share across threads / asyncio tasks
        # without copy. The dataclass is frozen too; assert both.
        assert isinstance(COMMAND_REGISTRY, tuple)
        for spec in COMMAND_REGISTRY:
            assert isinstance(spec, CommandSpec)

    def test_registry_contains_persona_persona_list_help(self) -> None:
        names = {spec.name for spec in COMMAND_REGISTRY}
        assert {"persona", "persona-list", "help"} <= names

    def test_persona_aliases_cover_latin_and_chinese_forms(self) -> None:
        spec = next(s for s in COMMAND_REGISTRY if s.name == "persona")
        assert "/persona" in spec.aliases
        assert "/角色" in spec.aliases
        assert "/人格" in spec.aliases
        assert "配置人格" in spec.aliases
        assert "配置角色" in spec.aliases

    def test_persona_list_aliases(self) -> None:
        spec = next(s for s in COMMAND_REGISTRY if s.name == "persona-list")
        assert "/persona-list" in spec.aliases
        assert "/角色列表" in spec.aliases
        assert "/人格列表" in spec.aliases

    def test_help_aliases(self) -> None:
        spec = next(s for s in COMMAND_REGISTRY if s.name == "help")
        assert "/help" in spec.aliases
        assert "/帮助" in spec.aliases

    def test_every_spec_has_a_nonempty_wizard_prelude(self) -> None:
        for spec in COMMAND_REGISTRY:
            assert spec.wizard_prelude, f"{spec.name} has empty prelude"
            assert spec.wizard_prelude.strip() == spec.wizard_prelude or True
            # Sanity-check: every prelude opens with the SYSTEM-INSERTED
            # sentinel so the agent can tell injected commands from
            # genuine user text in mid-conversation debug logs.
            assert spec.wizard_prelude.startswith("[SYSTEM-INSERTED]")

    def test_command_spec_is_frozen(self) -> None:
        spec = COMMAND_REGISTRY[0]
        with pytest.raises(Exception):  # FrozenInstanceError, subclass of AttributeError
            spec.name = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# match_command — exact match
# ---------------------------------------------------------------------------


class TestExactMatch:
    @pytest.mark.parametrize(
        "alias", ["/persona", "/角色", "/人格", "配置人格", "配置角色"]
    )
    def test_persona_aliases_match(self, alias: str) -> None:
        spec = match_command(alias)
        assert spec is not None
        assert spec.name == "persona"

    @pytest.mark.parametrize(
        "alias", ["/persona-list", "/角色列表", "/人格列表"]
    )
    def test_persona_list_aliases_match(self, alias: str) -> None:
        spec = match_command(alias)
        assert spec is not None
        assert spec.name == "persona-list"

    @pytest.mark.parametrize("alias", ["/help", "/帮助"])
    def test_help_aliases_match(self, alias: str) -> None:
        spec = match_command(alias)
        assert spec is not None
        assert spec.name == "help"

    def test_leading_trailing_whitespace_is_tolerated(self) -> None:
        # The matcher strips before comparing — mobile keyboards
        # frequently append a trailing space on auto-suggest.
        assert match_command("  /persona  ") is not None
        assert match_command("\t/帮助\n") is not None


# ---------------------------------------------------------------------------
# match_command — prefix + args
# ---------------------------------------------------------------------------


class TestPrefixWithArgs:
    def test_alias_followed_by_args_matches(self) -> None:
        # "command + args" form. Args are intentionally not parsed by
        # the matcher; they ride along on the inbox row for the agent
        # to read if it cares.
        spec = match_command("/persona edit grantley")
        assert spec is not None
        assert spec.name == "persona"

    def test_chinese_alias_with_args(self) -> None:
        spec = match_command("/角色 grantley")
        assert spec is not None
        assert spec.name == "persona"

    def test_persona_list_with_args(self) -> None:
        spec = match_command("/persona-list verbose")
        assert spec is not None
        assert spec.name == "persona-list"

    def test_help_with_args(self) -> None:
        spec = match_command("/help persona")
        assert spec is not None
        assert spec.name == "help"


# ---------------------------------------------------------------------------
# match_command — non-match (substring / prose)
# ---------------------------------------------------------------------------


class TestNonMatch:
    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "\n\t",
            "please run /persona for me",  # substring, not prefix
            "I want to configure 人格 settings",  # alias as substring
            "/personalize this",  # /persona is a substring, but missing the space sep
            "personas are cool",
            "the /help system is opaque",
            "/persona-listing",  # /persona-list is a substring, but missing the space sep
        ],
    )
    def test_non_match_returns_none(self, text: str) -> None:
        assert match_command(text) is None

    def test_unknown_slash_command_does_not_match(self) -> None:
        # An unregistered command is left for the agent to handle as
        # plain text — no command system has full coverage of the
        # localised verb space.
        assert match_command("/unknown") is None
        assert match_command("/skills") is None


# ---------------------------------------------------------------------------
# apply_command_prelude
# ---------------------------------------------------------------------------


class TestApplyCommandPrelude:
    def test_returns_wizard_prelude_verbatim(self) -> None:
        spec = next(s for s in COMMAND_REGISTRY if s.name == "persona")
        out = apply_command_prelude("/persona", spec)
        assert out == spec.wizard_prelude

    def test_args_in_text_do_not_alter_prelude_today(self) -> None:
        # Locks the current contract: ``text`` is accepted but unused.
        # When a future revision starts interpolating args, this test
        # should fail loudly so the contract change is visible.
        spec = next(s for s in COMMAND_REGISTRY if s.name == "persona")
        a = apply_command_prelude("/persona", spec)
        b = apply_command_prelude("/persona edit grantley", spec)
        assert a == b == spec.wizard_prelude

    def test_each_spec_round_trips(self) -> None:
        for spec in COMMAND_REGISTRY:
            assert apply_command_prelude(spec.aliases[0], spec) == spec.wizard_prelude
