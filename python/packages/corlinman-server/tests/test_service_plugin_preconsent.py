"""Gateway-side trust-but-verify on service-plugin dispatch (audit W3-2).

The agent-side EP2 gate consents before yielding a tool call, but the
gateway is the process that actually runs the plugin — the dispatcher
re-evaluates the unified gate under the exact canonical key and refuses a
hard ``deny`` before even spawning the service. Approved calls stamp
``PluginToolCall.approval_preconsented`` so the plugin bridge does not
re-prompt.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from corlinman_agent.authz.defaults import apply_permissions_config
from corlinman_server.gateway.grpc._service_plugin_dispatcher import (
    ServicePluginDispatcher,
)


class _ExplodingSupervisor:
    """Proves the deny path never reaches spawn."""

    def __init__(self) -> None:
        self.spawns = 0

    async def spawn_service(self, _manifest: object) -> str:
        self.spawns += 1
        raise AssertionError("denied call must not spawn the service")


def _entry(name: str = "file-ops") -> SimpleNamespace:
    return SimpleNamespace(manifest=SimpleNamespace(name=name))


@pytest.mark.asyncio
async def test_denied_plugin_tool_never_spawns() -> None:
    apply_permissions_config(
        {"rules": [{"tool": "plugin:file-ops/*", "action": "deny"}]}
    )
    supervisor = _ExplodingSupervisor()
    dispatcher = ServicePluginDispatcher(supervisor)

    result = await dispatcher.dispatch(
        _entry(), "write", {"path": "/x"}, timeout_ms=1000
    )

    assert result.is_error
    body = json.loads(result.content)
    assert body["error"] == "permission_denied"
    assert supervisor.spawns == 0


@pytest.mark.asyncio
async def test_wildcard_deny_blocks_at_gateway_too() -> None:
    apply_permissions_config({"rules": [{"tool": "*", "action": "deny"}]})
    dispatcher = ServicePluginDispatcher(_ExplodingSupervisor())
    result = await dispatcher.dispatch(_entry("net"), "fetch", {}, timeout_ms=1000)
    assert result.is_error
    assert json.loads(result.content)["error"] == "permission_denied"


def test_allowed_verdict_and_ask_pass_verification() -> None:
    """Only a hard deny blocks gateway-side; ``ask`` is trusted as already
    resolved by the agent EP (this hop has no prompt channel)."""
    apply_permissions_config(
        {"rules": [{"tool": "plugin:net/fetch", "action": "ask"}]}
    )
    dispatcher = ServicePluginDispatcher(_ExplodingSupervisor())
    assert dispatcher._authz_action("net", "fetch", {}) == "ask"
    apply_permissions_config(None)
    assert dispatcher._authz_action("net", "fetch", {}) == "allow"
