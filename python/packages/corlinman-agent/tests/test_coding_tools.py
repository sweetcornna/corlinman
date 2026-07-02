"""Tests for the builtin coding tools (file ops, search, shell).

Every tool is workspace-confined; these tests pass an explicit
``workspace=tmp_path`` so they never touch the real
``CORLINMAN_AGENT_WORKSPACE``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from corlinman_agent.coding import (
    CODING_TOOLS,
    coding_tool_schemas,
    dispatch_edit_file,
    dispatch_list_files,
    dispatch_read_file,
    dispatch_run_shell,
    dispatch_search_files,
    dispatch_write_file,
)


def _args(**kw: object) -> bytes:
    return json.dumps(kw).encode("utf-8")


# ---------------------------------------------------------------------------
# schemas / registry
# ---------------------------------------------------------------------------


def test_coding_tools_set_and_schemas_align() -> None:
    schemas = coding_tool_schemas()
    assert len(schemas) == len(CODING_TOOLS) == 9
    names = {s["function"]["name"] for s in schemas}
    assert names == set(CODING_TOOLS)


# ---------------------------------------------------------------------------
# write / read / edit / list round-trip
# ---------------------------------------------------------------------------


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    w = json.loads(
        dispatch_write_file(
            args_json=_args(path="src/hello.py", content="print('hi')\n"),
            workspace=tmp_path,
        )
    )
    assert w["action"] == "created"
    assert (tmp_path / "src" / "hello.py").read_text() == "print('hi')\n"

    r = json.loads(
        dispatch_read_file(args_json=_args(path="src/hello.py"), workspace=tmp_path)
    )
    assert "print('hi')" in r["content"]
    assert r["content"].startswith("1\t")  # 1-based line numbers
    assert r["lines"] == 1


def test_read_missing_file(tmp_path: Path) -> None:
    r = json.loads(
        dispatch_read_file(args_json=_args(path="nope.txt"), workspace=tmp_path)
    )
    assert r["error"] == "file_not_found"


def test_edit_file_replaces_unique_string(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("alpha beta gamma\n")
    res = json.loads(
        dispatch_edit_file(
            args_json=_args(path="f.txt", old_string="beta", new_string="DELTA"),
            workspace=tmp_path,
        )
    )
    assert res["replacements"] == 1
    assert (tmp_path / "f.txt").read_text() == "alpha DELTA gamma\n"


def test_edit_file_rejects_ambiguous_match(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("x x x\n")
    res = json.loads(
        dispatch_edit_file(
            args_json=_args(path="f.txt", old_string="x", new_string="y"),
            workspace=tmp_path,
        )
    )
    assert "old_string_not_unique" in res["error"]
    # replace_all bypasses the uniqueness guard.
    res2 = json.loads(
        dispatch_edit_file(
            args_json=_args(
                path="f.txt", old_string="x", new_string="y", replace_all=True
            ),
            workspace=tmp_path,
        )
    )
    assert res2["replacements"] == 3
    assert (tmp_path / "f.txt").read_text() == "y y y\n"


def test_list_files(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub").mkdir()
    res = json.loads(dispatch_list_files(args_json=_args(), workspace=tmp_path))
    kinds = {e["name"]: e["type"] for e in res["entries"]}
    assert kinds == {"a.txt": "file", "sub": "dir"}


# ---------------------------------------------------------------------------
# workspace confinement
# ---------------------------------------------------------------------------


def test_path_escape_is_rejected(tmp_path: Path) -> None:
    for dispatch in (dispatch_read_file, dispatch_write_file):
        res = json.loads(
            dispatch(
                args_json=_args(path="../../etc/passwd", content="x"),
                workspace=tmp_path,
            )
        )
        assert "workspace_escape" in res["error"]


def test_absolute_path_outside_workspace_rejected(tmp_path: Path) -> None:
    res = json.loads(
        dispatch_read_file(args_json=_args(path="/etc/hosts"), workspace=tmp_path)
    )
    assert "workspace_escape" in res["error"]


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_content_mode(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def foo():\n    return 1\n")
    (tmp_path / "b.py").write_text("def bar():\n    return 2\n")
    res = json.loads(
        dispatch_search_files(
            args_json=_args(pattern=r"def \w+", mode="content"),
            workspace=tmp_path,
        )
    )
    matched = {(m["path"], m["line"]) for m in res["matches"]}
    assert ("a.py", 1) in matched
    assert ("b.py", 1) in matched


def test_search_name_mode(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("")
    (tmp_path / "y.txt").write_text("")
    res = json.loads(
        dispatch_search_files(
            args_json=_args(pattern="*.py", mode="name"), workspace=tmp_path
        )
    )
    assert res["matches"] == ["x.py"]


# ---------------------------------------------------------------------------
# run_shell
# ---------------------------------------------------------------------------


async def test_run_shell_success(tmp_path: Path) -> None:
    res = json.loads(
        await dispatch_run_shell(
            args_json=_args(command="echo hello-shell"), workspace=tmp_path
        )
    )
    assert res["exit_code"] == 0
    assert "hello-shell" in res["output"]


async def test_run_shell_runs_in_workspace(tmp_path: Path) -> None:
    (tmp_path / "marker.txt").write_text("")
    res = json.loads(
        await dispatch_run_shell(args_json=_args(command="ls"), workspace=tmp_path)
    )
    assert "marker.txt" in res["output"]


async def test_run_shell_nonzero_exit(tmp_path: Path) -> None:
    res = json.loads(
        await dispatch_run_shell(
            args_json=_args(command="exit 3"), workspace=tmp_path
        )
    )
    assert res["exit_code"] == 3


async def test_run_shell_timeout(tmp_path: Path) -> None:
    res = json.loads(
        await dispatch_run_shell(
            args_json=_args(command="sleep 5", timeout=1), workspace=tmp_path
        )
    )
    assert "timeout" in res["error"]


async def test_run_shell_refuses_destructive_command(tmp_path: Path) -> None:
    res = json.loads(
        await dispatch_run_shell(
            args_json=_args(command="rm -rf /"), workspace=tmp_path
        )
    )
    assert "command_refused" in res["error"]


async def test_run_shell_refuses_smuggled_compound_command(tmp_path: Path) -> None:
    """A denied pattern hidden after ';' is still caught."""
    res = json.loads(
        await dispatch_run_shell(
            args_json=_args(command="ls ; sudo rm file"), workspace=tmp_path
        )
    )
    assert "command_refused" in res["error"]


# ---------------------------------------------------------------------------
# S2 — run_shell rlimits + env scrubbing + process-group kill
# ---------------------------------------------------------------------------


import sys as _sys

import pytest as _pytest


@_pytest.mark.skipif(
    _sys.platform == "win32",
    reason="POSIX rlimits / setsid only apply on POSIX",
)
async def test_run_shell_env_does_not_leak_provider_keys(
    tmp_path: Path, monkeypatch: _pytest.MonkeyPatch
) -> None:
    """The spawned shell MUST NOT see the gateway's provider API keys.

    Regression for S2: previously the subprocess inherited the parent
    process env wholesale, so the model could ``echo $OPENAI_API_KEY``
    and exfiltrate it via stdout. The whitelist
    (PATH/LANG/LC_ALL/HOME/USER) is the only env it sees.
    """
    # Plant a fake secret in the parent process env; the child must NOT
    # observe it.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-test-token-xyz")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-also-secret")

    res = json.loads(
        await dispatch_run_shell(
            args_json=_args(
                command=(
                    'echo "OPENAI=${OPENAI_API_KEY:-UNSET}";'
                    'echo "ANTHROPIC=${ANTHROPIC_API_KEY:-UNSET}";'
                    # Sanity: PATH (whitelisted) must survive.
                    'echo "PATH_PRESENT=${PATH:+yes}"'
                ),
            ),
            workspace=tmp_path,
        )
    )
    out = res["output"]
    # Secrets stripped — the child sees UNSET, never the real value.
    assert "OPENAI=UNSET" in out, out
    assert "ANTHROPIC=UNSET" in out, out
    # And the real secret never appears anywhere in the output.
    assert "sk-secret-test-token-xyz" not in out
    assert "sk-ant-also-secret" not in out
    # PATH survives the whitelist.
    assert "PATH_PRESENT=yes" in out


@_pytest.mark.skipif(
    _sys.platform == "win32",
    reason="POSIX rlimits only apply on POSIX",
)
async def test_run_shell_rlimit_fsize_caps_output_file(tmp_path: Path) -> None:
    """``RLIMIT_FSIZE`` truncates a giant write at 100 MiB instead of
    letting it fill the disk."""
    # Try to write 200 MiB; the rlimit (100 MiB) will trip and the
    # shell terminates the writer. We verify the resulting file is
    # bounded under the cap.
    target = tmp_path / "big.bin"
    # Use ``head`` + ``/dev/zero`` rather than ``dd if=`` (denylist).
    cmd = f"head -c 209715200 /dev/zero > {target.name}"
    res = json.loads(
        await dispatch_run_shell(
            args_json=_args(command=cmd, timeout=20), workspace=tmp_path
        )
    )
    # The command exits non-zero (signal or write error), and any file
    # that was created is bounded under the RLIMIT_FSIZE cap.
    assert res["exit_code"] != 0
    if target.exists():
        assert target.stat().st_size <= 100 * 1024 * 1024 + 4096


@_pytest.mark.skipif(
    _sys.platform == "win32",
    reason="POSIX setsid / killpg only apply on POSIX",
)
async def test_run_shell_timeout_kills_forked_children(tmp_path: Path) -> None:
    """A timeout must kill the whole process tree, not just the shell.

    Regression for S2: previously ``proc.kill()`` only killed the
    immediate child (the ``/bin/sh -c`` wrapper), and a long-running
    ``python -c 'time.sleep(...)'`` it spawned survived. With
    ``setsid`` + ``killpg(SIGKILL)`` the whole group dies.
    """
    import time as _time

    # The shell wrapper forks a python subprocess that sleeps for a
    # very long time. The OUTER timeout (1s) must reap both.
    cmd = "python3 -c 'import time; time.sleep(30)'"
    t0 = _time.monotonic()
    res = json.loads(
        await dispatch_run_shell(
            args_json=_args(command=cmd, timeout=1), workspace=tmp_path
        )
    )
    elapsed = _time.monotonic() - t0
    assert "timeout" in res["error"]
    # The whole call must return promptly — the wait_for on
    # ``proc.wait()`` after killpg gives us a tight envelope.
    assert elapsed < 5.0, f"timeout reap took {elapsed:.2f}s — process tree leak?"


async def test_run_shell_timeout_cap_lowered_to_60s(tmp_path: Path) -> None:
    """The hard ``_MAX_TIMEOUT`` is 60s (down from 120s)."""
    from corlinman_agent.coding.shell import _MAX_TIMEOUT

    assert _MAX_TIMEOUT == 60


# ---------------------------------------------------------------------------
# S3 — workspace escape through symlinked parent directory
# ---------------------------------------------------------------------------


@_pytest.mark.skipif(
    _sys.platform == "win32",
    reason="POSIX symlink semantics",
)
def test_write_through_symlinked_parent_is_refused(tmp_path: Path) -> None:
    """Plant ``workspace/escape_dir`` pointing at an outside directory.
    A write to ``escape_dir/secret`` MUST be refused and the outside
    target file MUST NOT be created.

    Regression for S3: ``Path.resolve()`` followed the symlink, so the
    write landed inside ``/tmp/attacker`` even though the call was
    "inside" the workspace.
    """
    # Set up the attacker target directory OUTSIDE the workspace.
    attacker = tmp_path.parent / f"attacker_{tmp_path.name}"
    attacker.mkdir()
    # Plant the symlink inside the workspace.
    escape = tmp_path / "escape_dir"
    escape.symlink_to(attacker, target_is_directory=True)

    res = json.loads(
        dispatch_write_file(
            args_json=_args(path="escape_dir/secret", content="LEAKED"),
            workspace=tmp_path,
        )
    )
    assert "workspace_escape" in res["error"], res
    # The attacker directory MUST remain empty.
    assert list(attacker.iterdir()) == [], (
        f"write escaped the workspace — attacker dir contains: "
        f"{list(attacker.iterdir())}"
    )


@_pytest.mark.skipif(
    _sys.platform == "win32",
    reason="POSIX symlink semantics",
)
def test_write_to_leaf_symlink_is_refused(tmp_path: Path) -> None:
    """A leaf that is itself a symlink (even pointing back inside the
    workspace) is refused for writes — easy to misuse."""
    target = tmp_path / "real.txt"
    target.write_text("real")
    link = tmp_path / "alias.txt"
    link.symlink_to(target)

    res = json.loads(
        dispatch_write_file(
            args_json=_args(path="alias.txt", content="rewritten"),
            workspace=tmp_path,
        )
    )
    assert "workspace_escape" in res["error"], res
    # The real target file is untouched.
    assert target.read_text() == "real"


@_pytest.mark.skipif(
    _sys.platform == "win32",
    reason="POSIX symlink semantics",
)
def test_apply_patch_through_symlinked_parent_is_refused(tmp_path: Path) -> None:
    """``apply_patch`` adding a file under a symlinked-out directory
    must be refused at the staging step before any disk write."""
    from corlinman_agent.coding import dispatch_apply_patch

    attacker = tmp_path.parent / f"attacker_{tmp_path.name}_patch"
    attacker.mkdir()
    escape = tmp_path / "escape_dir"
    escape.symlink_to(attacker, target_is_directory=True)

    patch = (
        "*** Begin Patch\n"
        "*** Add File: escape_dir/new.txt\n"
        "+leaked\n"
        "*** End Patch\n"
    )
    res = json.loads(
        dispatch_apply_patch(
            args_json=_args(patch=patch), workspace=tmp_path
        )
    )
    assert "workspace_escape" in res["error"], res
    assert list(attacker.iterdir()) == []


@_pytest.mark.skipif(
    _sys.platform == "win32",
    reason="POSIX symlink semantics",
)
def test_edit_file_through_symlinked_parent_is_refused(tmp_path: Path) -> None:
    """``edit_file`` resolves with ``for_write=True``, so a write
    through a symlinked-out parent must be refused even though the
    file appears to be inside the workspace."""
    attacker = tmp_path.parent / f"attacker_{tmp_path.name}_edit"
    attacker.mkdir()
    # Plant a real file in the attacker dir.
    target_in_attacker = attacker / "victim.txt"
    target_in_attacker.write_text("original content")
    # Symlink inside the workspace points at attacker dir.
    escape = tmp_path / "escape_dir"
    escape.symlink_to(attacker, target_is_directory=True)

    res = json.loads(
        dispatch_edit_file(
            args_json=_args(
                path="escape_dir/victim.txt",
                old_string="original",
                new_string="MODIFIED",
            ),
            workspace=tmp_path,
        )
    )
    assert "workspace_escape" in res["error"], res
    # Original content untouched.
    assert target_in_attacker.read_text() == "original content"


# ---------------------------------------------------------------------------
# todo_write
# ---------------------------------------------------------------------------


def test_todo_write_stores_and_renders() -> None:
    from corlinman_agent.coding import (
        TodoStore,
        dispatch_todo_write,
        render_todo_block,
    )

    store = TodoStore()
    todos = [
        {"content": "Step one", "activeForm": "Doing step one", "status": "completed"},
        {"content": "Step two", "activeForm": "Doing step two", "status": "in_progress"},
        {"content": "Step three", "activeForm": "Doing step three", "status": "pending"},
    ]
    res = json.loads(
        dispatch_todo_write(
            args_json=_args(todos=todos), store=store, session_key="s1"
        )
    )
    assert res["counts"] == {"pending": 1, "in_progress": 1, "completed": 1}
    block = render_todo_block(store, "s1")
    assert "[x] Step one" in block
    assert "[~] Step two" in block
    assert "[ ] Step three" in block


def test_todo_write_warns_on_multiple_in_progress() -> None:
    from corlinman_agent.coding import TodoStore, dispatch_todo_write

    store = TodoStore()
    todos = [
        {"content": "A", "activeForm": "Aing", "status": "in_progress"},
        {"content": "B", "activeForm": "Bing", "status": "in_progress"},
    ]
    res = json.loads(
        dispatch_todo_write(
            args_json=_args(todos=todos), store=store, session_key="s2"
        )
    )
    assert "warning" in res


def test_todo_write_rejects_bad_status() -> None:
    from corlinman_agent.coding import TodoStore, dispatch_todo_write

    store = TodoStore()
    res = json.loads(
        dispatch_todo_write(
            args_json=_args(
                todos=[{"content": "X", "activeForm": "Xing", "status": "weird"}]
            ),
            store=store,
            session_key="s3",
        )
    )
    assert "args_invalid" in res["error"]


# ---------------------------------------------------------------------------
# apply_patch
# ---------------------------------------------------------------------------


def test_apply_patch_add_file(tmp_path: Path) -> None:
    from corlinman_agent.coding import dispatch_apply_patch

    patch = (
        "*** Begin Patch\n"
        "*** Add File: pkg/new.py\n"
        "+print('a')\n"
        "+print('b')\n"
        "*** End Patch\n"
    )
    res = json.loads(
        dispatch_apply_patch(args_json=_args(patch=patch), workspace=tmp_path)
    )
    assert res["applied"] is True
    assert (tmp_path / "pkg" / "new.py").read_text() == "print('a')\nprint('b')"


def test_apply_patch_update_file(tmp_path: Path) -> None:
    from corlinman_agent.coding import dispatch_apply_patch

    (tmp_path / "f.py").write_text("line one\nline two\nline three\n")
    patch = (
        "*** Begin Patch\n"
        "*** Update File: f.py\n"
        "@@\n"
        " line one\n"
        "-line two\n"
        "+line TWO changed\n"
        " line three\n"
        "*** End Patch\n"
    )
    res = json.loads(
        dispatch_apply_patch(args_json=_args(patch=patch), workspace=tmp_path)
    )
    assert res["applied"] is True
    assert (tmp_path / "f.py").read_text() == (
        "line one\nline TWO changed\nline three\n"
    )


def test_apply_patch_delete_file(tmp_path: Path) -> None:
    from corlinman_agent.coding import dispatch_apply_patch

    (tmp_path / "gone.txt").write_text("bye")
    patch = "*** Begin Patch\n*** Delete File: gone.txt\n*** End Patch\n"
    res = json.loads(
        dispatch_apply_patch(args_json=_args(patch=patch), workspace=tmp_path)
    )
    assert res["applied"] is True
    assert not (tmp_path / "gone.txt").exists()


def test_apply_patch_rejects_escape(tmp_path: Path) -> None:
    from corlinman_agent.coding import dispatch_apply_patch

    patch = (
        "*** Begin Patch\n"
        "*** Add File: ../../evil.txt\n"
        "+pwned\n"
        "*** End Patch\n"
    )
    res = json.loads(
        dispatch_apply_patch(args_json=_args(patch=patch), workspace=tmp_path)
    )
    assert "workspace_escape" in res["error"]


def test_apply_patch_malformed_envelope(tmp_path: Path) -> None:
    from corlinman_agent.coding import dispatch_apply_patch

    res = json.loads(
        dispatch_apply_patch(
            args_json=_args(patch="not a patch"), workspace=tmp_path
        )
    )
    assert "patch_parse_error" in res["error"]


def test_apply_patch_hunk_not_found(tmp_path: Path) -> None:
    from corlinman_agent.coding import dispatch_apply_patch

    (tmp_path / "f.py").write_text("real content\n")
    patch = (
        "*** Begin Patch\n"
        "*** Update File: f.py\n"
        "@@\n"
        "-nonexistent line\n"
        "+replacement\n"
        "*** End Patch\n"
    )
    res = json.loads(
        dispatch_apply_patch(args_json=_args(patch=patch), workspace=tmp_path)
    )
    assert "patch_apply_error" in res["error"]
    # File untouched — staging failed before any write.
    assert (tmp_path / "f.py").read_text() == "real content\n"


# ---------------------------------------------------------------------------
# search — T1.5 polish: mtime sort, offset paging, VCS exclusion
# ---------------------------------------------------------------------------


def test_search_content_mtime_sorts_files(tmp_path: Path) -> None:
    """Files with newer mtime must appear before older ones in matches."""
    import os

    old = tmp_path / "old.py"
    new = tmp_path / "new.py"
    old.write_text("HIT in old\n")
    new.write_text("HIT in new\n")
    # Force ``old`` to have a clearly older mtime, ``new`` clearly newer,
    # regardless of fs granularity.
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))

    res = json.loads(
        dispatch_search_files(
            args_json=_args(pattern="HIT", mode="content"),
            workspace=tmp_path,
        )
    )
    paths = [m["path"] for m in res["matches"]]
    # Newer file's match must appear before the older file's match.
    assert paths.index("new.py") < paths.index("old.py")


def test_search_content_offset_pages(tmp_path: Path) -> None:
    """``offset`` skips earlier results; remaining count is consistent."""
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("MARK here\n")

    # First, get the full ordered list (offset=0) for comparison.
    full = json.loads(
        dispatch_search_files(
            args_json=_args(pattern="MARK", mode="content"),
            workspace=tmp_path,
        )
    )
    assert len(full["matches"]) == 3
    assert full["next_offset"] is None
    assert full["truncated"] is False

    paged = json.loads(
        dispatch_search_files(
            args_json=_args(pattern="MARK", mode="content", offset=1),
            workspace=tmp_path,
        )
    )
    # Exactly the tail of the full list (2 results, in the same order).
    assert len(paged["matches"]) == 2
    assert paged["matches"] == full["matches"][1:]
    # No more results past offset=1 + 2 returned.
    assert paged["next_offset"] is None
    assert paged["truncated"] is False


def test_search_excludes_git_dir_in_both_modes(tmp_path: Path) -> None:
    """``.git`` (and other VCS dirs) must never appear in either mode."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "foo.py").write_text("NEEDLE inside git\n")
    (tmp_path / "real.py").write_text("NEEDLE in real\n")

    # content mode: only the real file's match shows up.
    content_res = json.loads(
        dispatch_search_files(
            args_json=_args(pattern="NEEDLE", mode="content"),
            workspace=tmp_path,
        )
    )
    paths = {m["path"] for m in content_res["matches"]}
    assert "real.py" in paths
    assert not any(p.startswith(".git") or "/.git/" in p for p in paths)

    # name mode: same — globbing for *.py must not surface .git/foo.py.
    name_res = json.loads(
        dispatch_search_files(
            args_json=_args(pattern="**/*.py", mode="name"),
            workspace=tmp_path,
        )
    )
    assert "real.py" in name_res["matches"]
    assert not any(
        p.startswith(".git") or "/.git/" in p for p in name_res["matches"]
    )


# ---------------------------------------------------------------------------
# T1.1 — run_shell tail truncation + log spill
# ---------------------------------------------------------------------------


async def test_run_shell_tail_truncates_and_spills_to_log(tmp_path: Path) -> None:
    """Output above the cap is tail-biased and the full output lands on disk."""
    from corlinman_agent.coding.shell import _MAX_OUTPUT_CHARS

    # Emit 50_000 chars (5000 lines of 10 chars). Each line is unique so
    # we can confirm tail-bias (last lines present, first lines absent).
    cmd = (
        "python3 -c \"\n"
        "import sys\n"
        "for i in range(5000):\n"
        "    sys.stdout.write(f'{i:09d}\\n')\n"
        "\""
    )
    res = json.loads(
        await dispatch_run_shell(args_json=_args(command=cmd), workspace=tmp_path)
    )

    assert res["exit_code"] == 0
    assert res["truncated"] is True
    assert "log_path" in res
    log_rel = res["log_path"]
    assert log_rel.startswith(".corlinman/run_shell_")
    assert log_rel.endswith(".log")

    # Inline payload is tail-biased: the truncation notice references
    # the log path and the LAST line (4999) is present, the FIRST line
    # (0000000000) is gone.
    out = res["output"]
    assert "output truncated" in out
    assert log_rel in out
    # Inline output is bounded by the cap + the notice prefix.
    assert len(out) <= _MAX_OUTPUT_CHARS + 200
    assert "000004999" in out
    assert "000000000" not in out

    # The on-disk log holds the *complete* untruncated output.
    log_file = tmp_path / log_rel
    assert log_file.exists()
    full = log_file.read_text()
    assert "000000000" in full
    assert "000004999" in full
    # 5000 lines × 10 chars/line ("000000000\n") = 50_000 chars exactly.
    assert len(full) >= 50_000


# ---------------------------------------------------------------------------
# T2.4 — workspace snapshot + revert_changes
# ---------------------------------------------------------------------------


def test_snapshot_init_then_snapshot_then_revert_roundtrip(tmp_path: Path) -> None:
    """End-to-end: init → snapshot v1 → mutate → snapshot v2 → revert → v1."""
    from corlinman_agent.coding import (
        list_snapshots,
        revert_last,
        snapshot,
    )

    target = tmp_path / "a.txt"
    target.write_text("v1\n")
    sha1 = snapshot(tmp_path, "v1")
    assert sha1 is not None and len(sha1) >= 4

    target.write_text("v2\n")
    sha2 = snapshot(tmp_path, "v2")
    assert sha2 is not None and sha2 != sha1

    snaps = list_snapshots(tmp_path)
    # Newest first: [v2, v1, initial, …].
    assert len(snaps) >= 3
    assert snaps[0]["sha"] == sha2
    assert snaps[0]["label"].endswith("v2")
    assert snaps[1]["sha"] == sha1
    assert snaps[1]["label"].endswith("v1")

    result = revert_last(tmp_path)
    assert result.get("reverted_to") == sha1
    assert result.get("from") == sha2
    assert "v1" in result.get("label", "")
    assert target.read_text() == "v1\n"


def test_dispatch_revert_changes_list_mode(tmp_path: Path) -> None:
    """``mode='list'`` returns the recent snapshot log without reverting."""
    from corlinman_agent.coding import (
        dispatch_revert_changes,
        snapshot,
    )

    (tmp_path / "f.txt").write_text("hi\n")
    snapshot(tmp_path, "first")
    res = json.loads(
        dispatch_revert_changes(
            args_json=_args(mode="list"), workspace=tmp_path
        )
    )
    snaps = res["snapshots"]
    assert isinstance(snaps, list)
    # initial + first = 2 entries at minimum.
    assert len(snaps) >= 2
    # Each entry has sha+label keys.
    assert {"sha", "label"} <= snaps[0].keys()
    # Mode list never touches the working tree.
    assert (tmp_path / "f.txt").read_text() == "hi\n"


def test_dispatch_revert_changes_no_snapshots(tmp_path: Path) -> None:
    """Brand-new workspace (initial commit only) reports no_snapshots."""
    from corlinman_agent.coding import (
        dispatch_revert_changes,
        ensure_repo,
    )

    assert ensure_repo(tmp_path) is True
    res = json.loads(
        dispatch_revert_changes(
            args_json=_args(mode="last"), workspace=tmp_path
        )
    )
    assert res.get("error") == "no_snapshots"


def test_dispatch_revert_changes_default_mode_is_last(tmp_path: Path) -> None:
    """Omitting ``mode`` defaults to ``last`` (the spec'd default)."""
    from corlinman_agent.coding import (
        dispatch_revert_changes,
        snapshot,
    )

    (tmp_path / "f.txt").write_text("a\n")
    snapshot(tmp_path, "edit-a")
    (tmp_path / "f.txt").write_text("b\n")
    snapshot(tmp_path, "edit-b")

    res = json.loads(
        dispatch_revert_changes(args_json=_args(), workspace=tmp_path)
    )
    assert "reverted_to" in res
    assert (tmp_path / "f.txt").read_text() == "a\n"


def test_dispatch_revert_changes_rejects_bad_mode(tmp_path: Path) -> None:
    """Invalid mode strings return an ``args_invalid`` envelope."""
    from corlinman_agent.coding import dispatch_revert_changes

    res = json.loads(
        dispatch_revert_changes(
            args_json=_args(mode="bogus"), workspace=tmp_path
        )
    )
    assert "args_invalid" in res["error"]


def test_snapshot_handles_no_git_gracefully(
    tmp_path: Path, monkeypatch: object
) -> None:
    """When ``git`` is missing from PATH, snapshot() returns None silently."""
    import shutil

    from corlinman_agent.coding import list_snapshots, snapshot

    # Hide git from shutil.which without poisoning the rest of PATH.
    real_which = shutil.which

    def fake_which(name: str, *a: object, **kw: object) -> str | None:
        if name == "git":
            return None
        return real_which(name, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(shutil, "which", fake_which)  # type: ignore[attr-defined]

    assert snapshot(tmp_path, "anything") is None
    # And list_snapshots returns [] rather than raising.
    assert list_snapshots(tmp_path) == []


def test_snapshot_sanitises_long_multiline_label(tmp_path: Path) -> None:
    """A multi-line / overlong label becomes a single ≤80-char subject."""
    from corlinman_agent.coding import list_snapshots, snapshot

    long_label = "X" * 200 + "\nsecond line that must not leak\n"
    sha = snapshot(tmp_path, long_label)
    assert sha is not None

    snaps = list_snapshots(tmp_path)
    top = snaps[0]["label"]
    # Single line.
    assert "\n" not in top
    # Bounded length — "snapshot: " prefix + ≤80 char subject body.
    assert len(top) <= len("snapshot: ") + 80
    assert "second line" not in top


# ---------------------------------------------------------------------------
# Perf — snapshot resolves SHA without a third subprocess
# ---------------------------------------------------------------------------


def test_snapshot_resolves_sha_without_subprocess(
    tmp_path: Path, monkeypatch: object
) -> None:
    """After ``ensure_repo`` seeds the repo, a snapshot must run only
    TWO subprocess calls (``add`` + ``commit``) and resolve the SHA by
    reading ``.git/HEAD`` directly.

    Regression for the perf fix that dropped the ``git rev-parse
    --short HEAD`` step in favour of a Python-side ref walker.
    """
    import subprocess as _subprocess

    from corlinman_agent.coding import _snapshot as snap_mod
    from corlinman_agent.coding import snapshot

    # First, seed the repo so ensure_repo's three subprocess calls
    # don't pollute the count.
    snap_mod.ensure_repo(tmp_path)

    # Now count subprocess.run calls happening through _run_git
    # during ONE snapshot() invocation.
    calls: list[tuple[str, ...]] = []
    real_run = _subprocess.run

    def _counting_run(cmd: list[str], *a: object, **kw: object):  # type: ignore[no-untyped-def]
        # Only count git invocations originating from _snapshot —
        # ignore anything else the test harness might fork.
        if isinstance(cmd, list) and cmd and cmd[0] == "git":
            calls.append(tuple(cmd))
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(_subprocess, "run", _counting_run)  # type: ignore[attr-defined]

    (tmp_path / "f.txt").write_text("hello\n")
    sha = snapshot(tmp_path, "perf-check")
    assert sha is not None and len(sha) >= 4

    # Exactly two git subcommands: ``add -A`` and ``commit ... -m ...``.
    # The third (``rev-parse --short HEAD``) is GONE — that's the perf win.
    assert len(calls) == 2, (
        f"expected 2 git subprocess calls (add + commit), got {len(calls)}: "
        f"{calls!r}"
    )
    subcommands = [c[1] for c in calls]
    assert subcommands == ["add", "commit"], (
        f"unexpected subcommand sequence: {subcommands!r}"
    )


def test_snapshot_handles_detached_head(tmp_path: Path) -> None:
    """When ``.git/HEAD`` holds a SHA directly (detached HEAD), the
    ref walker still reads it correctly.
    """
    from corlinman_agent.coding import _snapshot as snap_mod
    from corlinman_agent.coding import snapshot

    snap_mod.ensure_repo(tmp_path)
    # Take an initial snapshot so we have a commit to detach to.
    (tmp_path / "a.txt").write_text("x\n")
    first_short = snapshot(tmp_path, "first")
    assert first_short is not None

    # Resolve the full SHA + detach.
    head_full = (tmp_path / ".git" / "HEAD").read_text().strip()
    if head_full.startswith("ref:"):
        ref = head_full[4:].strip()
        full = (tmp_path / ".git" / ref).read_text().strip()
    else:
        full = head_full
    # Write a detached HEAD: the file contains the SHA, no ``ref:`` prefix.
    (tmp_path / ".git" / "HEAD").write_text(full + "\n")

    # Direct call to the helper: detached HEAD must resolve cleanly.
    resolved = snap_mod._read_git_head_sha(tmp_path)
    assert resolved == full

    # And a fresh snapshot succeeds (git advances HEAD to the new commit
    # on the same detached anchor).
    (tmp_path / "a.txt").write_text("y\n")
    second_short = snapshot(tmp_path, "second")
    assert second_short is not None
    assert second_short != first_short


def test_snapshot_handles_packed_refs(tmp_path: Path) -> None:
    """When the loose-ref file is missing but ``packed-refs`` carries
    the entry, the helper finds the SHA via the packed-refs scan.
    """
    from corlinman_agent.coding import _snapshot as snap_mod

    snap_mod.ensure_repo(tmp_path)
    head_text = (tmp_path / ".git" / "HEAD").read_text().strip()
    assert head_text.startswith("ref:"), (
        f"test precondition: HEAD must be a symbolic ref, got {head_text!r}"
    )
    ref_path = head_text[4:].strip()
    loose = tmp_path / ".git" / ref_path
    sha = loose.read_text().strip()

    # Move the ref from loose form into a fake packed-refs file.
    loose.unlink()
    packed = tmp_path / ".git" / "packed-refs"
    packed.write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        f"{sha} {ref_path}\n"
    )

    resolved = snap_mod._read_git_head_sha(tmp_path)
    assert resolved == sha


def test_snapshot_head_parse_failure_returns_none(
    tmp_path: Path, monkeypatch: object
) -> None:
    """When ``.git/HEAD`` is unreadable / malformed the helper returns
    ``None`` and the snapshot logs + returns ``None`` — must NOT crash.
    """
    from corlinman_agent.coding import _snapshot as snap_mod
    from corlinman_agent.coding import snapshot

    snap_mod.ensure_repo(tmp_path)
    # Replace _read_git_head_sha so the SHA lookup fails even though
    # the commit succeeds — proves the failure-path is graceful.
    monkeypatch.setattr(  # type: ignore[attr-defined]
        snap_mod, "_read_git_head_sha", lambda _ws: None
    )

    out = snapshot(tmp_path, "broken")
    # No exception — failure observable only via the None return + log.
    assert out is None


def test_snapshot_short_sha_matches_list_snapshots(tmp_path: Path) -> None:
    """The SHA returned by ``snapshot()`` must round-trip against the
    SHA reported by ``list_snapshots()`` (``%h`` from ``git log``).

    Without this invariant the revert flow breaks — the SHA the agent
    just took wouldn't appear in the snapshot listing.
    """
    from corlinman_agent.coding import list_snapshots, snapshot

    (tmp_path / "f.txt").write_text("v1\n")
    sha1 = snapshot(tmp_path, "v1")
    (tmp_path / "f.txt").write_text("v2\n")
    sha2 = snapshot(tmp_path, "v2")

    listed = list_snapshots(tmp_path)
    seen = {entry["sha"] for entry in listed}
    assert sha1 in seen, f"sha1={sha1!r} not in {seen!r}"
    assert sha2 in seen, f"sha2={sha2!r} not in {seen!r}"


def test_coding_tools_set_includes_revert_changes() -> None:
    """T2.4 wiring: revert_changes must be in CODING_TOOLS and have a schema."""
    from corlinman_agent.coding import CODING_TOOLS, coding_tool_schemas

    assert "revert_changes" in CODING_TOOLS
    schemas = coding_tool_schemas()
    names = {s["function"]["name"] for s in schemas}
    assert "revert_changes" in names
    assert len(schemas) == len(CODING_TOOLS) == 9


# ---------------------------------------------------------------------------
# T2.1 — FileState (per-turn read cache + staleness tracker)
# ---------------------------------------------------------------------------


def test_filestate_cached_read_skips_disk(tmp_path: Path) -> None:
    """Second read hits the cache when mtime is unchanged."""
    import os

    from corlinman_agent.coding import FileState

    path = tmp_path / "f.txt"
    path.write_text("v1\n")
    state = FileState()

    r1 = json.loads(
        dispatch_read_file(args_json=_args(path="f.txt"), workspace=tmp_path, state=state)
    )
    assert "v1" in r1["content"]
    pinned = path.stat().st_mtime
    # Rewrite the bytes but pin mtime to the recorded value, so the
    # cache sees "no change" even though disk content shifted.
    path.write_text("v2-on-disk\n")
    os.utime(path, (pinned, pinned))

    r2 = json.loads(
        dispatch_read_file(args_json=_args(path="f.txt"), workspace=tmp_path, state=state)
    )
    # Cache fired → we see the original "v1", not the on-disk "v2".
    assert "v1" in r2["content"]
    assert "v2-on-disk" not in r2["content"]


def test_filestate_invalidates_on_write(tmp_path: Path) -> None:
    """A write through dispatch_write_file forgets the cached read."""
    from corlinman_agent.coding import FileState

    path = tmp_path / "f.txt"
    path.write_text("v1\n")
    state = FileState()

    dispatch_read_file(args_json=_args(path="f.txt"), workspace=tmp_path, state=state)
    assert state.cached_read(path) is not None

    dispatch_write_file(
        args_json=_args(path="f.txt", content="v2\n"),
        workspace=tmp_path,
        state=state,
    )
    assert state.cached_read(path) is None


def test_filestate_invalidates_on_edit(tmp_path: Path) -> None:
    """An edit through dispatch_edit_file forgets the cached read."""
    from corlinman_agent.coding import FileState

    path = tmp_path / "f.txt"
    path.write_text("alpha beta\n")
    state = FileState()

    dispatch_read_file(args_json=_args(path="f.txt"), workspace=tmp_path, state=state)
    assert state.cached_read(path) is not None

    dispatch_edit_file(
        args_json=_args(path="f.txt", old_string="beta", new_string="GAMMA"),
        workspace=tmp_path,
        state=state,
    )
    assert state.cached_read(path) is None


def test_filestate_is_stale_after_external_mtime_bump(tmp_path: Path) -> None:
    """A change to mtime under the agent flips is_stale True."""
    import os

    from corlinman_agent.coding import FileState

    path = tmp_path / "f.txt"
    path.write_text("v1\n")
    state = FileState()
    dispatch_read_file(args_json=_args(path="f.txt"), workspace=tmp_path, state=state)
    assert state.is_stale(path) is False

    new_mtime = path.stat().st_mtime + 100
    os.utime(path, (new_mtime, new_mtime))
    assert state.is_stale(path) is True


def test_filestate_is_stale_false_when_no_record(tmp_path: Path) -> None:
    """A file the state has never seen is not 'stale'."""
    from corlinman_agent.coding import FileState

    path = tmp_path / "f.txt"
    path.write_text("hi\n")
    state = FileState()
    assert state.is_stale(path) is False


def test_dispatch_read_file_no_state_works_as_before(tmp_path: Path) -> None:
    """Passing state=None keeps the existing single-read behaviour exactly."""
    path = tmp_path / "f.txt"
    path.write_text("hello\n")

    with_state_json = dispatch_read_file(
        args_json=_args(path="f.txt"), workspace=tmp_path, state=None
    )
    without_state_json = dispatch_read_file(
        args_json=_args(path="f.txt"), workspace=tmp_path
    )
    assert json.loads(with_state_json) == json.loads(without_state_json)


# ---------------------------------------------------------------------------
# T2.2 — fuzzy edit matcher + staleness guard
# ---------------------------------------------------------------------------


def test_edit_file_rstrip_tier_matches_trailing_whitespace_drift(tmp_path: Path) -> None:
    """Model's old_string lacks trailing spaces present in the file."""
    body = "def greet(name):  \n    return f'hi {name}'  \n"
    (tmp_path / "g.py").write_text(body)
    # The model omitted the trailing spaces — exact match fails, rstrip wins.
    res = json.loads(
        dispatch_edit_file(
            args_json=_args(
                path="g.py",
                old_string="def greet(name):\n    return f'hi {name}'",
                new_string="def greet(name):\n    return f'hello {name}'",
            ),
            workspace=tmp_path,
        )
    )
    assert res["replacements"] == 1
    assert res["match_tier"] == "rstrip"
    assert "hello" in (tmp_path / "g.py").read_text()


def test_edit_file_strip_tier_matches_indent_drift(tmp_path: Path) -> None:
    """Model's old_string is dedented relative to the file content."""
    body = "class A:\n    def m(self):\n        return 1\n"
    (tmp_path / "a.py").write_text(body)
    res = json.loads(
        dispatch_edit_file(
            args_json=_args(
                path="a.py",
                # Dedented + no leading spaces — exact + rstrip both fail.
                old_string="def m(self):\nreturn 1",
                new_string="def m(self):\n    return 2",
            ),
            workspace=tmp_path,
        )
    )
    assert res["replacements"] == 1
    assert res["match_tier"] == "strip"
    assert "return 2" in (tmp_path / "a.py").read_text()


def test_edit_file_exact_still_wins_over_fuzzy(tmp_path: Path) -> None:
    """When the exact string is present, the exact tier is used (no fuzzy noise)."""
    (tmp_path / "f.py").write_text("alpha\nbeta\ngamma\n")
    res = json.loads(
        dispatch_edit_file(
            args_json=_args(
                path="f.py", old_string="beta", new_string="BETA"
            ),
            workspace=tmp_path,
        )
    )
    assert res["replacements"] == 1
    assert res.get("match_tier") in (None, "exact")  # exact tier elides the field


def test_edit_file_rejects_multiple_fuzzy_matches(tmp_path: Path) -> None:
    """Multiple fuzzy spans without replace_all → not_unique error."""
    body = (
        "def f(x):  \n"
        "    return x\n"
        "\n"
        "def f(x):  \n"
        "    return x\n"
    )
    (tmp_path / "dup.py").write_text(body)
    res = json.loads(
        dispatch_edit_file(
            args_json=_args(
                path="dup.py",
                old_string="def f(x):\n    return x",
                new_string="def f(x):\n    return -x",
            ),
            workspace=tmp_path,
        )
    )
    assert "old_string_not_unique" in res["error"]
    assert "fuzzy" in res["error"]
    # File untouched.
    assert (tmp_path / "dup.py").read_text() == body


def test_edit_file_staleness_guard(tmp_path: Path) -> None:
    """When state says the file changed since the recorded read, edit is refused."""
    import os

    from corlinman_agent.coding import FileState

    path = tmp_path / "stale.py"
    path.write_text("x = 1\n")
    state = FileState()
    # Record a read so the state has a mtime to compare against.
    dispatch_read_file(args_json=_args(path="stale.py"), workspace=tmp_path, state=state)
    # External mtime bump.
    bumped = path.stat().st_mtime + 100
    os.utime(path, (bumped, bumped))
    res = json.loads(
        dispatch_edit_file(
            args_json=_args(
                path="stale.py", old_string="x = 1", new_string="x = 2"
            ),
            workspace=tmp_path,
            state=state,
        )
    )
    assert res["error"] == "file_changed_since_read"
    # File untouched.
    assert path.read_text() == "x = 1\n"


def test_edit_file_no_state_no_staleness_check(tmp_path: Path) -> None:
    """Without state, the staleness guard does not fire."""
    path = tmp_path / "f.py"
    path.write_text("x = 1\n")
    res = json.loads(
        dispatch_edit_file(
            args_json=_args(path="f.py", old_string="x = 1", new_string="x = 2"),
            workspace=tmp_path,
        )
    )
    assert res["replacements"] == 1
    assert path.read_text() == "x = 2\n"


# ---------------------------------------------------------------------------
# read-before-edit guard (Claude-Code parity)
# ---------------------------------------------------------------------------


def test_edit_unread_existing_file_with_state_is_rejected(tmp_path: Path) -> None:
    """A state-threaded edit to a file the agent never read is refused."""
    from corlinman_agent.coding import FileState

    path = tmp_path / "f.py"
    path.write_text("x = 1\n")
    state = FileState()

    res = json.loads(
        dispatch_edit_file(
            args_json=_args(path="f.py", old_string="x = 1", new_string="x = 2"),
            workspace=tmp_path,
            state=state,
        )
    )
    assert res["error"].startswith("file_not_read")
    # File untouched — the blind edit was blocked.
    assert path.read_text() == "x = 1\n"


def test_edit_after_read_with_state_proceeds(tmp_path: Path) -> None:
    """Reading first satisfies the guard."""
    from corlinman_agent.coding import FileState

    path = tmp_path / "f.py"
    path.write_text("x = 1\n")
    state = FileState()
    dispatch_read_file(args_json=_args(path="f.py"), workspace=tmp_path, state=state)
    res = json.loads(
        dispatch_edit_file(
            args_json=_args(path="f.py", old_string="x = 1", new_string="x = 2"),
            workspace=tmp_path,
            state=state,
        )
    )
    assert res["replacements"] == 1
    assert path.read_text() == "x = 2\n"


def test_consecutive_edits_with_state_allowed(tmp_path: Path) -> None:
    """A second edit to a just-edited file (no re-read) is still allowed —
    the edit marks the path seen even though it drops the read cache."""
    from corlinman_agent.coding import FileState

    path = tmp_path / "f.py"
    path.write_text("a = 1\nb = 2\n")
    state = FileState()
    dispatch_read_file(args_json=_args(path="f.py"), workspace=tmp_path, state=state)
    dispatch_edit_file(
        args_json=_args(path="f.py", old_string="a = 1", new_string="a = 10"),
        workspace=tmp_path,
        state=state,
    )
    res = json.loads(
        dispatch_edit_file(
            args_json=_args(path="f.py", old_string="b = 2", new_string="b = 20"),
            workspace=tmp_path,
            state=state,
        )
    )
    assert res["replacements"] == 1
    assert path.read_text() == "a = 10\nb = 20\n"


def test_write_then_edit_with_state_allowed(tmp_path: Path) -> None:
    """A file the agent just wrote can be edited without a redundant read."""
    from corlinman_agent.coding import FileState

    state = FileState()
    dispatch_write_file(
        args_json=_args(path="g.py", content="k = 1\n"),
        workspace=tmp_path,
        state=state,
    )
    res = json.loads(
        dispatch_edit_file(
            args_json=_args(path="g.py", old_string="k = 1", new_string="k = 2"),
            workspace=tmp_path,
            state=state,
        )
    )
    assert res["replacements"] == 1
    assert (tmp_path / "g.py").read_text() == "k = 2\n"


def test_read_before_edit_guard_env_opt_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CORLINMAN_REQUIRE_READ_BEFORE_EDIT=0 disables the guard."""
    from corlinman_agent.coding import FileState

    monkeypatch.setenv("CORLINMAN_REQUIRE_READ_BEFORE_EDIT", "0")
    path = tmp_path / "f.py"
    path.write_text("x = 1\n")
    state = FileState()
    res = json.loads(
        dispatch_edit_file(
            args_json=_args(path="f.py", old_string="x = 1", new_string="x = 2"),
            workspace=tmp_path,
            state=state,
        )
    )
    assert res["replacements"] == 1


def test_filestate_was_seen_tracks_reads_and_writes(tmp_path: Path) -> None:
    from corlinman_agent.coding import FileState

    path = tmp_path / "f.txt"
    path.write_text("hi\n")
    state = FileState()
    assert state.was_seen(path) is False
    dispatch_read_file(args_json=_args(path="f.txt"), workspace=tmp_path, state=state)
    assert state.was_seen(path) is True
    # forget drops the cache but the path stays "seen".
    state.forget(path)
    assert state.cached_read(path) is None
    assert state.was_seen(path) is True


# ---------------------------------------------------------------------------
# read_file truncation guidance (pre-read token-gate parity)
# ---------------------------------------------------------------------------


def test_read_file_truncation_emits_next_offset_and_hint(tmp_path: Path) -> None:
    """A truncated read points the model at the next offset instead of a
    silent head slice it would just re-read."""
    from corlinman_agent.coding._common import MAX_READ_CHARS

    # Build a file whose numbered render comfortably exceeds the char cap.
    line = "x" * 200
    n_lines = (MAX_READ_CHARS // 200) + 50
    path = tmp_path / "big.txt"
    path.write_text("\n".join(line for _ in range(n_lines)) + "\n")

    res = json.loads(
        dispatch_read_file(
            args_json=_args(path="big.txt", offset=1, limit=n_lines),
            workspace=tmp_path,
        )
    )
    assert res["truncated"] is True
    assert isinstance(res["next_offset"], int)
    assert res["next_offset"] > 1
    assert "hint" in res
    # next_offset is the first line NOT fully shown.
    assert res["shown"][1] == res["next_offset"] - 1


def test_write_preserves_existing_file_mode(tmp_path: Path) -> None:
    """Atomic write (tmp→replace) must keep an existing file's mode — e.g. the
    executable bit — not reset it to 0644 (ABSORB_MATRIX Dim 4)."""
    import os as _os
    import stat as _stat

    target = tmp_path / "script.sh"
    target.write_text("#!/bin/sh\necho old\n")
    _os.chmod(target, 0o755)

    res = json.loads(
        dispatch_write_file(
            args_json=_args(path="script.sh", content="#!/bin/sh\necho new\n"),
            workspace=tmp_path,
        )
    )
    assert res["action"] == "overwritten"
    assert target.read_text() == "#!/bin/sh\necho new\n"
    assert _stat.S_IMODE(target.stat().st_mode) == 0o755  # executable bit kept


def test_write_leaves_no_staging_temp_file(tmp_path: Path) -> None:
    """The atomic staging temp file is renamed onto the target, never left."""
    dispatch_write_file(
        args_json=_args(path="a.txt", content="hello"), workspace=tmp_path
    )
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    assert (tmp_path / "a.txt").read_text() == "hello"
