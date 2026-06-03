# Architecture & Module Map

## System Overview

Corlinman is a multi-layered LLM platform with 25+ Python packages organized around an HTTP/WebSocket gateway, agentic reasoning loop, multi-channel message routing, provider abstraction, and extensible skill/memory systems.

### Layer Organization

**P0: HTTP/WebSocket Gateway** (56K LOC, monolith in corlinman-server)
- Boot orchestration, config loading, middleware wiring (lifecycle, middleware, core modules)
- Public API routes: chat, canvas, models, health, voice WebSocket
- Admin control-plane: multi-tenant config, session replay, credentials, personas, API keys, scheduler
- Runtime services: chat pipeline, evolution orchestration, channel adapters, gRPC bridging, provider registry

**P1: Reasoning Loop & Tools** (corlinman-agent, 15K LOC)
- Multi-turn agentic reasoning with tool-call aggregation
- Domain-specific tool suites: coding (file/shell/search), web, vision, memory, persona lifecycle
- Subagent spawning with depth/concurrency caps
- Context assembly (agent expansion, variable cascade, skill injection, placeholder render)

**P2: Message Transport** (corlinman-channels, 12K LOC)
- Seven channel adapters (QQ/OneBot, Telegram, Discord, Slack, Feishu, WeChat, LogStream)
- Normalized inbound event stream, slash-command routing, rate-limiting, bi-directional send
- Mutable-spinner status UI, token-bucket per-user throttling

**P3: Provider Abstraction** (corlinman-providers, 10K LOC)
- Unified CorlinmanProvider Protocol across 18+ LLM vendors
- Vendor error normalization to CorlinmanError hierarchy for failover
- Provider-config declarative TOML specs, plugin lifecycle/sandbox, capability probes

**P4: Data & Knowledge** (corlinman-memory-host, evolution-*.packages, corlinman-goals, 20K LOC)
- Hybrid memory search (SQLite+FTS5, remote HTTP, federated RRF, read-only isolation)
- Evolution loop: signal clustering → proposal generation → shadow testing → auto-rollback
- Goal tracking with reflection grading and evidence windows

**Foundation** (corlinman-persona, corlinman-identity, corlinman-grpc, corlinman-wstool, corlinman-nodebridge, corlinman-skills-registry, corlinman-subagent, corlinman-hooks, corlinman-mcp-server, corlinman-canvas, 24K LOC)
- Session state & user identity resolution
- gRPC service stubs, distributed tool bus, mobile device registration
- Skill registry, concurrent subagent supervisor, event hook bus, MCP server
- Canvas rendering (code, tables, LaTeX, mermaid)

---

## Module Map: Complete Reference

| Module/Area | Responsibility | Public Interface | Key Inbound Deps | Key Outbound Deps | Owner Area |
|---|---|---|---|---|---|
| **server:gateway/boot-middleware** | Orchestrate FastAPI boot: config load, state build, route mount, lifespan, middleware wire | `build_app`, `AppState`, `get_app_state`, `load_from_path`, `init_metrics`, `require_api_key`, `require_admin`, `require_approval`, `require_tenant` | routes, routes_voice, routes_admin_a/b, grpc, services, providers | fastapi, starlette, uvicorn, structlog, tenancy, persona, memory_host, identity, hooks, scheduler | **gateway-lead** |
| **server:gateway/public-routes** | HTTP/WebSocket user-facing endpoints: chat, canvas, models, health, voice, OAuth credential mgmt | `routes.router()`, `GatewayState`, `routes_voice.router()`, `oauth.storage`, `oauth.sessions`, PKCE flows | routes_admin_b, channels_runtime, services | middleware.auth, gateway_api, ChatService, approval_queue, tenancy, providers | **voice-chat-platform-team** |
| **server:gateway/admin-a** | Admin control-plane: session lifecycle, auth, channels, personas, API keys, identity, profiles, tenant mgmt | `router()`, `AdminState`, `require_admin_dependency`, admin CRUD routes (11 sub-domains) | entrypoint, routes_admin_b, gateway.core | tenancy, persona, profiles, sessions, identity, argon2, structlog | **admin-control-plane-team** |
| **server:gateway/admin-b** | Admin backend: config CRUD, model/provider mgmt, evolution queue, skill hub, scheduler, sessions analytics, system | 26 sub-routers (agents, config, credentials, evolution, plugins, scheduler, skills, etc.) | entrypoint | providers, evolution_store, skills_registry, scheduler, system.marketplace, profiles | **admin-backend-team** |
| **server:runtime-services** | Chat pipeline, evolution orchestration, channel adapters, gRPC bridging, provider registry, placeholder resolution | `ChatService`, `ChatBackend`, `EvolutionApplier`, `PlaceholderService`, `ProviderRegistry`, `MCP server builder` | routes/chat, evolution, channels_runtime, grpc | gateway_api, providers, evolution_store, channels, hooks, memory_host, mcp_server | **runtime-orchestration-team** |
| **server:system** | Update checker, audit log, marketplace sources (GitHub/ClawHub), plugin/skill installers, one-click upgrader, subagent dispatcher | `UpdateChecker`, `SystemAuditLog`, `MarketplaceSource`, `install_skill`, `resolve_upgrader`, `AsyncSubagentDispatcher` | routes_admin_b.system, routes_admin_b.skills, routes_admin_b.subagents | httpx, providers.plugins, docker, tarfile, asyncio | **system-integration-team** |
| **server:platform/scheduler** | Cron-based job runner, hook bus integration, persistent run history, builtin task registry | `spawn`, `dispatch`, `SchedulerConfig`, `SchedulerStore`, `SchedulerHandle` | routes_admin_b.scheduler, lifecycle, cli | croniter, aiosqlite, hooks, structlog | **scheduler-orchestration-team** |
| **server:platform/tenancy** | Multi-tenant DB pooling, AdminDb schema (tenants/admins/api_keys), path layout, TenantId newtype | `TenantId`, `TenantPool`, `AdminDb`, `default_tenant`, path functions | middleware, routes_admin_a, gateway.core, lifecycle, legacy_migration | aiosqlite, sqlite3, structlog | **tenancy-primitives-team** |
| **server:platform/persona** | Agent session state (mood/fatigue/topics), asset storage, persona store CRUD, default seeding | `PersonaStore`, `PersonaAssetStore`, `Persona`, `seed_builtin_personas`, `DEFAULT_GRANTLEY_ID` | routes_admin_a/b, gateway.core, agent_servicer, memory_tools | aiosqlite, structlog | **persona-lifecycle-team** |
| **server:platform/profiles** | Profile registry (clone, CRUD, path layout), skill directory, state DB | `ProfileStore`, `Profile`, `profile_root`, `profile_skills_dir`, `ensure_profile_dirs` | routes_admin_a.profiles, routes_admin_b.skills, gateway.core | sqlite3, structlog | **profile-management-team** |
| **server:platform/cli** | Operator CLI entry point, subcommands (tenant, doctor, config, onboard, plugins, replay) | `main` (console script), subcommand modules | __main__.py, entrypoint for `corlinman` binary | click, structlog, providers, replay, config | **devops-cli-team** |
| **server:tools** | PDF document rendering for admin UX | `doc_render` module | routes_admin_a/b (lazy) | – | **documentation-team** |
| **pkg:agent** | Multi-turn reasoning loop, context assembly, tool execution, permission gating, subagent spawning, memory curation | `ReasoningLoop`, `ContextAssembler`, `CODING_TOOLS`, `IMAGE_*`, `PERSONA_*`, `SkillRegistry`, `VariableCascade`, `CuratorPipeline` | agent_servicer (gRPC), runner_pool, gateway.lifecycle, system.subagent, memory tools, persona tools | providers, grpc, coding tools, image tools, web tools, hooks, memory_host, persona, episodes, goals | **reasoning-agent-team** |
| **pkg:channels** | Seven channel adapters (QQ/Telegram/Discord/Slack/Feishu/WeChat/LogStream), message normalization, slash commands | `InboundEvent`, `ChannelBinding`, adapters (OneBotAdapter, TelegramAdapter, etc.), `run_*_channel`, `ChannelRouter`, `COMMAND_REGISTRY` | gateway.channels_runtime, routes_admin_a/b, services | identity, hooks, ChatService | **channels-gateway-team** |
| **pkg:providers** | LLM vendor abstraction (18+ vendors), error normalization, plugin lifecycle/sandbox, OAuth flows | `CorlinmanProvider` (Protocol), `ProviderRegistry`, `ProviderChunk`, `CorlinmanError` hierarchy, declarative specs, plugin APIs | agent_servicer, routes_admin_b/providers, gateway.services, direct_backend, cli | anthropic, openai, google-generativeai, httpx, docker, cryptography, pydantic | **provider-adapters-team** |
| **pkg:memory-host** | Hybrid memory search: SQLite+FTS5, remote HTTP, federated RRF, read-only wrapper | `MemoryHost` (Protocol), `LocalSqliteHost`, `RemoteHttpHost`, `FederatedMemoryHost`, `ReadOnlyMemoryHost`, `MemoryQuery/Hit/Doc` | agent_servicer, gateway.memory, memory tools, rag_store | aiosqlite, httpx | **memory-backend-team** |
| **pkg:evolution-engine** | Signal clustering, proposal generation, handler dispatch (7 kinds) | `EvolutionEngine.run_once()`, `KindHandler` (Protocol), handler impls | gateway.routes_admin_b.evolution, gateway.evolution, scheduler | evolution_store | **evolution-engine-team** |
| **pkg:evolution-store** | SQLite schema, 4 repos (signals/proposals/history/intent), async context manager | `EvolutionStore`, `ProposalsRepo`, `SignalsRepo`, `HistoryRepo`, `IntentLogRepo`, `EvolutionProposal/Signal/History` types | evolution_engine, shadow_tester, auto_rollback, gateway.evolution | aiosqlite | **evolution-store-team** |
| **pkg:shadow-tester** | Medium/high-risk proposal evaluation via in-process simulators (memory_op, skill_update, tag_rebalance) | `ShadowRunner.run_once()`, `KindSimulator` (Protocol) | gateway.evolution, scheduler | evolution_store, PyYAML | **shadow-tester-team** |
| **pkg:auto-rollback** | Post-apply metric regression monitoring, auto-revert strategies per evolution kind | `AutoRollbackMonitor.run_once()`, `Applier` (Protocol) | gateway.evolution, scheduler | evolution_store | **auto-rollback-team** |
| **pkg:skills-registry** | SKILL.md parser, runtime requirement checking, usage telemetry | `SkillRegistry`, `Skill`, `SkillRequirements`, `bump_use/bump_view` | routes_admin_b.skills, gateway.services.context_assembler, mcp_server | PyYAML, structlog | **skills-infrastructure-team** |
| **pkg:persona** | Persona state machine (mood/fatigue/recent topics), decay cron, placeholder resolver | `PersonaState`, `PersonaStore`, `PersonaResolver`, `apply_decay`, `seed_from_card` | gateway.services, agent.persona tools, cli | aiosqlite, croniter, structlog | **persona-state-team** |
| **pkg:identity** | User identity resolution (3-table schema: identities/aliases/verification_phrases), channel adapter protocol | `UserId`, `ChannelAlias`, `VerificationPhrase`, `SqliteIdentityStore`, `IdentityStore` (Protocol), `ChannelAdapter` | channels, gateway, replay, agent_brain | aiosqlite, structlog | **identity-management-team** |
| **pkg:grpc** | gRPC service stubs (6 services), agent client, chat stream, tool executor | agent_pb2/agent_pb2_grpc, plugin_pb2, llm_pb2, AgentClient, ChatStream, ToolExecutor | agent_servicer, gateway.grpc | grpcio, opentelemetry | **grpc-infrastructure-team** |
| **pkg:wstool** | Distributed tool bus protocol (invoke/result/error over WebSocket), registry, heartbeat | `WsToolServer`, `WsToolRunner`, `ToolAdvert`, `FileFetcher`, `DiskFileServer` | agent tools, gateway.wstool | websockets, pydantic, hooks | **distributed-tools-team** |
| **pkg:nodebridge** | Mobile device registration protocol (iOS/Android/macOS), job dispatch, telemetry bridge | `NodeBridgeServer`, `NodeBridgeClient`, `NodeBridgeMessage`, `Capability`, `DispatchJob` | gateway.nodebridge | websockets, pydantic, hooks | **device-protocol-team** |
| **pkg:subagent** | Concurrency cap accountant (per-parent/per-tenant/depth), async task lifecycle driver | `Supervisor`, `SupervisorPolicy`, `Slot`, `TaskSpec`, `ParentContext` | agent.subagent tools, gateway.services | asyncio, hooks | **subagent-supervisor-team** |
| **pkg:hooks** | Priority-tiered async event bus, bounded buffers, cooperative cancel tokens | `HookBus`, `HookSubscription`, `HookEvent`, `HookPriority`, `CancelToken` | all packages (cross-cutting) | asyncio, structlog | **hooks-infrastructure-team** |
| **pkg:mcp-server** | MCP 2024-11-05 server, tools/resources/prompts adapters, plugin/skill/memory bridges | `McpServer`, `McpServerConfig`, `AdapterDispatcher`, `ToolsAdapter`, `ResourcesAdapter` | gateway.mcp, agent tools, plugins, skills | websockets, structlog, pydantic | **mcp-protocol-team** |
| **pkg:canvas** | Pure-function renderers: code (Pygments), tables, LaTeX, sparkline, mermaid | `Renderer`, `CanvasPresentPayload`, `RenderedArtifact` | routes/canvas, agent image tools | pygments, markdown-it-py, pylatexenc | **canvas-rendering-team** |
| **pkg:goals** | Goal hierarchy store (short/mid/long tiers), reflection grading, evidence windows | `GoalStore`, `Goal`, `GoalEvaluation`, `GoalsResolver`, `Grader`, reflection jobs | gateway (lazy), persona (bridge) | aiosqlite, PyYAML, providers | **goals-intelligence-team** |

---

## Dependency Overview & Coupling Hotspots

### Gateway Monolith (56K LOC)

The `corlinman-server` gateway is a multi-layer HTTP service:

```
entrypoint.py (4040 LOC)
├─ lifecycle/   (admin_seed, legacy_migration, py_config, starter_skills)
├─ middleware/  (auth, admin_auth, admin_session, approval, tenant_scope, trace)
├─ core/        (config, config_watcher, state, server, metrics, telemetry, log_broadcast)
├─ routes (2.8K LOC, 10 handlers)
│   └── routes_voice/ (4.9K LOC, audio WebSocket)
│   └── oauth/ (2.6K LOC, PKCE flows × 6 vendors)
├─ routes_admin_a (7.4K LOC, 11 sub-routers)
│   └── auth, channels, sessions, personas, agents, api_keys, tenants, approvals, identity, profiles, password_reset
├─ routes_admin_b (16.9K LOC, 26 sub-routers)
│   └── skills, plugins, curator, evolution, scheduler, providers, config, models, credentials, system, subagents, extensions, mcp_market, ...
├─ services (chat pipeline, evolution observer, channel runtime, grpc bridging, provider registry)
├─ grpc (placeholder server, agent client wrapper)
├─ channels_runtime (7 channel adapters bootstrap)
├─ providers (ProviderRegistry attachment)
├─ placeholder ({{memory.*}}, {{episodes.*}} resolvers)
├─ mcp (dispatcher wiring)
└─ observability (event fan-out to journal + SSE)
```

**Critical Hotspots:**

1. **entrypoint.py (4040 LOC)** — single file orchestrating all boot, config loading, route mounting, lifespan, 30+ wiring tasks, admin seeding, scheduler setup. Merge conflict magnet.

2. **routes_admin_b (16.9K LOC across 26 submodules)** — god bundle for admin backend. Top files:
   - `skills.py` (1699 LOC) — skill install, uninstall, CRUD
   - `providers.py` (1342 LOC) — provider config, model alias CRUD
   - `evolution.py` (1216 LOC) — evolution proposal approval, status
   - `scheduler.py` (1140 LOC) — job CRUD, cron parsing, runtime history
   - `config.py`, `models.py`, `credentials.py`, `plugins.py` (800–1100 LOC each)
   - All 26 submodules share mutable `AdminState.extras` dict (no type safety)

3. **routes_admin_a (7.4K LOC across 11 submodules)** — admin control-plane. Top files:
   - `auth.py` (931 LOC) — session lifecycle, credential hashing, login rate limiting
   - `channels.py` (1125 LOC) — QQ/Telegram/Discord/Slack/Feishu config + status checks
   - `sessions.py` (1041 LOC) — replay surface, journal queries

4. **Shared mutable AdminState (228 lines)** — merge magnet. Added by both admin-a and admin-b modules. Every new subsystem (skills, evolution, scheduler) adds a field and wires it in boot sequence.

### Cross-Package Coupling

**Chain Imports (Circular Risk):**
- `entrypoint.py` → routes_admin_b → oauth → routes_admin_b.credentials → entrypoint (via AdminState mutation)
- `routes_admin_a.auth` → state.py → routes_admin_a._auth_shim (soft cross-bundle dependency)
- `admin-b.personas` ↔ `admin-a.personas` (cross-bundle finalization import)

**Shared Mutable State:**
- `AdminState.extras` dict — 27 modules use `extras[key]` by convention with no registry (scheduler_runtime_jobs, config_swap_fn, skill_registry_factory, etc.)
- `channels_runtime._status` constants (STATUS_THINKING, TEXT_LIMIT) — per-channel mutation without locks
- `oauth.sessions._oauth_sessions` dict + threading.Lock (not async-safe)

**Leaky Boundaries:**
- `providers.py` hard-codes 18 vendor error → CorlinmanError mappings (in each provider adapter, not shared)
- `routes_admin_b` reaches into routes_admin_a for persona finalization instead of calling a facade
- `auth.py + password_reset.py` compete for admin credential writes under `admin_write_lock`

### Dependency Chains (P0 → P4)

```
HTTP Requests
  ↓
routes.chat → ChatService (gateway.services)
  ↓
ReasoningLoop (corlinman-agent) → ProviderRegistry (corlinman-providers)
  ↓
Vendor SDKs (anthropic, openai, google, bedrock, etc.) — 18 implementations
  ↓
ProviderChunk stream → tool_call aggregation → agent ToolExecutor
  ↓
Subagent Supervisor (corlinman-subagent) + approval gates
  ↓
Channel Adapters (corlinman-channels) + Memory Host (corlinman-memory-host)
```

---

## Proposed Ownership Map

Group the 25 packages + gateway monolith into **8 independent owner-areas**, each a team that can iterate in parallel:

### 1. **Gateway Orchestration** (gateway-lead)
- Owns: `gateway/lifecycle`, `gateway/core`, `gateway/middleware`
- Responsibility: Boot, config hot-reload, middleware wiring, AppState composition, lifespan hooks
- Interfaces with all other teams via stable AppState contract
- CODEOWNERS: `@corlinman/gateway-lead`

### 2. **Voice & Chat Platform** (voice-chat-platform-team)
- Owns: `gateway/routes`, `gateway/routes_voice`, `gateway/oauth`
- Responsibility: User-facing HTTP/WebSocket endpoints, OAuth credential flows, model fallback
- Consumes: ChatService, ProviderRegistry, approval gates, memory stubs
- CODEOWNERS: `@corlinman/voice-chat-platform-team`

### 3. **Admin Control-Plane** (admin-control-plane-team)
- Owns: `gateway/routes_admin_a`
- Responsibility: Session/auth/channels/personas/identity/profiles/tenants/api_keys/password recovery
- Consumes: tenancy, persona store, identity store, profiles store
- CODEOWNERS: `@corlinman/admin-control-plane-team`

### 4. **Admin Backend** (admin-backend-team)
- Owns: `gateway/routes_admin_b` (except persona finalization exports)
- Responsibility: Config/model/provider/evolution/scheduler/skills CRUD, system control surface
- Consumes: providers, evolution_store, skills_registry, scheduler, system.marketplace
- CODEOWNERS: `@corlinman/admin-backend-team`

### 5. **Runtime Orchestration** (runtime-orchestration-team)
- Owns: `gateway/services`, `gateway/evolution`, `gateway/channels_runtime`, `gateway/grpc`, `gateway/providers`, `gateway/placeholder`, `gateway/mcp`, `gateway/observability`
- Responsibility: Chat pipeline, evolution orchestration, channel dispatch, provider registry, placeholder resolution
- Central hub: wires reasoning loop + channels + memory + evolution into working system
- CODEOWNERS: `@corlinman/runtime-orchestration-team`

### 6. **Reasoning & Agent Tools** (reasoning-agent-team)
- Owns: `corlinman-agent` (all submodules)
- Responsibility: Multi-turn loop, context assembly, tool execution, permission gating, subagent spawning, memory curation
- Consumes: providers, grpc, memory_host, persona, goals, skills
- CODEOWNERS: `@corlinman/reasoning-agent-team`

### 7. **Message Transports & Integrations** (channels-gateway-team)
- Owns: `corlinman-channels`
- Responsibility: Seven channel adapters, inbound event normalization, slash-command routing, outbound send
- Consumes: identity, ChatService, hooks
- CODEOWNERS: `@corlinman/channels-gateway-team`

### 8. **Provider Abstraction & Plugin Platform** (provider-adapters-team + plugin-platform-team)
- **Provider Adapters Team** owns: `corlinman-providers` (base, 18 vendor adapters, specs, registry, declarative, error normalization)
- **Plugin Platform Team** owns: `corlinman-providers/plugins` (manifest, sandbox, lifecycle, approval, discovery, async_task)
- Responsibility: LLM vendor unification, error mapping, plugin lifecycle, capability probes
- Consumes: vendor SDKs (anthropic, openai, google, etc.), docker, httpx
- CODEOWNERS: `@corlinman/provider-adapters-team`, `@corlinman/plugin-platform-team`

### 9. **Data & Knowledge Layer** (memory-backend-team + evolution-engine-team + goals-intelligence-team)
- **Memory Backend Team** owns: `corlinman-memory-host`
- **Evolution Engine Team** owns: `corlinman-evolution-engine`, `corlinman-evolution-store`, `corlinman-shadow-tester`, `corlinman-auto-rollback`
- **Goals Intelligence Team** owns: `corlinman-goals`
- Responsibility: Hybrid memory search, evolution loop, goal tracking with reflection
- CODEOWNERS: `@corlinman/memory-backend-team`, `@corlinman/evolution-engine-team`, `@corlinman/goals-intelligence-team`

### 10. **Platform Services** (platform-services-team)
- Owns: `corlinman-server/scheduler`, `corlinman-server/tenancy`, `corlinman-server/persona`, `corlinman-server/profiles`, `corlinman-server/cli`, `corlinman-server/tools`, `corlinman-server/bundled_skills`
- Responsibility: Job scheduling, multi-tenant DB pooling, operator CLI, profile/persona/bundled-skill registries
- CODEOWNERS: `@corlinman/platform-services-team`

### 11. **Foundation Infrastructure** (foundation-infrastructure-team)
- Owns: `corlinman-persona` (session state), `corlinman-identity`, `corlinman-grpc`, `corlinman-wstool`, `corlinman-nodebridge`, `corlinman-skills-registry`, `corlinman-subagent`, `corlinman-hooks`, `corlinman-mcp-server`, `corlinman-canvas`
- Responsibility: Cross-cutting primitives, wire protocols, stateful services (identity/persona stores)
- CODEOWNERS: `@corlinman/foundation-infrastructure-team`

### 12. **System Integration** (system-integration-team)
- Owns: `corlinman-server/system` (marketplace, upgrader, audit, skill_hub)
- Responsibility: Update checking, plugin/skill marketplace, one-click upgrader, audit logging
- CODEOWNERS: `@corlinman/system-integration-team`

---

## CODEOWNERS File

```text
# Core Gateway Orchestration
python/packages/corlinman-server/src/corlinman_server/gateway/lifecycle/           @corlinman/gateway-lead
python/packages/corlinman-server/src/corlinman_server/gateway/core/                @corlinman/gateway-lead
python/packages/corlinman-server/src/corlinman_server/gateway/middleware/          @corlinman/gateway-lead

# Voice & Chat Platform
python/packages/corlinman-server/src/corlinman_server/gateway/routes/              @corlinman/voice-chat-platform-team
python/packages/corlinman-server/src/corlinman_server/gateway/routes_voice/        @corlinman/voice-chat-platform-team
python/packages/corlinman-server/src/corlinman_server/gateway/oauth/               @corlinman/voice-chat-platform-team

# Admin Control-Plane
python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_a/      @corlinman/admin-control-plane-team

# Admin Backend
python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_b/      @corlinman/admin-backend-team

# Runtime Orchestration
python/packages/corlinman-server/src/corlinman_server/gateway/services/            @corlinman/runtime-orchestration-team
python/packages/corlinman-server/src/corlinman_server/gateway/evolution/           @corlinman/runtime-orchestration-team
python/packages/corlinman-server/src/corlinman_server/gateway/channels_runtime/    @corlinman/runtime-orchestration-team
python/packages/corlinman-server/src/corlinman_server/gateway/grpc/                @corlinman/runtime-orchestration-team
python/packages/corlinman-server/src/corlinman_server/gateway/providers/           @corlinman/runtime-orchestration-team
python/packages/corlinman-server/src/corlinman_server/gateway/placeholder/         @corlinman/runtime-orchestration-team
python/packages/corlinman-server/src/corlinman_server/gateway/mcp/                 @corlinman/runtime-orchestration-team
python/packages/corlinman-server/src/corlinman_server/gateway_api/                 @corlinman/runtime-orchestration-team
python/packages/corlinman-server/src/corlinman_server/gateway/observability/       @corlinman/runtime-orchestration-team

# Reasoning & Agent Tools
python/packages/corlinman-agent/                                                   @corlinman/reasoning-agent-team
python/packages/corlinman-agent-brain/                                             @corlinman/reasoning-agent-team

# Message Transports & Integrations
python/packages/corlinman-channels/                                                @corlinman/channels-gateway-team

# Provider Abstraction & Plugin Platform
python/packages/corlinman-providers/src/corlinman_providers/                       @corlinman/provider-adapters-team
python/packages/corlinman-providers/src/corlinman_providers/plugins/               @corlinman/plugin-platform-team

# Data & Knowledge Layer
python/packages/corlinman-memory-host/                                             @corlinman/memory-backend-team
python/packages/corlinman-evolution-engine/                                        @corlinman/evolution-engine-team
python/packages/corlinman-evolution-store/                                         @corlinman/evolution-engine-team
python/packages/corlinman-shadow-tester/                                           @corlinman/evolution-engine-team
python/packages/corlinman-auto-rollback/                                           @corlinman/evolution-engine-team
python/packages/corlinman-goals/                                                   @corlinman/goals-intelligence-team

# Platform Services
python/packages/corlinman-server/src/corlinman_server/scheduler/                   @corlinman/platform-services-team
python/packages/corlinman-server/src/corlinman_server/tenancy/                     @corlinman/platform-services-team
python/packages/corlinman-server/src/corlinman_server/persona/                     @corlinman/platform-services-team
python/packages/corlinman-server/src/corlinman_server/profiles/                    @corlinman/platform-services-team
python/packages/corlinman-server/src/corlinman_server/cli/                         @corlinman/platform-services-team
python/packages/corlinman-server/src/corlinman_server/tools/                       @corlinman/platform-services-team
python/packages/corlinman-server/src/corlinman_server/bundled_skills/              @corlinman/platform-services-team

# Foundation Infrastructure
python/packages/corlinman-persona/                                                 @corlinman/foundation-infrastructure-team
python/packages/corlinman-identity/                                                @corlinman/foundation-infrastructure-team
python/packages/corlinman-grpc/                                                    @corlinman/foundation-infrastructure-team
python/packages/corlinman-wstool/                                                  @corlinman/foundation-infrastructure-team
python/packages/corlinman-nodebridge/                                              @corlinman/foundation-infrastructure-team
python/packages/corlinman-skills-registry/                                         @corlinman/foundation-infrastructure-team
python/packages/corlinman-subagent/                                                @corlinman/foundation-infrastructure-team
python/packages/corlinman-hooks/                                                   @corlinman/foundation-infrastructure-team
python/packages/corlinman-mcp-server/                                              @corlinman/foundation-infrastructure-team
python/packages/corlinman-canvas/                                                  @corlinman/foundation-infrastructure-team

# System Integration
python/packages/corlinman-server/src/corlinman_server/system/                      @corlinman/system-integration-team
```

---

## Cross-Cutting Concerns & Integration Seams

### Seam: AdminState Mutation
**Problem:** entrypoint.py builds skeleton AdminState → lifespan populates fields → admin-a/b routes expect specific fields (no type validation).
**Responsibility:** gateway-lead owns AdminState contract evolution. Admin-control-plane and admin-backend teams request new fields via gateway-lead code review.

### Seam: Config Hot-Reload
**Problem:** config_watcher (gateway-lead) monitors TOML → emits hook → admin-backend.config routes must re-sync.
**Responsibility:** gateway-lead owns watcher contract. Admin-backend-team implements hook listeners.

### Seam: Scheduler Job Builtins
**Problem:** platform-services-team (scheduler) + admin-backend-team (evolution) both register default jobs in entrypoint (qzone_daily, darwin_curate, update_check).
**Responsibility:** platform-services-team owns scheduler lifecycle. Admin-backend-team passes job specs to scheduler at boot via explicit AdminState wiring.

### Seam: Provider Registry
**Problem:** provider-adapters-team (corlinman-providers) builds registry → runtime-orchestration-team attaches to AppState → voice-chat-platform-team consumes in DirectProviderBackend.
**Responsibility:** runtime-orchestration-team owns provider-to-AppState attachment. voice-chat-platform-team calls registry.resolve() via shared interface.

### Seam: Memory & Evolution
**Problem:** reasoning-agent-team (memory tools) + evolution-engine-team (memory_op handler) both read/write memory_host.
**Responsibility:** evolution-engine-team owns memory_op proposal logic. reasoning-agent-team implements memory tool UX. Both depend on memory-backend-team's MemoryHost interface (no concurrent mutations).

---

## Key Metrics & Health Signals

| Metric | Current State | Target | Owner |
|---|---|---|---|
| **entrypoint.py LOC** | 4040 | < 2000 | gateway-lead |
| **routes_admin_b LOC** | 16.9K (26 files) | Split into 6 sub-packages | admin-backend-team |
| **routes_admin_a LOC** | 7.4K (11 files) | Reduce to < 4K | admin-control-plane-team |
| **Monolith gateway LOC** | 56K | < 40K via extraction | gateway-lead |
| **AdminState.extras keys** | 27+ string keys (no registry) | Typed dataclass hierarchy | gateway-lead |
| **Test fixture duplication** | conftest.py AdminState in 5+ files | Shared factory | all teams |
| **Middleware install site** | Scattered across entrypoint | Single bootstrap.py | gateway-lead |
| **Cross-bundle imports** | admin-a.auth ↔ admin-b.personas | Facade pattern | admin-lead |

---

## Deployment & Review Model

1. **Gateway Lead** approves all changes to lifecycle, core, middleware (affects all other teams).
2. **Admin-Control-Plane + Admin-Backend** change ONLY via AdminState contract negotiation with gateway-lead (to avoid merge conflicts on AdminState dataclass).
3. **Runtime-Orchestration** owns all P2–P4 plumbing (services, evolution, channels_runtime, grpc, providers). Changes to ProviderRegistry interface require voice-chat and reasoning-agent sign-off.
4. **All package teams** (corlinman-*) require code review from designated maintainer before merge (CODEOWNERS enforcement).
5. **Cross-team seams** (scheduler builtins, provider registry, memory host) require approvals from both owner-areas.

---

## Document History

- **2026-06-03** — Initial architecture & module map captured from module-descriptor JSONs. 25 packages, 85K LOC gateway monolith identified. Ownership split into 12 teams. Modularization opportunities recorded per package.