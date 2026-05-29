"""Coverage + cross-tenant authorization-boundary tests for
:mod:`corlinman_server.gateway.mcp.server`.

The MCP gateway bootstrap is security-critical: ``token_config_to_acl``
is the single translator that carries a configured token's
per-capability allowlists **and its tenant scope** from the gateway's
``[mcp.server.tokens]`` config into the runtime :class:`TokenAcl` the
WebSocket transport stamps onto every connection. A regression here
(dropping an allowlist entry, or — worst case — losing/overwriting the
``tenant_id``) silently widens what a token can reach or leaks one
tenant's memory into another's session.

These tests pin that boundary:

* a full token config maps 1:1 into the runtime ACL (allowlists
  translated faithfully, label preserved);
* an empty / missing ``tenant_id`` falls back to ``DEFAULT_TENANT_ID``
  via :meth:`TokenAcl.effective_tenant` — and does so per-token so the
  fallback never bleeds across entries;
* a *set* ``tenant_id`` is preserved verbatim (no cross-tenant leak,
  no clobber by the default);
* ``build_server_config`` / ``build_dispatcher`` / ``build_mcp_server``
  construct a real, ready-to-bind server from a representative config.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from corlinman_mcp_server import (
    DEFAULT_TENANT_ID,
    AdapterDispatcher,
    McpServer,
    McpServerConfig,
    PluginOutputSuccess,
    TokenAcl,
)
from corlinman_server.gateway.mcp.server import (
    DEFAULT_MAX_FRAME_BYTES,
    McpConfig,
    McpServerSection,
    McpTokenConfig,
    build_dispatcher,
    build_mcp_server,
    build_server_config,
    token_config_to_acl,
)

# ---------------------------------------------------------------------
# Minimal in-memory doubles for the bridge protocols build_dispatcher
# consumes. Shapes mirror the corlinman-mcp-server test conftest stubs;
# kept local so this test owns no cross-package import dependency.
# ---------------------------------------------------------------------


@dataclass
class _StubSkill:
    name: str
    description: str = ""
    body_markdown: str = ""


class _StubSkillRegistry:
    def __init__(self, skills: list[_StubSkill] | None = None) -> None:
        self._skills: dict[str, _StubSkill] = {s.name: s for s in (skills or [])}

    def get(self, name: str) -> _StubSkill | None:
        return self._skills.get(name)

    def __iter__(self) -> Iterator[_StubSkill]:
        return iter(self._skills.values())


class _StubPluginRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, Any] = {}

    def add(self, entry: Any) -> None:
        self._entries[entry.manifest.name] = entry

    def list(self) -> list[Any]:
        return list(self._entries.values())

    def get(self, name: str) -> Any:
        return self._entries.get(name)


class _StubPluginRuntime:
    def __init__(self) -> None:
        self.seen: list[Any] = []

    async def execute(self, input_, progress, cancel):  # noqa: ARG002
        self.seen.append(input_)
        return PluginOutputSuccess(content=b"{}")

    def kind(self) -> str:
        return "stub"


class _StubMemoryHost:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name

    async def query(self, req):  # noqa: ARG002
        return []

    async def upsert(self, doc) -> str:  # noqa: ARG002
        raise NotImplementedError

    async def delete(self, id: str) -> None:  # noqa: ARG002
        raise NotImplementedError

    async def get(self, id: str):  # noqa: ARG002
        return None


def _full_token_config() -> McpTokenConfig:
    """A token config with every field populated to a distinct value so
    a translation that drops or transposes a field is caught."""
    return McpTokenConfig(
        token="secret-bearer-abc",
        label="scoped-tenant-a",
        tools_allowlist=["fs:read", "search:*"],
        resources_allowed=["memory", "skill"],
        prompts_allowed=["onboarding-*"],
        tenant_id="tenant-a",
    )


# ---------------------------------------------------------------------
# (a) full token config maps 1:1 into the runtime ACL
# ---------------------------------------------------------------------


def test_token_config_to_acl_maps_full_config_faithfully() -> None:
    cfg = _full_token_config()

    acl = token_config_to_acl(cfg)

    assert isinstance(acl, TokenAcl)
    assert acl.token == "secret-bearer-abc"
    assert acl.label == "scoped-tenant-a"
    # (d) allowlists translated faithfully — same contents, same order.
    assert acl.tools_allowlist == ["fs:read", "search:*"]
    assert acl.resources_allowed == ["memory", "skill"]
    assert acl.prompts_allowed == ["onboarding-*"]
    assert acl.tenant_id == "tenant-a"


def test_token_config_to_acl_copies_allowlists_not_aliases() -> None:
    """The translator must defensively copy each allowlist (the source
    uses ``list(...)``). Mutating the runtime ACL must not write back
    through to the config, and vice-versa — otherwise a per-connection
    ACL tweak could mutate shared config state."""
    cfg = _full_token_config()

    acl = token_config_to_acl(cfg)

    acl.tools_allowlist.append("admin:*")
    assert cfg.tools_allowlist == ["fs:read", "search:*"]

    cfg.resources_allowed.append("persona")
    assert acl.resources_allowed == ["memory", "skill"]


# ---------------------------------------------------------------------
# (b) empty / missing tenant_id falls back to DEFAULT_TENANT_ID
# ---------------------------------------------------------------------


def test_missing_tenant_id_falls_back_to_default() -> None:
    """``tenant_id=None`` (the dataclass default) must resolve to
    ``DEFAULT_TENANT_ID`` once ``effective_tenant`` is consulted."""
    cfg = McpTokenConfig(token="t", tools_allowlist=["*"])
    assert cfg.tenant_id is None

    acl = token_config_to_acl(cfg)

    # The raw field is preserved as None — the fallback is applied at
    # resolution time, not eagerly stamped.
    assert acl.tenant_id is None
    assert acl.effective_tenant() == DEFAULT_TENANT_ID
    assert acl.to_session_context().tenant_id == DEFAULT_TENANT_ID


def test_empty_string_tenant_id_falls_back_to_default() -> None:
    """An explicitly-empty ``tenant_id`` ("") counts as missing and must
    also fall back — matching the Rust impl's empty-string handling."""
    cfg = McpTokenConfig(token="t", tenant_id="")

    acl = token_config_to_acl(cfg)

    assert acl.tenant_id == ""
    assert acl.effective_tenant() == DEFAULT_TENANT_ID


# ---------------------------------------------------------------------
# (c) a SET tenant_id is preserved (no cross-tenant leak / clobber)
# ---------------------------------------------------------------------


def test_set_tenant_id_is_preserved_not_defaulted() -> None:
    cfg = McpTokenConfig(token="t", tenant_id="tenant-xyz")

    acl = token_config_to_acl(cfg)

    assert acl.tenant_id == "tenant-xyz"
    assert acl.effective_tenant() == "tenant-xyz"
    assert acl.effective_tenant() != DEFAULT_TENANT_ID
    assert acl.to_session_context().tenant_id == "tenant-xyz"


def test_per_token_tenant_scope_does_not_cross_leak() -> None:
    """Translating a batch of tokens with mixed tenant scopes must keep
    each token's scope isolated — a defaulting token must not inherit a
    sibling's tenant, and a scoped token must not leak into the default
    bucket. This is the core cross-tenant authorization invariant."""
    section = McpServerSection(
        tokens=[
            McpTokenConfig(token="ta", tenant_id="tenant-a"),
            McpTokenConfig(token="tb", tenant_id="tenant-b"),
            McpTokenConfig(token="td"),  # no tenant -> default
        ],
    )

    acls = [token_config_to_acl(t) for t in section.tokens]
    effective = [a.effective_tenant() for a in acls]

    assert effective == ["tenant-a", "tenant-b", DEFAULT_TENANT_ID]
    # No two distinct configured tenants collapse into one another.
    assert acls[0].effective_tenant() != acls[1].effective_tenant()
    # The defaulting token did not absorb either sibling's tenant.
    assert acls[2].effective_tenant() not in ("tenant-a", "tenant-b")


# ---------------------------------------------------------------------
# build_server_config — config -> McpServerConfig
# ---------------------------------------------------------------------


def test_build_server_config_translates_tokens_and_frame_cap() -> None:
    cfg = McpConfig(
        enabled=True,
        server=McpServerSection(
            max_frame_bytes=4096,
            tokens=[
                _full_token_config(),
                McpTokenConfig(token="other", tenant_id="tenant-b"),
            ],
        ),
    )

    server_cfg = build_server_config(cfg)

    assert isinstance(server_cfg, McpServerConfig)
    assert server_cfg.max_frame_bytes == 4096
    assert [a.token for a in server_cfg.tokens] == ["secret-bearer-abc", "other"]
    assert [a.effective_tenant() for a in server_cfg.tokens] == [
        "tenant-a",
        "tenant-b",
    ]
    # First token's allowlists survive the round-trip through the config.
    assert server_cfg.tokens[0].tools_allowlist == ["fs:read", "search:*"]


def test_build_server_config_defaults_frame_cap_and_empty_tokens() -> None:
    """An all-defaults section yields the default frame cap and a
    fail-closed (empty) token list."""
    server_cfg = build_server_config(McpConfig())

    assert server_cfg.max_frame_bytes == DEFAULT_MAX_FRAME_BYTES
    assert server_cfg.tokens == []


# ---------------------------------------------------------------------
# build_dispatcher — registries + memory hosts -> AdapterDispatcher
# ---------------------------------------------------------------------


def test_build_dispatcher_constructs_with_three_capabilities() -> None:
    plugins = _StubPluginRegistry()
    skills = _StubSkillRegistry([_StubSkill(name="onboarding-intro")])
    memory_hosts = {"tenant-a": _StubMemoryHost("tenant-a")}
    runtime = _StubPluginRuntime()

    dispatcher = build_dispatcher(plugins, skills, memory_hosts, runtime)

    assert isinstance(dispatcher, AdapterDispatcher)
    # ServerInfo identity wired from the package name + resolved version.
    assert dispatcher.server_info.name == "corlinman"
    assert isinstance(dispatcher.server_info.version, str)
    assert dispatcher.server_info.version
    # All three capability adapters registered -> advertised.
    caps = dispatcher.capabilities
    assert caps.tools is not None
    assert caps.resources is not None
    assert caps.prompts is not None


# ---------------------------------------------------------------------
# build_mcp_server — full bootstrap; honours the enabled flag
# ---------------------------------------------------------------------


def _representative_config(*, enabled: bool) -> McpConfig:
    return McpConfig(
        enabled=enabled,
        server=McpServerSection(
            bind="127.0.0.1:0",
            max_frame_bytes=8192,
            tokens=[_full_token_config()],
        ),
    )


def test_build_mcp_server_returns_none_when_disabled() -> None:
    """``[mcp].enabled = False`` must omit the server entirely so the
    boot path skips the bind (mirrors the Rust ``Option`` contract)."""
    result = build_mcp_server(
        _representative_config(enabled=False),
        _StubPluginRegistry(),
        _StubSkillRegistry(),
        {"tenant-a": _StubMemoryHost("tenant-a")},
        _StubPluginRuntime(),
    )
    assert result is None


def test_build_mcp_server_constructs_from_representative_config() -> None:
    cfg = _representative_config(enabled=True)
    plugins = _StubPluginRegistry()
    skills = _StubSkillRegistry([_StubSkill(name="onboarding-intro")])
    memory_hosts = {"tenant-a": _StubMemoryHost("tenant-a")}
    runtime = _StubPluginRuntime()

    server = build_mcp_server(cfg, plugins, skills, memory_hosts, runtime)

    assert isinstance(server, McpServer)
    # The server carries the translated config (frame cap + token ACL
    # with its tenant scope intact) and a real dispatcher handler.
    assert server.config.max_frame_bytes == 8192
    assert [a.effective_tenant() for a in server.config.tokens] == ["tenant-a"]
    assert isinstance(server.handler, AdapterDispatcher)
    assert server.handler.server_info.name == "corlinman"
