"""Tests for ``POST /admin/plugins/{name}/invoke``.

Regression coverage for the bug where the route always returned 501
``invoke_runtime_unavailable``: it ``getattr``'d a non-existent
``corlinman_providers.plugins.sandbox.jsonrpc_execute`` / ``execute``
symbol instead of the real plugin executor. The fix wires the route to
``gateway.grpc.plugin_invoker.build_registry_invoker`` — the same invoker
the chat tool-executor uses — so a registered ``sync`` plugin is actually
spawned and its JSON-RPC result flows back.

The happy path writes a tiny real Python plugin to disk, registers it in
a live :class:`~corlinman_providers.plugins.PluginRegistry`, and drives a
test-invoke through the HTTP route.
"""

from __future__ import annotations

import json
import sys
import textwrap
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
from corlinman_providers.plugins import (
    MANIFEST_FILENAME,
    Origin,
    PluginEntry,
    PluginManifest,
    PluginRegistry,
)
from corlinman_server.gateway.routes_admin_b.marketplace import plugins
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._admin_auth import authenticated_test_client, configure_admin_auth

# ---------------------------------------------------------------------------
# Real-on-disk sync plugin scaffold (mirrors tests/gateway/grpc).
# ---------------------------------------------------------------------------

_ECHO_PLUGIN_SOURCE = textwrap.dedent(
    """
    import json, sys
    line = sys.stdin.readline()
    req = json.loads(line)
    resp = {
        "jsonrpc": "2.0",
        "id": req.get("id"),
        "result": {"echoed": req.get("params"), "method": req.get("method")},
    }
    sys.stdout.write(json.dumps(resp) + "\\n")
    sys.stdout.flush()
    """
).strip()


def _write_sync_plugin(
    root: Path, name: str, *, tool: str = "echo", plugin_type: str = "sync"
) -> PluginEntry:
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.py").write_text(_ECHO_PLUGIN_SOURCE, encoding="utf-8")

    manifest_body = textwrap.dedent(
        f"""
        name = "{name}"
        version = "0.1.0"
        plugin_type = "{plugin_type}"

        [entry_point]
        command = "{sys.executable}"
        args = ["plugin.py"]

        [[capabilities.tools]]
        name = "{tool}"
        description = "test tool"
        """
    ).strip()
    (plugin_dir / MANIFEST_FILENAME).write_text(manifest_body, encoding="utf-8")

    manifest = PluginManifest.model_validate(tomllib.loads(manifest_body))
    manifest.migrate_to_current_in_memory()
    return PluginEntry(
        manifest=manifest,
        origin=Origin.WORKSPACE,
        manifest_path=plugin_dir / MANIFEST_FILENAME,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def registry(tmp_path: Path) -> PluginRegistry:
    import asyncio

    reg = PluginRegistry()
    asyncio.run(reg.upsert(_write_sync_plugin(tmp_path, "echoer", tool="echo")))
    asyncio.run(
        reg.upsert(
            _write_sync_plugin(tmp_path, "svc", tool="ping", plugin_type="service")
        )
    )
    return reg


@pytest.fixture()
def admin_state(registry: PluginRegistry) -> Iterator[AdminState]:
    state = AdminState(plugins=registry)
    configure_admin_auth(state)
    set_admin_state(state)
    try:
        yield state
    finally:
        set_admin_state(None)


@pytest.fixture()
def client(admin_state: AdminState) -> TestClient:
    app = FastAPI()
    app.include_router(plugins.router())
    return authenticated_test_client(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_invoke_runs_real_sync_plugin(client: TestClient) -> None:
    """A registered sync plugin is spawned and its result flows back — no
    more spurious 501 ``invoke_runtime_unavailable``."""
    resp = client.post(
        "/admin/plugins/echoer/invoke",
        json={"tool": "echo", "arguments": {"x": 9}},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "success"
    assert payload["result"] == {"echoed": {"x": 9}, "method": "echo"}
    # The raw JSON-RPC result text is preserved too.
    assert json.loads(payload["result_raw"]) == payload["result"]


def test_invoke_unknown_plugin_404s(client: TestClient) -> None:
    resp = client.post(
        "/admin/plugins/ghost/invoke",
        json={"tool": "anything", "arguments": {}},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"] == "not_found"


def test_invoke_undeclared_tool_400s(client: TestClient) -> None:
    resp = client.post(
        "/admin/plugins/echoer/invoke",
        json={"tool": "not-a-real-tool", "arguments": {}},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "tool_not_declared"


def test_invoke_service_plugin_still_501s(client: TestClient) -> None:
    """Service plugins are short-circuited *before* the executor — that
    explicit 501 (unsupported, not 'runtime unavailable') is preserved."""
    resp = client.post(
        "/admin/plugins/svc/invoke",
        json={"tool": "ping", "arguments": {}},
    )
    assert resp.status_code == 501, resp.text
    assert resp.json()["error"] == "invoke_unsupported"
