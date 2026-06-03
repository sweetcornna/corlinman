# Modularization Roadmap

**corlinman Python monorepo · gateway monolith decomposition**
Status: PLAN ONLY — no code is moved or edited by this document.
Source of truth: the 14 module maps + `architecture-modules.md` (current-state module map, CODEOWNERS).

---

## 1. Goals & Non-Goals

### Goals

1. **Enable many devs in parallel.** The monolith forces serialization on a handful of god-files (`entrypoint.py`, `AdminState`, `routes_admin_b/skills.py`, `channels/service.py`). The target state lets each owner-area iterate behind a stable contract without touching shared hotspots.
2. **No big-bang rewrite.** Every change is an *extraction* of existing, working code into a clearer boundary — never a re-implementation. Code keeps its current behavior; only its import path and ownership change.
3. **Preserve behavior.** Each phase ships behind a **backward-compat re-export shim** at the old import path, so consumers (and in-flight PRs) keep working through the migration. No public symbol disappears in the same PR that moves it.
4. **Lean on existing CI.** import-linter already runs a "no cycles" / boundary-check job. We add *one contract per extracted boundary* so a regression (a re-introduced cycle, a leak across a new seam) fails CI rather than review.
5. **Replace the merge magnets with typed seams.** Specifically: the `AdminState` god-object and its `extras` string-keyed dict (the #1 hotspot, touched by 6+ contributors/month across admin-a and admin-b), and the `entrypoint.py` (4040 LOC) boot orchestrator.

### Non-Goals

- **Not** splitting `corlinman-server` into multiple distributions/wheels in this roadmap. The decomposition is *intra-package* (new subpackages with enforced boundaries). Promotion to separate packages (e.g. a standalone `corlinman_gateway_api`) is noted as a *follow-on* opportunity, not a phase here.
- **Not** rewriting any vendor adapter, channel adapter, or provider error-mapping logic. Provider/channel god-files are addressed only where the maps flag a clean internal split (schema vs. adapter), and only as later, lower-priority phases.
- **Not** changing runtime/deploy topology, config file formats, or the wire protocols (gRPC stubs, wstool/nodebridge frames, MCP). Deploy compatibility is a hard constraint (see §8).
- **Not** re-litigating the owner-areas already established in `architecture-modules.md` CODEOWNERS — we *refine* them, not replace them.

---

## 2. Problem Ranking (by collaboration pain)

Ranked by the collaboration-pain evidence in the maps: frequency of merge conflicts, number of contributors/month, and blast radius of a change.

### Tier 1 — Active, daily merge magnets (fix first)

| # | Problem | Root cause (grounded in maps) |
|---|---------|-------------------------------|
| **1.1** | **`AdminState` god-object + `extras` dict.** Shared across admin-a (200+ lines, 30+ optional fields, 11 sub-routers) *and* admin-b (228 lines, 27 modules). | Every new subsystem adds a field; every boot site wires it; every test reconstructs it. The free-form `extras` dict is keyed by convention (`scheduler_runtime_jobs`, `config_swap_fn`, `config_watcher`, `hook_runner`, `skill_registry_factory`, …) with **no type safety and no key registry** — competing writers to the same key silently break tests. |
| **1.2** | **`entrypoint.py` (4040 LOC) is the single boot hotspot.** | Holds config loading, state building, route mounting, lifespan with 30+ wiring tasks, admin seed, scheduler-job registration, C2 handles, persona stores, plugin hotload, identity sweep, update checker, config watcher. *Every* gateway boot feature lands here. It also builds/mutates `AdminState` across 3 separate code sites, so 1.1 and 1.2 are coupled. |
| **1.3** | **Config-write duplication across admin surfaces.** `_write_config_atomic` is copy-pasted in 4 modules (`onboard`, `models`, `providers`, `credentials`); `_redact` and config sub-key merging duplicated. | No shared config-mutation layer. Atomicity guarantees, TOML-writer fallbacks, and error codes can't be changed in one place — they must be changed in 4, in parallel, by different owners. This directly collides with the **config-coverage initiative** (see §5). |

### Tier 2 — Per-area god-files that serialize a single team

| # | Problem | Root cause |
|---|---------|-----------|
| **2.1** | **admin-b god-files**: `skills.py` (1699), `providers.py` (1342), `evolution.py` (1216), `scheduler.py` (1140) each fuse CRUD + UI + business logic. | The 16.9K-LOC bundle has one nominal owner but four independently-evolving concerns; marketplace/skills, provider config, evolution queue, and scheduler all collide in `routes_admin_b/*`. |
| **2.2** | **`channels/service.py` (5217 LOC) hub with cyclic imports.** | `service` imports 12 channel modules; those re-import `service` (lazy). Every new channel feature lands in `handle_one_*`/`run_*` in `service.py`; `_status.py` (1132 LOC) shared spinner couples all channels' text-limit semantics. |
| **2.3** | **admin-a god-files**: `auth.py` (931), `channels.py` (1125), `sessions.py` (1041). | `auth.py` mixes hashing + session lifecycle + rate limiting + rotation; `auth.py` and `password_reset.py` *compete for the admin credential write* under `admin_write_lock` (race risk). Tenant-resolution logic is duplicated across `api_keys.py`/`sessions.py`/`tenants.py`. |
| **2.4** | **Cross-bundle persona triangle.** `routes_admin_b.onboard` → `routes_admin_b.personas.ensure_default_persona_active`; `personas` imports `require_admin` from `state`; `admin-b.personas` ↔ `admin-a.personas` finalization import. | A tight coupling triangle across two owner-areas; persona finalization should live in admin-a (where persona CRUD already is) and be called via a facade. |

### Tier 3 — Real but lower-frequency / single-owner internal splits

| # | Problem | Root cause |
|---|---------|-----------|
| **3.1** | **`routes_voice/mod.py` (1642 LOC)** single-dev async state machine; **OAuth PKCE flows** duplicated 250–350 LOC × 4 vendors. | Pump loop fuses router + budget + approval + persistence + provider; PKCE providers share no base. |
| **3.2** | **`system` duplication**: `UpgradeStateStore`≈`SubagentTaskStore`; `plugin_store`≈`mcp_store`; skill/plugin tarball installers 95% duplicated. | No shared `AsyncJsonPersistentStore` / `ExtensionStore<T>` / `HardenedTarballInstaller` base. A bug fixed in one copy must be back-ported. |
| **3.3** | **`providers` god-files**: `anthropic_provider.py` (1305), `bedrock_provider.py` (753); plugin platform (1600+ LOC) bundled inside the provider package. | Schema (`specs`/`registry`/`declarative`/`capabilities`) is mixed with adapters; `plugins/` is orthogonal but co-packaged. |
| **3.4** | **`runtime-services` bootstrap fusion**: `channels_runtime/__init__.py` (916 LOC); `grpc/__init__.py` bundles 3 concerns; evolution observer/applier/curator concurrency uncodified. | Per-channel builders nested in one file; `grpc` merges placeholder UDS + agent server + plugin invoker. |
| **3.5** | **Foundation/infra internal god-modules**: agent `reasoning_loop.py` (2781), `tool_wrapper.py` (2755); `hooks/event.py` (857, 27 variants); memory `local_sqlite.py` (1134); evolution `repo.py` (1146). | Package-internal and already single-owner, so lowest priority for the *cross-team* goal — but they belong on each owner's backlog. |

---

## 3. Target Decomposition

Concrete, independently-ownable modules. Each is an **extraction of existing code** into a clearer subpackage with a published contract and an import-linter contract that fences it. New paths are under existing trees unless noted. "Contract enforced by" names the import-linter rule type (the CI job already understands `forbidden`, `independence`, `layers`, and "no cycles").

### 3.1 `gateway-core` — AdminState + middleware + app composition

- **What moves (existing code):** `gateway/core/state.py` (`AppState`), the middleware state objects (`ApiKeyAuthState`, `AdminAuthState`, `TenantScopeState`, `ApprovalMiddlewareState`) into a single `gateway/core/middleware_state.py` with a `MiddlewareStateBuilder`; a new `gateway/middleware/bootstrap.py` with one `install(app, state)` entry-point encapsulating the trace → tenant_scope → auth → approval ordering; the **AdminState type definitions** move here as the *single* place that owns the typed state surface (see §4). Construction stays in admin-a/admin-b builders.
- **Public contract:** `build_app`, `AppState`, `get_app_state`, `install(app, state)`, `require_api_key/require_admin/require_approval/require_tenant`, plus the typed `AdminState` slices (§4). This is the existing boot-middleware public interface, now *closed* (no `extras` escape hatch).
- **import-linter contract:** a **`layers`** contract placing `gateway.core` + `gateway.middleware` as the lowest gateway layer; routes/admin/services may import *down* into core but core must not import *up*. Plus an **independence** rule among the middleware modules. Owner: `@corlinman/gateway-lead`.

### 3.2 `admin-config` — shared config-mutation layer

- **What moves:** the duplicated `_write_config_atomic`, `_redact`, `config_snapshot`, sub-key merge helpers (today copied across `onboard`, `models`, `providers`, `credentials`) into one `gateway/core/config_mutation.py`. The five admin-b config modules import from it.
- **Public contract:** `write_config_atomic(path, patch) -> Result`, `redact(cfg)`, `snapshot(cfg)` — one definition of atomicity, TOML-writer fallback, and error codes.
- **import-linter contract:** a **`forbidden`** rule: no module *other than* the designated config modules may import `config_mutation` internals, and `config_mutation` may not import any `routes_admin_*` module (keeps it a leaf the config-coverage initiative can target). Owner: `@corlinman/admin-backend-team` (config sub-owner).

### 3.3 `admin-marketplace` — skills + plugins + MCP/plugin market bundle

- **What moves:** `routes_admin_b/skills.py` (1699), `plugins.py`, `plugin_market.py`, `mcp_market.py`, `marketplace_settings.py`, `extensions.py` into a `routes_admin_b/marketplace/` subpackage with its own router composition and its own state subset (`clawhub_client`, `skill_install_store`/`SkillInstallTaskStore`, `mcp_market_state`). Cuts ~3500 LOC out of the shared bundle.
- **Public contract:** `marketplace.build_router()`, and a typed `MarketplaceAdminState` slice (§4) instead of `extras["skill_registry_factory"]`, etc.
- **import-linter contract:** an **`independence`** rule between `routes_admin_b.marketplace` and the other admin-b sub-bundles (config, infra, evolution). Owner: `@corlinman/admin-backend-team` (marketplace sub-owner).

### 3.4 `admin-infrastructure` — scheduler + evolution + system controls

- **What moves:** `routes_admin_b/scheduler.py` (1140) + scheduler-history wiring, `evolution.py` (1216), `system.py`, `subagents.py`, `memory.py`, `hooks.py`, `logs.py`, `sessions_*` into a `routes_admin_b/infra/` subpackage. Large IO-heavy handlers split into `core` (status/list) + `mutations` (approve/apply/upsert) — internal to this bundle, no contract needed.
- **Public contract:** `infra.build_router()`, typed `SchedulerAdminState` + `EvolutionAdminState` slices (replacing `extras["scheduler_runtime_jobs"]`, `extras["scheduler_history"]`, etc.). Scheduler job specs are passed to `corlinman_server.scheduler` via explicit wiring (no back-import from scheduler into gateway).
- **import-linter contract:** **`independence`** from `admin-marketplace` and `admin-config`; **`forbidden`** rule that `routes_admin_b.infra` must not import `routes_admin_a.*` directly (forces the facade in 3.7). Owner: `@corlinman/admin-backend-team` (infra sub-owner).

### 3.5 `chat-pipeline` — `gateway_api` + services + backends

- **What moves:** keep `gateway_api` (protocol-only, I/O-free types) and `gateway/services` (ChatService, `DirectProviderBackend`, `GrpcAgentChatBackend`, `chat_bootstrap`) as a cohesive unit; formalize the `ChatBackend` boundary that already exists structurally. Optional follow-on (noted, not a phase): promote `gateway_api` to a standalone `corlinman_gateway_api` package.
- **Public contract:** `ChatService`, `ChatEventStream`, `InternalChatRequest/Event`, `ChatBackend`, `DirectProviderBackend`, `GrpcAgentChatBackend`, `services.bootstrap`.
- **import-linter contract:** a **`layers`** contract: `gateway_api` is a leaf; `services` may import `gateway_api` but routes/admin import `services` only via its public bootstrap/`ChatService`. A **no-cycles** check on `routes.chat` ↔ `gateway_api` ↔ `services`. Owner: `@corlinman/runtime-orchestration-team`.

### 3.6 provider schema/adapter split + plugin-platform

- **What moves (later-priority, package-internal):** group `specs.py`, `registry.py`, `declarative.py`, `capabilities.py` as the **schema/config** surface; keep the vendor adapters as the **adapter** surface. Separately, fence the `plugins/` subtree (manifest, sandbox, lifecycle, approval, discovery, registry, async_task) as the orthogonal **plugin platform** it already is.
- **Public contract:** schema side exposes `ProviderSpec/ProviderKind/AliasEntry/EmbeddingSpec`, `load_*_specs`, `ProviderRegistry`; adapter side exposes `CorlinmanProvider`, the concrete `*Provider` classes, `CorlinmanError` hierarchy; plugin side exposes `plugins.*`.
- **import-linter contract:** a **`layers`** rule `adapters → specs`, and a **`forbidden`** rule that nothing in `corlinman_providers` (non-plugin) imports `corlinman_providers.plugins` *except* through the published entry-point. Owners: `@corlinman/provider-adapters-team` + `@corlinman/plugin-platform-team`.

### 3.7 persona-finalization facade (resolves the cross-bundle triangle)

- **What moves:** persona finalization (`ensure_default_persona_active` + `onboard.finalize-persona`) consolidates into admin-a persona CRUD. admin-b calls a thin facade method on admin-a; the reverse import is deleted.
- **Public contract:** an admin-a-owned `finalize_persona_pick(...)` facade.
- **import-linter contract:** a **`forbidden`** rule: `routes_admin_b.*` may not import `routes_admin_a.*` except the single facade module; and `routes_admin_a.*` may not import `routes_admin_b.*` (removes the documented cycle). Owners: `@corlinman/admin-control-plane-team` (facade) + `@corlinman/admin-backend-team` (caller).

> Not proposing new packages the maps don't support: there is **no** "channels micro-package split", "agent-workspace package", or "evolution-commons package" *phase* here — those are recorded in the maps as opportunities and stay on the owning team's backlog (Tier 3).

---

## 4. AdminState Decomposition

This is the centerpiece (Tier-1 #1.1). Goal: replace one 200–228-line dataclass + a free-form `extras` dict with **typed, per-area state slices**, each owned by the team that owns the routes that use it.

### 4.1 Target shape

Define an `AdminState` that is a *composition of typed slices* (dataclasses), one slice per route family, living in `gateway-core` (§3.1):

- `AuthAdminState` — credentials, `admin_write_lock`, session store, login-failure store, `must_change_password`.
- `ChannelsAdminState` — channel config/status handles.
- `SessionsAdminState` — replay store handle (`SqliteSessionStore`).
- `TenantsAdminState` — tenant pool / AdminDb references.
- `ConfigAdminState` — `config_swap_fn`, `config_watcher`, config snapshot handles (+ config_mutation layer §3.2).
- `MarketplaceAdminState` — `clawhub_client`, `skill_install_store`, `mcp_market_state`, `skill_registry_factory`.
- `SchedulerAdminState` — `scheduler_runtime_jobs`, `scheduler_history`, `BUILTIN_ACTIONS` wiring.
- `EvolutionAdminState` — evolution store/observer handles.
- `SystemAdminState` — `UpdateChecker`, `SystemAuditLog`, upgrader, subagent dispatcher handles.

`AdminState` becomes a small frozen container of these slices (`state.auth`, `state.config`, `state.marketplace`, …). The `extras: dict` field is **deleted**.

### 4.2 Mechanism: slot registry + validator

To kill the "competing string-key writers / silent test failures" failure mode:

1. Each slice declares its required handles as typed fields — no more `extras["scheduler_runtime_jobs"]`. Type safety + IDE completion + a *named owner* per field.
2. `set_admin_state(...)` runs a **boot-time validator**: every slice a mounted router requires must be populated, else boot fails fast with a clear error. Replaces today's "discover the missing wiring at request time / in a flaky test."
3. During migration, `extras` is kept as a **deprecated, read-through shim** that logs on access and proxies to the typed slice, so in-flight branches that still poke `extras[...]` keep working until they're cut over (see §5 sequencing).

### 4.3 Why this removes the merge magnet

Today a new subsystem edits the *one* `AdminState` dataclass (conflict with everyone). After decomposition, a team adds a field to *its own slice file*, owned by *its CODEOWNERS line* — parallel work no longer collides on a single 228-line class, and the boot wiring lives in *that area's* builder, not in `entrypoint.py`.

---

## 5. Migration Sequence

Each phase is **one small, reviewable, independently-shippable PR** with a **backward-compat re-export shim** at the old path. Phases are ordered to minimize conflict with **PR #53 (feat/marketplace, +9.5k, touches `entrypoint.py` / middleware / channels)** and the **config-coverage initiative**.

> Sequencing principle: do the *additive, non-`entrypoint`* extractions first (parallel-safe with PR #53), land `entrypoint`-touching phases only in the window after PR #53 merges, and do config-mutation early so the config-coverage initiative can build coverage against the *new* single seam instead of 4 copies.

### Phase 0 — Add the import-linter contract skeleton (no code moves)
Add the new contracts (§6) in **disabled/report-only** mode where the boundary doesn't yet exist, enabled where it already holds. Establishes the ratchet. *Conflict surface: none.*

### Phase 1 — `admin-config` config-mutation layer (Tier-1 #1.3)
Extract `config_mutation.py`; the 5 config modules import it; leave thin re-export shims in each original module.
**Coordination point — config-coverage initiative:** land this *before or jointly with* the coverage push so new tests target the single seam. *Conflict with PR #53: low.*

### Phase 2 — Provider & system internal de-duplication (Tier-3 #3.2, #3.6)
`system`: introduce `AsyncJsonPersistentStore<T>`, `ExtensionStore<T>`, `HardenedTarballInstaller` bases. `providers`: group schema modules + add `adapters → specs` layering. All package-internal, single-owner, **zero `entrypoint.py` / `AdminState` contact** → fully parallel with PR #53. *Conflict: none.*

### Phase 3 — Typed AdminState slices, additive (Tier-1 #1.1, part A)
Introduce the typed slice dataclasses (§4.1) **alongside** the existing `extras` dict; `extras` becomes a logging read-through shim. No router is forced to migrate yet.
**Coordination point — PR #53:** the read-through shim guarantees PR #53 (which reads `extras`) keeps working; notify `@corlinman/gateway-lead` to rebase onto the shim. *Conflict: low (additive).*

### Phase 4 — `persona-finalization` facade (Tier-2 #2.4)
Move finalization into admin-a, add the facade, delete the reverse import, enable the §3.7 forbidden-import contract. Small, self-contained, removes one documented cycle. *Conflict with PR #53: none.*

### Phase 5 — `admin-marketplace` bundle (Tier-2 #2.1, part A)
Move `skills/plugins/*market*` into `routes_admin_b/marketplace/`; cut over to `MarketplaceAdminState`; leave re-export shims at the old module paths.
**Coordination point — PR #53 (the big one):** PR #53 is `feat/marketplace` and *will* collide here. **Land Phase 5 only after PR #53 merges.** This is the single most important sequencing constraint — call it out in both trackers.

### Phase 6 — `admin-infrastructure` bundle (Tier-2 #2.1, part B)
Move `scheduler/evolution/system/subagents/...` into `routes_admin_b/infra/`, cut over to the typed slices, split big handlers into core/mutations internally. Re-export shims at old paths. *Conflict with PR #53: none after Phase 5.*

### Phase 7 — admin-a god-file splits (Tier-2 #2.3)
Split `auth.py` into `auth_session` + `auth_credentials` so `password_reset` imports only credentials (kills the `admin_write_lock` write race); extract `_tenant_resolver`; move per-protocol channel factories under `channels/{protocol}.py`; extract `_replay_engine`. Each its own small PR with shims. *Conflict: none.*

### Phase 8 — `entrypoint.py` decomposition + middleware bootstrap (Tier-1 #1.2)
**Land only after PR #53 has merged** (PR #53 touches `entrypoint.py`/middleware). Then extract `middleware/bootstrap.py::install(app, state)`; split `entrypoint.py` into `boot_phases` (ConfigResolution / StateBuilder / RouteMount / LifespanHooks), `config_resolver`, `c2_wiring`, `admin_state_builder`, `config_hot_reload`, `scheduler_integration`. Each extraction is a separate PR; `lifecycle/__init__` keeps re-exporting `build_app`. Target: `entrypoint.py` < 2000 LOC.
**Coordination point — PR #53:** highest-conflict phase; gate behind PR #53 merge + fresh rebase, reviewed by `@corlinman/gateway-lead`.

### Phase 9 — Retire the `extras` shim (Tier-1 #1.1, part B)
Once all routers consume typed slices (Phases 3, 5, 6) and PR #53 is cut over, delete the `extras` read-through shim and enable the boot-time slot validator. Flip the relevant import-linter contracts from report-only to enforcing. *Conflict: none.*

### Phase 10 — `chat-pipeline` boundary hardening (Tier-3 #3.5) + remaining Tier-3
Formalize the `gateway_api`/`services` layering and no-cycles contract; runtime-services internal splits and remaining foundation god-module splits proceed on each owner's schedule as small same-package PRs. *Conflict: none.*

**Net ordering vs. in-flight work:** Phases 1–4, 7 run *in parallel* with PR #53. Phases 5, 6, 8, 9 are *gated behind* PR #53's merge. Config-coverage couples only to Phase 1.

---

## 6. CI Enforcement

The boundary-check job already runs import-linter ("no cycles"). Add these contracts (each maps to a §3 boundary). Roll out **report-only → enforcing** as each phase lands.

| Contract (import-linter) | Type | Rule | Phase enabled |
|---|---|---|---|
| **gateway-core layering** | `layers` | `gateway.core` / `gateway.middleware` below `routes*` / `services` / `channels_runtime`; core must not import upward | 3, 8 |
| **middleware independence** | `independence` | the `middleware.*` install modules don't import each other | 8 |
| **admin-config leaf** | `forbidden` | `config_mutation` imports no `routes_admin_*`; only designated config modules import it | 1 |
| **admin bundle independence** | `independence` | `routes_admin_b.marketplace` ⟂ `…infra` ⟂ `…config` | 5, 6 |
| **no admin-a ↔ admin-b cycle** | `forbidden` | `routes_admin_b.*` may import `routes_admin_a` only via the persona facade; admin-a imports no admin-b | 4 |
| **chat-pipeline layering** | `layers` + no-cycles | `gateway_api` leaf; `services` above it; no `routes.chat ↔ gateway_api ↔ services` cycle | 10 |
| **provider adapters→specs** | `layers` | adapters may import specs; specs must not import adapters | 2 |
| **provider→plugins fence** | `forbidden` | non-plugin `corlinman_providers` reaches `plugins` only via its entry-point | 2 |
| **AdminState slot validator** | runtime check (boot) | not import-linter — `set_admin_state` fails boot on missing/undeclared slot | 9 |

**Per-package test isolation.** The maps repeatedly flag *test-fixture cascades*: every admin sub-router builds an `AdminState` with 10+ optional fields, and a schema change in evolution-store forces all 4 evolution suites to re-run serially. Mitigations to bake into CI: one shared `AdminState` slice-factory fixture; keep each extracted bundle's tests in its own subpackage so they run independently; add isolation so a schema bump runs only the affected package's suite.

---

## 7. Ownership Model

Refines the owner-areas in `architecture-modules.md` CODEOWNERS. Placeholder handles use the `@corlinman/<area>-owners` / existing team-handle convention.

| New module (§3) | Path(s) | Owner |
|---|---|---|
| `gateway-core` (AppState + AdminState types + middleware bootstrap) | `gateway/core/`, `gateway/middleware/`, `gateway/lifecycle/` | `@corlinman/gateway-lead` |
| `admin-config` (config_mutation) | `gateway/core/config_mutation.py` + admin-b config modules | `@corlinman/admin-backend-team` (config sub-owner) |
| `admin-marketplace` | `gateway/routes_admin_b/marketplace/` | `@corlinman/admin-backend-team` (marketplace sub-owner) |
| `admin-infrastructure` | `gateway/routes_admin_b/infra/` | `@corlinman/admin-backend-team` (infra sub-owner) |
| admin-a god-file splits + persona facade | `gateway/routes_admin_a/` | `@corlinman/admin-control-plane-team` |
| `chat-pipeline` | `gateway_api/`, `gateway/services/` | `@corlinman/runtime-orchestration-team` |
| runtime-services internals | `gateway/{channels_runtime,grpc,providers,placeholder,mcp,observability}/` | `@corlinman/runtime-orchestration-team` |
| `provider-specs` / `provider-adapters` | `corlinman-providers/.../{specs,registry,declarative,capabilities}.py` vs adapters | `@corlinman/provider-adapters-team` |
| plugin-platform fence | `corlinman-providers/.../plugins/` | `@corlinman/plugin-platform-team` |
| system shared-base | `corlinman-server/.../system/` | `@corlinman/system-integration-team` |
| scheduler/tenancy/persona/profiles/cli | `corlinman-server/.../{scheduler,tenancy,persona,profiles,cli}/` | `@corlinman/platform-services-team` |
| channels package internals | `corlinman-channels/` | `@corlinman/channels-gateway-team` |

**Seam ownership (contract-backed):** `@corlinman/gateway-lead` owns the `AdminState` *container* contract and the boot/middleware bootstrap; each admin sub-team owns *its slice*. New slice fields are reviewed by the slice's CODEOWNERS, not by everyone touching one dataclass — which is the entire point of §4.

---

## 8. Risks & Mitigations

| Risk | Grounding | Mitigation |
|---|---|---|
| **Hidden import cycles surface during extraction.** | Documented cycles: `entrypoint → routes_admin_b → oauth → credentials → entrypoint`; `admin-a.auth → state → _auth_shim`; `admin-b.personas ↔ admin-a.personas`; channels `service ↔ adapters`. | Each phase adds its no-cycles / forbidden contract in **report-only first** (Phase 0). Persona facade (Phase 4) and admin-config leaf (Phase 1) explicitly cut two named cycles. |
| **Test-fixture cascades** — a slice/field change ripples across 5+ admin test files; an evolution-store schema bump re-runs 4 suites serially. | admin-a/admin-b/evolution maps. | Shared `AdminState` slice-factory fixture; per-bundle test isolation; typed slices mean a field add touches one fixture. |
| **`extras` consumers break mid-migration.** | 27 modules read `extras[...]` by convention. | Phase 3 keeps `extras` as a logging read-through shim; deleted only in Phase 9 after all consumers migrate. |
| **Deploy / compat regressions.** | Boot wiring spread across 3 sites; `lifecycle/__init__` re-exports `build_app`. | Re-export shims at every old import path; `build_app` stays exported; no wire-protocol/config-format change; the boot-time slot validator (Phase 9) converts silent mis-wiring into a loud pre-traffic failure. |
| **Collision with PR #53 (`feat/marketplace`, entrypoint/middleware/channels).** | Stated in-flight constraint. | Sequencing (§5): Phases 1–4, 7 are PR-#53-safe; Phases **5, 6, 8, 9 gated behind PR #53 merge** and rebased after. |
| **Collision with config-coverage initiative.** | Stated in-flight constraint. | Phase 1 (`config_mutation`) lands *with/before* the coverage push so coverage targets one seam, not 4 copies. |
| **Scheduler ↔ gateway back-import** (`qzone_daily` imports `routes_admin_b.scheduler`). | platform map. | `admin-infrastructure` (Phase 6) passes scheduler job specs *into* `corlinman_server.scheduler` via explicit wiring; forbidden-import contract prevents new back-imports. |

---

## 9. Success Metrics

| Metric | Baseline | Target | Verified by |
|---|---|---|---|
| `entrypoint.py` LOC | 4040 | < 2000 | Phase 8; line count |
| `routes_admin_b` shape | 16.9K in one bundle | 3 independent bundles (config / marketplace / infra), each `independence`-fenced | Phases 5–6; import-linter |
| `AdminState.extras` keys | 27+ untyped string keys | 0 (typed slices + boot validator) | Phase 9 |
| `AdminState` merge load | 6+ contributors/month on one dataclass | field changes localized to per-area slice files | git-blame churn |
| Config-write definitions | `_write_config_atomic` × 4 copies | 1 (`config_mutation`) | Phase 1; grep = 1 def |
| Cross-bundle cycles | 3 documented chains | 0 (enforced) | import-linter contracts |
| Monolith gateway LOC | 56K | < 40K via extraction | cumulative |
| Behavior preserved | — | full test suite green at **every** phase; re-export shims keep old paths importable | per-PR CI |
| In-flight work unbroken | — | PR #53 and config-coverage merge without rework | coordination sign-off at Phases 1, 3, 5, 8 |
