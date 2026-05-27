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

    def test_every_spec_has_at_least_one_delivery_path(self) -> None:
        for spec in COMMAND_REGISTRY:
            assert spec.wizard_prelude is not None or spec.handler is not None, (
                f"{spec.name} has neither wizard_prelude nor handler"
            )

    def test_every_prelude_uses_system_inserted_sentinel(self) -> None:
        # Sanity-check: every prelude (when present) opens with the
        # SYSTEM-INSERTED sentinel so the agent can tell injected
        # commands from genuine user text in mid-conversation debug logs.
        for spec in COMMAND_REGISTRY:
            if spec.wizard_prelude is None:
                continue
            assert spec.wizard_prelude, f"{spec.name} has empty prelude"
            assert spec.wizard_prelude.startswith("[SYSTEM-INSERTED]")

    def test_command_spec_is_frozen(self) -> None:
        spec = COMMAND_REGISTRY[0]
        with pytest.raises(Exception):  # FrozenInstanceError, subclass of AttributeError
            spec.name = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _PERSONA_WIZARD_PRELUDE — content contract
# ---------------------------------------------------------------------------


class TestPersonaWizardPreludeContract:
    """Lock the staged-materials wizard contract into the prelude text.

    The previous prelude said only "walk them through configuring a
    persona using the persona.* tools" — too soft, and agents would
    open with ``persona_list`` and stop, effectively turning ``/persona``
    into ``/persona-list``. These assertions are the regression net for
    that bug.
    """

    @pytest.fixture
    def prelude(self) -> str:
        spec = next(s for s in COMMAND_REGISTRY if s.name == "persona")
        assert spec.wizard_prelude is not None
        return spec.wizard_prelude

    def test_prelude_forbids_persona_list_as_opening_action(self, prelude: str) -> None:
        # The agent must NOT call persona_list as its first action —
        # that is the exact failure mode that prompted the rewrite.
        assert "persona_list" in prelude
        assert ("Do NOT call `persona_list`" in prelude) or (
            "do not call `persona_list`" in prelude.lower()
        )

    def test_prelude_requires_ask_user_as_first_action(self, prelude: str) -> None:
        # First action must be ask_user (the only branch picker).
        assert "ask_user" in prelude
        assert ("FIRST action" in prelude) or ("first action" in prelude.lower())

    def test_prelude_lists_all_seven_stages(self, prelude: str) -> None:
        # Each stage gets a label; the agent must walk them in order.
        # W2 added Stage 0 (Character Source) before the existing 1-6.
        for i in range(0, 7):
            assert f"Stage {i}" in prelude, f"missing Stage {i} marker"

    def test_prelude_pins_the_four_review_options(self, prelude: str) -> None:
        # The fixed four-option review contract — agent must use
        # exactly these labels on every stage-end ask_user (Stages 1-5).
        for opt in ("确认", "补充", "修改", "重做"):
            assert opt in prelude, f"missing review option: {opt}"

    def test_prelude_defers_persona_create_until_stage_6_confirm(
        self, prelude: str
    ) -> None:
        # No early persist — locks the "no persona_create before Stage 6
        # confirmation" rule into the visible contract.
        assert "persona_create" in prelude
        assert "Stage 6" in prelude

    def test_prelude_names_web_fetch_for_stage_4(self, prelude: str) -> None:
        # Stage 4 pulls URL summaries via web_fetch; the prelude must
        # signal that the builtin is in-play so agents don't invent
        # a fallback.
        assert "web_fetch" in prelude

    # ------------------------------------------------------------------
    # W2: Stage 0 character-source branching + nuwa distillation
    # ------------------------------------------------------------------

    def test_prelude_names_both_stage_0_branches(self, prelude: str) -> None:
        # Stage 0 branches the wizard: public-character (auto research +
        # distill via huashu-nuwa) vs self-created (existing manual flow).
        # Both branch labels must be in the prelude so the agent's first
        # ask_user can offer them as the two fixed options.
        assert "公众人物" in prelude, "missing 公众人物 branch label"
        assert "自创角色" in prelude, "missing 自创角色 branch label"

    def test_prelude_lists_research_tools_for_stage_0b(
        self, prelude: str
    ) -> None:
        # Stage 0b researches via web_search + web_fetch for the public
        # branch. The prelude must surface web_search so the agent knows
        # the builtin is in-play and doesn't fall back to training-corpus
        # hallucination (the explicit risk this wave was built to close).
        assert "web_search" in prelude

    def test_prelude_references_huashu_nuwa_skill(self, prelude: str) -> None:
        # The distillation framework (identity / mental_models /
        # expression_dna / anti_patterns / honest_boundaries) lives in
        # the bundled huashu-nuwa skill (W1). The prelude must name it
        # so the agent knows where to read the full extraction rubric.
        assert "huashu-nuwa" in prelude

    def test_prelude_forbids_training_corpus_hallucination(
        self, prelude: str
    ) -> None:
        # The biggest risk in the public branch: agent skips web_search
        # and fills buckets from training-corpus memory. Lock the red-
        # line wording into the prelude.
        assert "禁止" in prelude
        assert "训练语料" in prelude


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
