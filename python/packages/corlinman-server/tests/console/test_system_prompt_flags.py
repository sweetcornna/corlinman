"""``--system-prompt`` / ``--append-system-prompt`` (ABSORB_MATRIX Dim 10).

claude-code semantics: ``--system-prompt`` REPLACES the default coding
prompt + project memory wholesale; ``--append-system-prompt`` adds after
whatever prompt is in effect (default composition or an override). With
neither flag the composition must stay byte-identical to the pre-flag
behavior so the servicer's default-prompt path keeps working.
"""

from __future__ import annotations

from corlinman_server.console.app import compose_system_prompt


def test_no_flags_no_memory_returns_none() -> None:
    """Nothing to send → None so the servicer default applies."""
    assert (
        compose_system_prompt(base_prompt="BASE", memory_text=None, override=None, append=None)
        is None
    )


def test_no_flags_memory_prefixes_base_prompt() -> None:
    """Pre-flag behavior pinned: memory rides after the coding prompt."""
    assert (
        compose_system_prompt(base_prompt="BASE", memory_text="MEM", override=None, append=None)
        == "BASE\n\nMEM"
    )


def test_no_flags_memory_without_base_prompt() -> None:
    """Attach-only installs (no importable coding prompt) send memory alone."""
    assert (
        compose_system_prompt(base_prompt="", memory_text="MEM", override=None, append=None)
        == "MEM"
    )


def test_override_replaces_base_and_memory() -> None:
    assert (
        compose_system_prompt(base_prompt="BASE", memory_text="MEM", override="CUSTOM", append=None)
        == "CUSTOM"
    )


def test_append_rides_after_base_and_memory() -> None:
    assert (
        compose_system_prompt(base_prompt="BASE", memory_text="MEM", override=None, append="EXTRA")
        == "BASE\n\nMEM\n\nEXTRA"
    )


def test_append_without_memory_still_keeps_base_prompt() -> None:
    """Append alone must not drop the default coding prompt (the servicer
    preserves a caller-supplied system message verbatim, so sending only
    the extra text would silently lose tool/coding behavior)."""
    assert (
        compose_system_prompt(base_prompt="BASE", memory_text=None, override=None, append="EXTRA")
        == "BASE\n\nEXTRA"
    )


def test_append_stacks_on_override() -> None:
    assert (
        compose_system_prompt(
            base_prompt="BASE", memory_text="MEM", override="CUSTOM", append="EXTRA"
        )
        == "CUSTOM\n\nEXTRA"
    )


def test_cli_exposes_both_flags() -> None:
    """The click command carries both options (plumbed to run_console)."""
    from corlinman_server.cli.console import console

    names = {p.name for p in console.params}
    assert "system_prompt" in names
    assert "append_system_prompt" in names
