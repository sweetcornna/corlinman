"""Tests for the builtin coding tools (file ops, search, shell).

Every tool is workspace-confined; these tests pass an explicit
``workspace=tmp_path`` so they never touch the real
``CORLINMAN_AGENT_WORKSPACE``.
"""

from __future__ import annotations

import json
from pathlib import Path

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
