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
    assert len(schemas) == len(CODING_TOOLS) == 8
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
