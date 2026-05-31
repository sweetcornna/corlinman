"""CMP-07 — commands-dir loader + $ARGUMENTS substitution must be wired.

``register_commands_from_dir`` / ``register_skill_command`` /
``substitute_arguments`` are implemented + exported but have no production
caller, and ``apply_command_prelude`` returns the wizard prelude verbatim
(args never substituted).

Acceptance:
* A ``commands/foo.md`` with a ``$ARGUMENTS`` placeholder becomes an
  invokable ``/foo`` whose delivered prelude has the args substituted.
* The bootstrap helper registers the dir + a skill command and is safe
  against path traversal (it only loads ``*.md`` directly under the dir).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from corlinman_channels import commands as cmds
from corlinman_channels.commands import (
    apply_command_prelude,
    match_command_with_args,
    register_commands_from_dir,
    substitute_arguments,
)
from corlinman_channels.onebot import (
    MessageEvent,
    MessageType,
    Sender,
    TextSegment,
)
from corlinman_channels.router import ChannelRouter


@pytest.fixture(autouse=True)
def _clean_registry():
    """Snapshot + restore the mutable runtime registry around each test."""
    saved = list(cmds.runtime_registry)
    cmds.runtime_registry.clear()
    yield
    cmds.runtime_registry.clear()
    cmds.runtime_registry.extend(saved)


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


def test_prelude_substitutes_arguments() -> None:
    # apply_command_prelude must substitute $ARGUMENTS when args are given.
    from corlinman_channels.commands import register_skill_command

    spec = register_skill_command(
        name="echoargs",
        summary="echo",
        prelude="Run with: $ARGUMENTS and first=$1",
    )
    assert spec is not None
    out = apply_command_prelude("/echoargs hello world", spec, args_text="hello world")
    assert "hello world" in out
    assert "first=hello" in out
    # The placeholder itself must be gone.
    assert "$ARGUMENTS" not in out
    assert "$1" not in out


def test_register_commands_from_dir_makes_invokable(tmp_path: Path) -> None:
    cmd_dir = tmp_path / "commands"
    cmd_dir.mkdir()
    (cmd_dir / "foo.md").write_text(
        "---\ndescription: foo command\n---\nDo the foo with $ARGUMENTS",
        encoding="utf-8",
    )
    registered = register_commands_from_dir(cmd_dir)
    assert any(s.name == "foo" for s in registered)

    match = match_command_with_args("/foo bar baz")
    assert match is not None
    spec, args = match
    assert spec.name == "foo"
    assert args == "bar baz"
    delivered = apply_command_prelude("/foo bar baz", spec, args_text=args)
    assert "Do the foo with bar baz" == delivered


def test_router_prelude_substitutes_args(tmp_path: Path) -> None:
    cmd_dir = tmp_path / "commands"
    cmd_dir.mkdir()
    (cmd_dir / "deploy.md").write_text(
        "Deploy target=$1 all=$ARGUMENTS", encoding="utf-8"
    )
    register_commands_from_dir(cmd_dir)

    router = ChannelRouter(group_keywords={}, self_ids=[100])
    ev = _group_event("/deploy prod now")
    req = router.dispatch(ev)
    assert req is not None
    # The router-side prelude rewrite must substitute the args.
    assert req.content == "Deploy target=prod all=prod now"


def test_substitute_arguments_leaves_unknown_dollar() -> None:
    assert substitute_arguments("price is $foo", "x") == "price is $foo"
