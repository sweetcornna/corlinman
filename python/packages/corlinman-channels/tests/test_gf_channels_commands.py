"""Gap-fill (lane-channels) — commands.py extensions.

Covers the commands-dir ``*.md`` loader + frontmatter, ``$ARGUMENTS`` /
``$N`` substitution, the SlashAccessPolicy ACL matrix, the unknown-command
notice helper, and the skills->command bridge.
"""

from __future__ import annotations

import pytest

from corlinman_channels.commands import (
    CommandSpec,
    SlashAccessPolicy,
    SlashAccessTier,
    _parse_frontmatter,
    _spec_from_md,
    load_commands_dir,
    register_skill_command,
    runtime_registry,
    substitute_arguments,
    unknown_command_notice,
)
from corlinman_channels.common import ChannelBinding


@pytest.fixture(autouse=True)
def _clear_runtime_registry():
    """Keep the global runtime registry clean across tests in this file."""
    before = list(runtime_registry)
    yield
    runtime_registry[:] = before


# ---------------------------------------------------------------------------
# $ARGUMENTS / $N substitution
# ---------------------------------------------------------------------------


def test_substitute_full_arguments() -> None:
    assert substitute_arguments("run $ARGUMENTS now", "a b c") == "run a b c now"


def test_substitute_positional() -> None:
    assert substitute_arguments("$1 then $2", "first second third") == (
        "first then second"
    )


def test_substitute_out_of_range_is_empty() -> None:
    assert substitute_arguments("x=$3", "only one") == "x="


def test_substitute_brace_form() -> None:
    assert substitute_arguments("${1}suffix", "tok rest") == "toksuffix"
    assert substitute_arguments("${ARGUMENTS}!", "a b") == "a b!"


def test_substitute_leaves_unknown_tokens_intact() -> None:
    # A bare ``$`` and a ``$word`` (non-numeric, non-ARGUMENTS) are not
    # rewritten so shell snippets / prices survive.
    assert substitute_arguments("cost $5.00 of $stuff bare$", "x") == (
        "cost .00 of $stuff bare$"
    )
    # NOTE: $5 IS a positional token; the ``.00`` shows it substituted to "".


# ---------------------------------------------------------------------------
# Frontmatter + commands-dir loader
# ---------------------------------------------------------------------------


def test_parse_frontmatter_basic() -> None:
    meta, body = _parse_frontmatter(
        "---\ndescription: A cmd\naliases: foo, 你好\ncategory: Custom\n---\nbody $ARGUMENTS"
    )
    assert meta["description"] == "A cmd"
    assert meta["aliases"] == "foo, 你好"
    assert meta["category"] == "Custom"
    assert body == "body $ARGUMENTS"


def test_parse_frontmatter_no_fence_returns_raw() -> None:
    meta, body = _parse_frontmatter("plain body no fence")
    assert meta == {}
    assert body == "plain body no fence"


def test_parse_frontmatter_strips_quotes() -> None:
    meta, _ = _parse_frontmatter('---\ndescription: "Quoted value"\n---\nx')
    assert meta["description"] == "Quoted value"


def test_spec_from_md_builds_aliases_and_prelude() -> None:
    spec = _spec_from_md(
        "greet",
        "---\ndescription: Greet\naliases: hi, 你好\nargument-hint: <name>\n---\nSay hi to $1",
    )
    assert spec.name == "greet"
    assert spec.aliases[0] == "/greet"
    assert "/hi" in spec.aliases
    assert "你好" in spec.aliases  # bare CJK alias passes through verbatim
    assert spec.summary == "Greet"
    assert spec.args_hint == "<name>"
    assert spec.wizard_prelude == "Say hi to $1"


def test_spec_from_md_admin_only_flag() -> None:
    spec = _spec_from_md("danger", "---\ndescription: D\nadmin_only: true\n---\nbody")
    assert spec.admin_only is True


def test_load_commands_dir(tmp_path) -> None:
    (tmp_path / "foo.md").write_text(
        "---\ndescription: Foo\n---\nDo foo $ARGUMENTS", encoding="utf-8"
    )
    (tmp_path / "bar.md").write_text("Bare body", encoding="utf-8")
    (tmp_path / "ignore.txt").write_text("not markdown", encoding="utf-8")
    specs = load_commands_dir(tmp_path)
    names = sorted(s.name for s in specs)
    assert names == ["bar", "foo"]


def test_load_commands_dir_missing_returns_empty(tmp_path) -> None:
    assert load_commands_dir(tmp_path / "nope") == []


# ---------------------------------------------------------------------------
# SlashAccessPolicy ACL matrix
# ---------------------------------------------------------------------------


def _binding() -> ChannelBinding:
    return ChannelBinding("qq", "bot", "group", "user")


def _spec(name: str, *, admin_only: bool = False) -> CommandSpec:
    return CommandSpec(
        name=name, aliases=(f"/{name}",), summary="", wizard_prelude="x", admin_only=admin_only
    )


def test_acl_public_allow_by_default() -> None:
    pol = SlashAccessPolicy()
    spec = _spec("open")
    assert pol.allows(spec, _binding(), is_dm=False) is True


def test_acl_dm_only() -> None:
    pol = SlashAccessPolicy(tiers={"x": SlashAccessTier.DM_ONLY})
    spec = _spec("x")
    assert pol.allows(spec, _binding(), is_dm=True) is True
    assert pol.allows(spec, _binding(), is_dm=False) is False


def test_acl_allowlist_admin_gate() -> None:
    pol = SlashAccessPolicy(tiers={"x": SlashAccessTier.ALLOWLIST})
    spec = _spec("x")
    assert pol.allows(spec, _binding(), is_dm=False, is_admin=True) is True
    assert pol.allows(spec, _binding(), is_dm=False, is_admin=False) is False


def test_acl_admin_only_flag_implies_allowlist() -> None:
    pol = SlashAccessPolicy()
    spec = _spec("x", admin_only=True)
    assert pol.tier_for(spec) == SlashAccessTier.ALLOWLIST
    assert pol.allows(spec, _binding(), is_dm=False, is_admin=False) is False


def test_acl_default_tier_flips_polarity() -> None:
    pol = SlashAccessPolicy(default_tier=SlashAccessTier.ALLOWLIST)
    spec = _spec("x")
    # No explicit tier — falls to the stricter default.
    assert pol.allows(spec, _binding(), is_dm=False, is_admin=False) is False
    assert pol.allows(spec, _binding(), is_dm=False, is_admin=True) is True


# ---------------------------------------------------------------------------
# Unknown-command notice
# ---------------------------------------------------------------------------


def test_unknown_notice_plain_prose_returns_none() -> None:
    assert unknown_command_notice("just chatting") is None


def test_unknown_notice_registered_command_returns_none() -> None:
    assert unknown_command_notice("/help") is None


def test_unknown_notice_not_command_shaped_returns_none() -> None:
    # A path-like leading slash isn't a command shape.
    assert unknown_command_notice("/foo/bar") is None
    assert unknown_command_notice("/") is None


def test_unknown_notice_suggests_close_match() -> None:
    out = unknown_command_notice("/halp")
    assert out is not None
    assert "/halp" in out
    assert "/help" in out  # shares the /h prefix


def test_unknown_notice_unrelated_falls_back_to_help() -> None:
    out = unknown_command_notice("/zzzzzz")
    assert out is not None
    assert "/help" in out


# ---------------------------------------------------------------------------
# Skills -> command bridge
# ---------------------------------------------------------------------------


def test_register_skill_command_registers() -> None:
    spec = register_skill_command(name="gf_skill_demo", summary="demo skill")
    assert spec is not None
    assert spec.aliases[0] == "/gf_skill_demo"
    assert spec.wizard_prelude is not None
    assert "$ARGUMENTS" in spec.wizard_prelude


def test_register_skill_command_idempotent_on_collision() -> None:
    assert register_skill_command(name="gf_skill_dup", summary="one") is not None
    # Re-register the same name — returns None instead of raising.
    assert register_skill_command(name="gf_skill_dup", summary="two") is None


def test_register_skill_command_custom_aliases() -> None:
    spec = register_skill_command(
        name="gf_skill_alias", summary="s", aliases=("/alt_gf",)
    )
    assert spec is not None
    assert "/alt_gf" in spec.aliases
