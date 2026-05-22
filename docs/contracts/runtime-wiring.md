# Runtime-wiring contract — corlinman Python gateway

> **Status**: locked by Parcel **P0** (2026-05-21). Wave-1 parcels
> (P1–P4) and Wave-2 parcels code against this document. Changing a
> contract here is a coordination event — ping the other parcel owners.
> **Source of truth**: `docs/PLAN_PORT_COMPLETION.md` §2 (this file is
> §2 fixed into a contract).

## 0. Why this exists

The Python gateway's HTTP/admin **shell** is complete, but the
**runtime** (provider registry, chat pipeline, channels) was never
wired — the gateway boots in `degraded` mode. `entrypoint.py`'s
lifespan already has an **integration seam**: it lazy-imports sibling
modules and calls an optional `bootstrap(state)` on each. P0 generalised
that seam and landed the keystone config loader. P1–P4 each add **one
sibling module** that plugs into the seam — they do **not** edit
`entrypoint.py` or `state.py` again. This document is the fixed contract
those modules share.

## 1. The config loader — `gateway.core.config` (P0, done)

```python
from corlinman_server.gateway.core.config import load_from_path

cfg: dict = load_from_path(path)        # path: Path | str
```

- **Returns a plain `dict`.** Every `{ env = "X" }` /
  `{ env = "X", default = "Y" }` reference is resolved against
  `os.environ` (live var → `default` → `None`). A multi-key table such
  as a scheduler job's `env = { KEY = "val" }` is **not** an env-ref and
  passes through untouched.
- Raises `FileNotFoundError` / `tomllib.TOMLDecodeError`; the caller
  (`entrypoint._load_config`) guards file-exists and catches parse
  failures to fall back to degraded mode.
- Helpers also exported: `parse_config(text)` (same resolution, from a
  string — used by the config-watcher parser hook) and
  `resolve_env_refs(value)` (the recursive resolver).

### 1.1 Config-access pattern — read it as a dict

`AppState.config` is the dict returned by `load_from_path`. **Read it
with dict access**, never assume a typed object:

```python
cfg = state.config or {}
providers   = cfg.get("providers") or {}      # {name: {kind, api_key, base_url, enabled, params}}
models_cfg  = cfg.get("models") or {}          # {default, aliases: {alias: {provider, model, params}}}
channels    = cfg.get("channels") or {}        # {qq: {...}, telegram: {...}}
embedding   = cfg.get("embedding") or {}
evolution   = cfg.get("evolution") or {}
```

This keeps parity with every existing reader
(`routes_admin_b.state.config_snapshot` does `isinstance(snap, Mapping)`;
admin routes do `cfg.get(...)`; `config_watcher.diff_sections` takes a
`dict`).

### 1.2 Two config consumers — do not confuse them

| Consumer | Shape | Source |
| --- | --- | --- |
| **Runtime** (P1 registry, P2 chat) | env-refs **resolved** | `AppState.config` (this loader) |
| **Admin API** (`/admin/providers` etc.) | env-refs **raw** `{env="X"}` | `routes_admin_b` `config_loader` — a separate raw-`tomllib` snapshot wired in `entrypoint._mount_routes` |

P1–P4 read `AppState.config` (resolved). They must **not** repoint the
admin `config_loader` — the admin tree intentionally surfaces the
unresolved `{env=...}` shape and never wants the literal secret.

## 2. The sibling `bootstrap(state)` seam

`entrypoint.py`'s lifespan iterates a fixed list of sibling modules and,
for each that exists, calls its optional `bootstrap`:

```python
sibling_names = (
    "corlinman_server.gateway.providers",  # P1 — provider_registry
    "corlinman_server.gateway.services",   # P2/P3 — chat + channels
    "corlinman_server.gateway.evolution",  # evolution observer
)
```

Order is **load-bearing**: `providers` boots before `services` so the
registry attach point is populated when the chat/channel bootstraps read
it.

### 2.1 The `bootstrap` signature

A sibling module **optionally** exports:

```python
def bootstrap(state: AppState) -> None | Awaitable[None] | list[asyncio.Task]:
    """Startup wiring. Called once during the gateway lifespan, after
    the config + evolution-store are open and before the app accepts
    requests.

    - Mutate ``state`` in place to attach runtime handles
      (``state.provider_registry = ...``, ``state.chat = ...``).
    - May be sync or ``async``.
    - May return a list of ``asyncio.Task`` (channel adapters, hot
      reloaders). Returned tasks are registered into the gateway's
      background list and **cancelled + awaited** at shutdown under a
      shared ``cancel`` event — do not manage their lifecycle yourself.
    """
```

Accepted return values: `None`, an awaitable (awaited), a single
`asyncio.Task`, or a `list`/`tuple` of `asyncio.Task`. Anything else is
ignored. A raised exception is caught and logged
(`gateway.sibling.bootstrap_failed`) — the gateway still boots degraded.

### 2.2 Rules for parcel owners

- **Add a sibling module, not an `entrypoint.py` edit.** P1 creates
  `gateway/providers/__init__.py` with a `bootstrap`; P2/P3 add a
  `bootstrap` to `gateway/services/__init__.py`. The seam list above is
  already final — if a parcel genuinely needs a new dotted name, that is
  a contract change (coordinate first).
- **Gate, never crash.** If a dependency is missing, log and leave the
  `AppState` slot `None`; the route returns its typed 501/503 envelope.
- **Long-running work returns Tasks.** Do not spawn detached tasks — the
  gateway must be able to cancel them at shutdown.

## 3. `AppState` runtime fields (`gateway/core/state.py`)

`AppState` is a plain `@dataclass`, every field optional. P0 added two
attach points; the rest already existed:

```python
@dataclass
class AppState:
    config: Any = None                # P0 — dict from load_from_path (env-resolved)
    config_path: Path | None = None
    config_watcher: ConfigWatcher | None = None
    provider_registry: Any = None     # P1 — corlinman_providers.registry.ProviderRegistry
    chat: Any = None                  # P2 — gateway.services.ChatService
    # ... pre-existing fields (plugin_registry, session_store, approval_gate,
    #     admin_db, tenant_pool, log_broadcaster, extras, ...) unchanged ...
```

- `provider_registry` — built by **P1** from `config["providers"]`;
  consumed by `/v1/models` (`routes/models.py` `build_router(source=)`).
- `chat` — a `ChatService` built by **P2**; consumed by
  `/v1/chat/completions` (`routes/chat.py`) and by the **P3** channel
  adapters (`chat_service=state.chat`).
- Both default to `None` ⇒ degraded mode (`/v1/models` → 501
  `no ProviderRegistry wired`; `/v1/chat/completions` → 501
  `no ChatService wired`).

`AppState` has no `__slots__` — dynamic attribute writes are allowed,
but **load-bearing handles get a first-class field** (do not stash them
in `extras`).

## 4. The `ChatBackend` protocol (exists — `gateway/services/chat_service.py`)

P0 does **not** change this; P2 and P4 implement it. It is a
`@runtime_checkable` `Protocol`:

```python
class ChatBackend(Protocol):
    async def start(
        self, start: agent_pb2.ChatStart,
    ) -> tuple[asyncio.Queue[Any], AsyncIterator[agent_pb2.ServerFrame]]:
        ...
```

- `start` opens an in-process pipeline. The returned `(tx, rx)`:
  - `tx` — outbound `asyncio.Queue` of `agent_pb2.ClientFrame`
    (`tool_result` / `cancel`).
  - `rx` — async iterator of `agent_pb2.ServerFrame` (`token` /
    `tool_call` / `done` / `error`).
- **P2** implements `DirectProviderBackend` — calls
  `corlinman-providers` directly, translating provider chunks into
  `ServerFrame`s (fast path, no tools).
- **P4** wires `GrpcAgentChatBackend` (already implemented) onto a real
  gRPC agent server (full path, with tools/memory).
- `ChatService(backend, tool_executor=...)` wraps either backend and is
  what lands on `AppState.chat`. The gateway picks the backend per
  `config["models"]` / deployment mode.

## 5. Boot order recap

```
entrypoint.build_app(config_path, data_dir)
  └─ _load_config()                → gateway.core.config.load_from_path  [P0]
  └─ _build_state()                → AppState(config=cfg, ...)
  └─ _mount_routes()               → admin routers + /v1/* routers
  └─ lifespan:
       ├─ ensure_admin_credentials
       ├─ open evolution.sqlite
       ├─ for sibling in (providers, services, evolution):  [seam — §2]
       │     bootstrap(state)       → attaches provider_registry [P1],
       │                              chat [P2], spawns channels [P3]
       ├─ grpc.serve_placeholder_in_background  (→ P4 swaps for real agent)
       └─ yield  (gateway serves)
       finally: cancel.set(); cancel + await all background tasks
```

## 6. Acceptance signals (the whole port)

1. `GET /health` → `mode` not `degraded`.
2. `GET /v1/models` → 200, configured models listed.
3. `POST /v1/chat/completions` → 200, streaming + non-streaming.
4. QQ bot replies; `/admin/channels/qq/status` online.
5. `pytest python/packages -q` green.
6. Startup log free of `sibling_missing` / `degraded`.
