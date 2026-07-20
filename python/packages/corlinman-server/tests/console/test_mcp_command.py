"""``/mcp`` console command — list / tools / add / remove / lifecycle
(Dim 5 parity: the claude-code ``/mcp`` analog).

The command talks to the embedded brain's :class:`McpClientManager`
handle; a brain without one (attach mode / direct fallback) degrades to
a clean "unavailable" line, mirroring ``/hooks``. Mutations trigger the
brain's ``refresh_mcp_tools`` so the model-facing advertisement follows
hot-plug without a restart.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

import pytest
from corlinman_mcp_server.client_manager import (
    McpManagedServer,
    McpServerSpec,
)
from corlinman_server.console.brain import BrainSession
from corlinman_server.console.commands import dispatch
from corlinman_server.console.render import Renderer
from corlinman_server.console.router import ModelRouter
from rich.console import Console

pytestmark = pytest.mark.asyncio


@dataclass
class _Tool:
    name: str
    description: str = ""


class _FakeManager:
    """Minimal stand-in for McpClientManager's ``/mcp`` surface."""

    def __init__(self) -> None:
        spec = McpServerSpec.from_mapping(
            "files", {"command": "mcp-files", "args": ["--root", "/tmp"]}
        )
        self._rows: dict[str, McpManagedServer] = {
            "files": McpManagedServer(
                spec=spec, status="ready", tools=[_Tool("read"), _Tool("write")]
            )
        }
        # ``is_ready`` needs a non-None peer.
        self._rows["files"].peer = object()  # type: ignore[assignment]
        self.calls: list[str] = []

    def servers(self) -> list[McpManagedServer]:
        return list(self._rows.values())

    def server(self, name: str) -> McpManagedServer | None:
        return self._rows.get(name)

    def discovered_tools(self) -> dict[str, list[Any]]:
        return {
            n: list(r.tools) for n, r in self._rows.items() if r.is_ready
        }

    async def add_server(self, spec: McpServerSpec, *, replace: bool = False) -> McpManagedServer:
        if spec.name in self._rows and not replace:
            raise ValueError(f"mcp server {spec.name!r} already registered")
        row = McpManagedServer(spec=spec, status="ready")
        row.peer = object()  # type: ignore[assignment]
        self._rows[spec.name] = row
        self.calls.append(f"add:{spec.name}")
        return row

    async def remove_server(self, name: str) -> bool:
        self.calls.append(f"remove:{name}")
        return self._rows.pop(name, None) is not None

    async def restart_one(self, name: str) -> bool:
        self.calls.append(f"restart:{name}")
        return name in self._rows

    async def enable_one(self, name: str) -> bool:
        self.calls.append(f"enable:{name}")
        return name in self._rows

    async def disable_one(self, name: str) -> bool:
        self.calls.append(f"disable:{name}")
        return name in self._rows


@dataclass
class _McpBrain:
    descriptor: str = "stub brain with mcp"
    manager: _FakeManager = field(default_factory=_FakeManager)
    refreshes: int = 0

    @property
    def mcp_manager(self) -> _FakeManager:
        return self.manager

    async def ensure_mcp_manager(self) -> _FakeManager:
        return self.manager

    async def refresh_mcp_tools(self) -> bool:
        self.refreshes += 1
        return True

    def run_turn(self, **_kw: Any) -> Any:  # pragma: no cover — unused
        raise AssertionError("commands must not run turns")

    async def aclose(self) -> None:  # pragma: no cover — unused
        pass


class _BareBrain:
    descriptor = "stub brain without mcp surface"

    def run_turn(self, **_kw: Any) -> Any:  # pragma: no cover — unused
        raise AssertionError("commands must not run turns")

    async def aclose(self) -> None:  # pragma: no cover — unused
        pass


class StubApp:
    def __init__(self, brain: Any) -> None:
        self.session = BrainSession(brain=brain, model="big")
        self.renderer = Renderer(
            Console(file=io.StringIO(), force_terminal=False)
        )
        self.router = ModelRouter(
            default_model="big", small_fast_model="small", auto_route=False
        )
        self.running = True


async def test_mcp_unavailable_without_surface() -> None:
    app = StubApp(_BareBrain())
    out = await dispatch(app, "/mcp tools")
    assert "unavailable" in str(out)


async def test_mcp_list_renders_servers() -> None:
    app = StubApp(_McpBrain())
    out = str(await dispatch(app, "/mcp"))
    assert "files" in out
    assert "ready" in out
    assert "tools=2" in out


async def test_mcp_tools_renders_namespaced_names() -> None:
    app = StubApp(_McpBrain())
    out = str(await dispatch(app, "/mcp tools"))
    assert "files_read" in out
    assert "files_write" in out


async def test_mcp_add_url_and_stdio_and_refresh() -> None:
    brain = _McpBrain()
    app = StubApp(brain)

    out = str(await dispatch(app, "/mcp add web wss://example.com/mcp"))
    assert "added 'web'" in out
    assert brain.manager.server("web") is not None
    assert brain.manager.server("web").spec.transport == "ws"

    out = str(await dispatch(app, "/mcp add local mcp-run --flag x"))
    assert "added 'local'" in out
    spec = brain.manager.server("local").spec
    assert spec.transport == "stdio"
    assert spec.command == "mcp-run"
    assert spec.args == ["--flag", "x"]

    # Every mutation re-advertised the tool plane.
    assert brain.refreshes == 2


async def test_mcp_add_duplicate_is_reported() -> None:
    app = StubApp(_McpBrain())
    out = str(await dispatch(app, "/mcp add files mcp-files"))
    assert "already registered" in out


async def test_mcp_remove_restart_enable_disable() -> None:
    brain = _McpBrain()
    app = StubApp(brain)

    assert "'files': ready" in str(await dispatch(app, "/mcp test files"))
    assert "enabled 'files'" in str(await dispatch(app, "/mcp enable files"))
    assert "disabled 'files'" in str(await dispatch(app, "/mcp disable files"))
    assert "removed 'files'" in str(await dispatch(app, "/mcp remove files"))
    assert "no server named 'files'" in str(
        await dispatch(app, "/mcp remove files")
    )
    assert brain.manager.calls == [
        "restart:files",
        "enable:files",
        "disable:files",
        "remove:files",
        "remove:files",
    ]
    assert brain.refreshes == 4  # the failed remove does not refresh


async def test_mcp_usage_line_on_unknown_subcommand() -> None:
    app = StubApp(_McpBrain())
    out = str(await dispatch(app, "/mcp frobnicate"))
    assert out.startswith("usage: /mcp")
