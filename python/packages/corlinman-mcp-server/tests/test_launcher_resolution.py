"""Resolving stdio launcher commands against a widened PATH.

Found in production: the gateway seeded its bundled search server correctly
and then could not start it — ``No such file or directory: 'uvx'``. A systemd
unit gets ``/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin``
and nothing else, while ``uv`` installs to ``~/.local/bin``. The same command
worked fine in the operator's shell, which is what makes this class of bug
expensive to diagnose.

This affects every stdio MCP server, not just the bundled one — the
ecosystem's launchers (``uvx`` / ``npx`` / ``bunx`` / ``pipx``) all live in
per-user directories.
"""

from __future__ import annotations

import os
from pathlib import Path

from corlinman_mcp_server.client_manager import resolve_launcher


def _make_exe(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / name
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    return exe


def test_absolute_paths_are_returned_untouched() -> None:
    """An operator who wrote a path meant it — never second-guess it."""
    assert resolve_launcher("/opt/custom/uvx") == ("/opt/custom/uvx", None)


def test_empty_command_is_passed_through() -> None:
    assert resolve_launcher("") == ("", None)


def test_a_command_on_path_wins(tmp_path: Path, monkeypatch) -> None:
    """PATH is searched first, so an operator's own copy always wins over
    the well-known-directory fallback."""
    on_path = tmp_path / "bin"
    _make_exe(on_path, "faketool")
    monkeypatch.setenv("PATH", str(on_path))

    resolved, extra = resolve_launcher("faketool")
    assert resolved == str(on_path / "faketool")
    # Came from PATH, so the child needs no PATH surgery.
    assert extra is None


def test_a_launcher_off_path_is_found_and_its_dir_returned(
    tmp_path: Path, monkeypatch
) -> None:
    """The production case: uvx installed under the account's ~/.local/bin
    while the service PATH omits it."""
    home = tmp_path / "home"
    local_bin = home / ".local" / "bin"
    _make_exe(local_bin, "faketool")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    resolved, extra = resolve_launcher("faketool")
    assert resolved == str(local_bin / "faketool")
    # Handed back so the child can find its siblings (uvx shells out to uv).
    assert extra == str(local_bin)


def test_the_account_home_is_searched_even_when_HOME_is_overridden(
    tmp_path: Path, monkeypatch
) -> None:
    """corlinman's own gateway unit sets ``HOME=/opt/corlinman/data``, which
    makes ``expanduser`` miss the account's real ~/.local/bin. The password
    database is consulted precisely because no env override can move it."""
    account_home = tmp_path / "account"
    local_bin = account_home / ".local" / "bin"
    _make_exe(local_bin, "faketool")

    # HOME points somewhere with no launcher in it, as on the VPS.
    monkeypatch.setenv("HOME", str(tmp_path / "service-data"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    import pwd

    real_getpwuid = pwd.getpwuid

    class _Entry:
        pw_dir = str(account_home)

    monkeypatch.setattr(
        pwd, "getpwuid", lambda uid: _Entry() if uid == os.getuid() else real_getpwuid(uid)
    )

    resolved, extra = resolve_launcher("faketool")
    assert resolved == str(local_bin / "faketool")
    assert extra == str(local_bin)


def test_an_unresolvable_command_is_left_alone(tmp_path: Path, monkeypatch) -> None:
    """Rewriting it would only obscure the OS error, which names the command
    and is the thing an operator can act on."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert resolve_launcher("definitely-not-installed-xyz") == (
        "definitely-not-installed-xyz",
        None,
    )
