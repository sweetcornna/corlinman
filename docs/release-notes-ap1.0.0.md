# corlinman ap1.0.0 — Agent-Port Runtime-Complete

## Headline

Completes the Rust→Python gateway port. The Python gateway no longer
starts in degraded mode: `ProviderRegistry`, `ChatService`,
QQ/Telegram channel runtime, and the gRPC agent backend are all wired
via a clean sibling-seam bootstrap. `/v1/chat/completions` and
`/v1/models` serve real responses; the agent reasoning loop runs a
**real tool executor** instead of the `awaiting_plugin_runtime`
placeholder stub; `corlinman doctor` gained three runtime-wiring health
checks. Full test suite is **2586 passed**.

## Before → After

| Probe | Before (degraded) | After (ap1.0.0) |
| --- | --- | --- |
| `GET /health` | `{"status":"ok","mode":"degraded"}` | `{"status":"ok","mode":"ok"}` |
| `POST /v1/chat/completions` | `501 no ChatService wired` | 200, streaming + non-streaming real completions |
| `GET /v1/models` | `501 no ProviderRegistry wired` | 200, configured model list |
| QQ channel | No `:3001` connection, no channel log | NapCat OneBot WS connected; bot replies |
| Startup log | `gateway.sibling_missing module=...core.config` | Clean; no `sibling_missing` / `degraded` |
| Agent tool calls | `awaiting_plugin_runtime` stub — loop short-circuits | Real plugin dispatch via JSON-RPC 2.0 stdio |

## Architectural note

The Rust implementation (M0–M8, `v0.1.0`) was fully functional and was
deliberately removed. The Python gateway inherited the HTTP/admin
**shell** from the port, but the **runtime** — provider registry, chat
pipeline, channel adapters, agent executor — was never wired. The
gateway's `entrypoint.py` lifespan already contained the **integration
seam**: for each sibling module in a fixed list it lazy-imports and
calls an optional `bootstrap(state)` function. The missing piece was
the contract and the sibling modules themselves.

`ap1.0.0` closes that gap through four execution waves:

- **Wave 0 / P0** — config loader + contract. Lands
  `gateway/core/config.py` (`load_from_path`, `parse_config`,
  `resolve_env_refs`), adds `provider_registry` and `chat` attach
  points to `AppState`, and publishes `docs/contracts/runtime-wiring.md`
  as the shared contract for all subsequent parcels.
- **Wave 1 / P1–P4** — four parallel wiring parcels delivered as a
  single commit: providers bootstrap, direct-provider chat backend,
  QQ/Telegram channel runtime, gRPC agent server.
- **Wave 2 / P5** — real tool executor: `RegistryToolExecutor` /
  `PluginInvoker` replace the `PlaceholderExecutor`; the agent
  reasoning loop now dispatches JSON-RPC 2.0 stdio plugins instead of
  returning the stub acknowledgement.
- **Wave 3 / P12** — test + observability harness: config loader
  coverage, gRPC backend tests, three `corlinman doctor` runtime checks.

The port strictly follows the **sibling-seam** pattern: Wave-1 parcels
add new sibling modules; they do not edit `entrypoint.py` or
`state.py` again. This means the integration seam is open for future
parcels without coordination.

## What's new

### P0 — Config loader (`gateway/core/config.py`)

- `load_from_path(path)` — loads `config.toml`, resolves every
  `{env = "X"}` / `{env = "X", default = "Y"}` reference against
  `os.environ`, returns a plain resolved `dict`. Multi-key tables (e.g.
  scheduler job `env = {KEY="val"}`) pass through untouched.
- `parse_config(text)` — same resolution from a string (config-watcher
  hook).
- `resolve_env_refs(value)` — recursive resolver (exported for callers
  that process sub-trees).
- Raises `FileNotFoundError` / `tomllib.TOMLDecodeError`; the entrypoint
  guards these and falls back to degraded mode rather than crashing.
- `AppState` gains two typed attach points: `provider_registry` and
  `chat` (both `None` by default → degraded 501 envelopes until wired).
- `docs/contracts/runtime-wiring.md` published as the locked contract
  for all Wave-1 parcels.

### Wave 1 — Runtime wiring (P1–P4)

- **P1 — `gateway/providers`**: `bootstrap(state)` builds a
  `ProviderRegistry` from `config["providers"]`;
  `RegistryModelSource` feeds `/v1/models`.
- **P2 — `gateway/services/direct_backend`**: `DirectProviderBackend`
  drives the provider plane directly (no agent hop); `chat_bootstrap`
  composes `ChatService` and attaches it to `AppState.chat`. Fast path
  for single-turn completions without tool use.
- **P3 — `gateway/channels_runtime`**: `bootstrap(state)` reads
  `config["channels"]`, constructs `QqChannelParams` / Telegram params,
  runs `run_qq_channel` / `run_telegram_channel` as background tasks
  returned to the lifespan cancel-set.
- **P4 — `gateway/grpc` + `services/grpc_backend`**: `build_chat_service(state)`
  selects direct vs gRPC-agent backend per `config["models"]["backend"]`;
  the real `AgentServer` replaces `serve_placeholder_in_background`.
- Integration: `services.bootstrap` wires `AppState.chat` then launches
  channels; `routes/chat` and `routes/models` resolve the live runtime
  off `app.state.corlinman`; `entrypoint` computes `/health` `mode`
  from the actual attach points (`ok` when both wired, `degraded`
  otherwise).

### P5 — Real tool executor

Replaces `PlaceholderExecutor` — which acked every `tool_call` frame
with an `awaiting_plugin_runtime` stub and short-circuited the reasoning
loop — with a real executor that dispatches against the plugin plane:

- **`RegistryToolExecutor`** (`corlinman-grpc/agent_client/tool_executor.py`):
  implements the `ToolExecutor` protocol; never raises — every failure
  (no invoker, exception, timeout, tool error) folds into an
  `is_error ToolResult` so the loop keeps draining.
- **`PluginInvoker`** (`gateway/grpc/plugin_invoker.py`):
  `build_registry_invoker` resolves plugins off `PluginRegistry`;
  `invoke_sync_plugin` runs spawn-per-call JSON-RPC 2.0 stdio `sync`
  plugins. `service` / `mcp` plugin types return a clean
  `unsupported_plugin_type` result.
- `grpc_backend.build_tool_executor(state)` wires a
  `RegistryToolExecutor` bound to `AppState.plugin_registry` into
  `build_grpc_chat_service`; degrades to `PlaceholderExecutor` if
  `corlinman-grpc` is missing.

### P12 — Test coverage + doctor health checks

- **`tests/gateway/core/test_config.py`**: covers `load_from_path`,
  `parse_config`, `resolve_env_refs` — valid TOML, `{env=}` ref
  resolution, missing file, malformed TOML, nested sections.
- **`tests/gateway/services/test_grpc_backend.py`**: `chat_backend_mode`
  selection and `resolve_agent_target` precedence (env / config /
  default).
- **`corlinman doctor`** gains three runtime checks:
  - `runtime_config` — config TOML loadable.
  - `provider_registry` — registry buildable, reports spec count.
  - `runtime_wiring` — offline simulation of P1+P2 boot, reports `ok`
    vs `degraded` mirroring `/health`'s `mode`.
  All three degrade gracefully; none crash.
- **`tests/cli/test_doctor_runtime.py`**: unit + CLI integration for
  the three new checks.

### QQ embedded scan-login

- `ScanLoginDialog` drops the broken relay-QR path; embeds the NapCat
  WebUI directly via iframe so scanning the dialog QR is scanning
  NapCat's own QR. Tests trimmed to the iframe-embed contract.
- `deploy/config.toml.template` aligned with the easy-setup schema
  (`username` / `password_hash` / `must_change_password`).
  *(Subsequently removed in the Deploy UX overhaul — `admin_seed.py` now
  writes the `[admin]` block automatically; see `docs/config.example.toml`
  for the canonical reference.)*

## Acceptance gates (§5 of the plan) — all passed

| Gate | Status |
| --- | --- |
| `GET /health` → `mode` not `degraded` | ✅ |
| `GET /v1/models` → 200, configured models listed | ✅ |
| `POST /v1/chat/completions` → 200, stream + non-stream | ✅ |
| QQ bot replies; `/admin/channels/qq/status` online | ✅ |
| `pytest python/packages -q` → 2586 passed | ✅ |
| Startup log free of `sibling_missing` / `degraded` | ✅ |

## What's tested

- +67 tests (P12 wave): config loader, gRPC backend, doctor CLI
  integration.
- +22 tests (P5): tool executor round-trips, plugin invoker, error
  folding.
- Wave-1 parcels each ship their own test suites: 197 provider
  bootstrap tests, 288 direct-backend tests, 310 channels-runtime tests,
  195 gRPC agent-server tests.
- Full `pytest python/packages -q`: **2586 passed**.

## Deferred work (P6–P11, post-ap1.0.0)

Per plan §6, the following parcels are explicitly deferred to a later
iteration:

- **P6** — Evolution apply/rollback (the `501` in `routes_admin_b/evolution.py` → `EvolutionApplier`).
- **P7** — Voice real provider (OpenAI realtime, replacing `MockProvider`).
- **P8** — Bedrock (SigV4) / Azure (deployment routing) providers.
- **P9** — Memory/RAG + episodes integration (`tagmemo`, `about_tag` resolver).
- **P10** — Channel breadth (Discord, Slack, et al., per hermes/openclaw baseline).
- **P11** — `core.config` hot-reload wiring + placeholder engine replacement.

These are tracked in `docs/PLAN_PORT_COMPLETION.md` §3 Wave 2 for
future dispatch.

## Branch / tag

Branch: `feat/port-completion` — single PR, all parcels merged.
Milestone codename: `ap1.0.0` (agent-port runtime-complete).

> `ap1.0.0` is a milestone / release codename, not a PEP 440 package
> version. It is valid in docs and as a git tag; package versions in
> `pyproject.toml` are managed separately.
