"""Scoped ``.mcp.json`` discovery + precedence merge (Dim 5).

Pins: file shape (claude-code ``mcpServers`` + corlinman aliases),
precedence ``local > project > user > inline``, total-function
degradation on malformed files, and shadowed-server position stability.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from corlinman_mcp_server.scoped_config import (
    load_scoped_server_specs,
    scope_files,
)


def _write(path: Path, servers: dict[str, Any], key: str = "mcpServers") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({key: servers}), encoding="utf-8")


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "proj"
    user = tmp_path / "home"
    project.mkdir()
    user.mkdir()
    return project, user


def test_scope_files_layout(tmp_path: Path) -> None:
    files = scope_files(project_dir=tmp_path / "p", user_dir=tmp_path / "u")
    assert files["user"] == tmp_path / "u" / "mcp.json"
    assert files["project"] == tmp_path / "p" / ".mcp.json"
    assert files["local"] == tmp_path / "p" / ".mcp.local.json"


def test_no_files_falls_back_to_inline(tmp_path: Path) -> None:
    project, user = _dirs(tmp_path)
    specs = load_scoped_server_specs(
        {"mcp": {"servers": {"inline-srv": {"command": "x"}}}},
        project_dir=project,
        user_dir=user,
    )
    assert [s.name for s in specs] == ["inline-srv"]


def test_precedence_local_over_project_over_user_over_inline(
    tmp_path: Path,
) -> None:
    project, user = _dirs(tmp_path)
    _write(user / "mcp.json", {"srv": {"command": "from-user"}, "u-only": {"command": "u"}})
    _write(project / ".mcp.json", {"srv": {"command": "from-project"}, "p-only": {"command": "p"}})
    _write(project / ".mcp.local.json", {"srv": {"command": "from-local"}})

    specs = load_scoped_server_specs(
        {"mcp": {"servers": {"srv": {"command": "from-inline"}, "i-only": {"command": "i"}}}},
        project_dir=project,
        user_dir=user,
    )
    by_name = {s.name: s for s in specs}
    assert by_name["srv"].command == "from-local"
    # Non-colliding servers from every scope all survive.
    assert set(by_name) == {"srv", "i-only", "u-only", "p-only"}
    # A shadowed server keeps its original (weakest-scope) position.
    assert next(s.name for s in specs) == "srv"


def test_corlinman_key_aliases_accepted(tmp_path: Path) -> None:
    project, user = _dirs(tmp_path)
    _write(project / ".mcp.json", {"a": {"command": "x"}}, key="mcp_servers")
    _write(user / "mcp.json", {"b": {"url": "wss://x"}}, key="servers")
    specs = load_scoped_server_specs(
        None, project_dir=project, user_dir=user
    )
    assert {s.name for s in specs} == {"a", "b"}


def test_malformed_file_skipped_not_raised(tmp_path: Path) -> None:
    project, user = _dirs(tmp_path)
    (project / ".mcp.json").write_text("{not json", encoding="utf-8")
    _write(project / ".mcp.local.json", {"ok": {"command": "x"}})
    specs = load_scoped_server_specs(
        None, project_dir=project, user_dir=user
    )
    assert [s.name for s in specs] == ["ok"]


def test_bad_server_entry_skipped_others_kept(tmp_path: Path) -> None:
    project, user = _dirs(tmp_path)
    _write(
        project / ".mcp.json",
        {"good": {"command": "x"}, "bad": "not-a-mapping"},
    )
    specs = load_scoped_server_specs(
        None, project_dir=project, user_dir=user
    )
    assert [s.name for s in specs] == ["good"]
