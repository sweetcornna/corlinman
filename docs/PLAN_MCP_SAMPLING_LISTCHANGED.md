# PLAN — Dim 5 remainder: MCP sampling + tools/list_changed + dynamic advertisement

Date: 2026-07-03. Branch: `feat/mcp-sampling-listchanged` off `main@782cc00e`.
Decision source: `audit/ABSORB_MATRIX_2026-07-02.md:49,73-79` (Dim 5,
ADAPT-ADOPT, value=H cost=M) + `docs/parity-matrix-2026-06-11.json:205-221`
(MCP cluster) + issue #108 (MCP hot-plug schema refresh). Research basis:
two-agent sweep 2026-07-03 (client/advertise mapping + target contract).

## 0. Verified current state (what we build on)

- **Bespoke JSON-RPC client** — NOT the `mcp` SDK. Two hand-rolled reader
  loops: stdio `client.py:_reader_loop:327-368`, ws
  `client_ws.py:198-219`. Both demux by matching response `id` against
  `self._pending` and **drop every unmatched frame** (`"dropped
  unmatched response"`). No notification callback, no server-initiated
  request handling — the client is request/response only.
- **Empty client caps**: `client_manager.py:544` sends literal
  `"capabilities": {}`. A `ClientCapabilities` model with a `sampling`
  field already exists (`types.py:236-247`) but is used only server-side.
- **Advertisement is a boot snapshot**: `_wire_mcp_tool_plane`
  (`entrypoint.py:184`) runs once at `:1094`, writes
  `state.extras["mcp_tools_json"]` at `:224`; consumed once per ChatStart
  at `grpc_backend.py:240-254`. `register_mcp_tools` (`advertise.py:243`)
  upserts synthesized `mcp`-kind entries (namespaced `{server}_{tool}`,
  v1.22.3) but has **no unpublish** for removed servers.
- **Admin hot-plug** (`mcp_adapter.py:68-259`: enable/disable/restart/
  install/remove/reconfigure) mutates the live `McpClientManager` but
  **never re-advertises** — this IS issue #108. `_wire_mcp_tool_plane`
  has exactly one caller (boot).
- Only the **grpc** chat backend advertises MCP tools; the direct backend
  does not (pre-existing, out of scope).
- Server-side `list_changed` emit already exists (`dispatch.py:212`) — the
  gap is purely the client-side listener.

## 1. Deliverable (the recorded slice, nothing more)

Per ABSORB_MATRIX line 49: **sampling responder + tools/list_changed
client listener + dynamic (non-boot-snapshot) advertisement.** Plus the
issue #108 unification (same refresh entrypoint).

Out of scope (separate Dim 5 gaps, do NOT build now): `/mcp` console
command; `.mcp.json` 4-scope config + precedence; client `resources/`
(list/read); `--mcp-config`/`--strict-mcp-config`; MCP-provided
skills/prompts; direct-backend MCP advertisement.

## 2. The server→client inbound frame router (shared prerequisite)

Both features need the client to handle server-initiated frames. Add a
classify step to BOTH reader loops, before the id-demux:

- frame has `method` + non-null `id` → **server request** → await
  `on_server_request(method, params, id)` → enqueue the returned JSON-RPC
  response (result or error) onto the existing `_tx_queue`.
- frame has `method` + no `id` → **notification** → schedule
  `on_notification(method, params)` fire-and-forget (never blocks the
  reader).
- else (has `id`, no `method`) → **response** → existing `_pending`
  demux, unchanged.

**Design.** Add two optional async callbacks to `McpClient` and
`McpWebSocketClient`, defaulting to `None` (so nothing changes when
unset): `on_server_request` and `on_notification`. A `None`
`on_server_request` replies with JSON-RPC error `-32601 method not found`
(spec-correct for an unsupported server request) so a server never hangs.
Both peers gain a private `_reply(id, result=None, error=None)` that
enqueues onto `_tx_queue` (reuse the writer path). The two reader loops
share the classification via a small free function
`classify_inbound(parsed) -> Literal["request","notification","response"]`
in `types.py` so stdio and ws never diverge.

`McpClientManager` sets both callbacks per peer at connect time
(`_connect_stdio` / ws connect), bound to manager methods that know which
`McpManagedServer` the peer is (`_handle_server_request(server_name, ...)`
/ `_handle_notification(server_name, ...)`).

**Files.** `client.py`, `client_ws.py`, `types.py`, `client_manager.py`.

## 3. Sampling responder (`sampling/createMessage`)

**Config** (`[mcp.sampling]`):
- `mode` = `"off"` (default) | `"auto"` | `"ask"`. **Secure by default**:
  `off` = never advertise the `sampling` capability, and reject any
  `sampling/createMessage` with JSON-RPC error. `auto` = allow within
  whitelist + rate limit. `ask` = route through the existing
  `ApprovalGate` — which today fail-closes every `ask` to deny (Dim 3 has
  no console resolver yet), so `ask` == deny until Dim 3 lands (documented;
  no cross-dependency taken on).
- `allowed_models` = list of model aliases the responder may run. Empty =
  none (so `auto` with an empty list still can't run — explicit opt-in).
- `rate_limit_per_min` = int (default 10), a per-server token bucket.
  Breach → JSON-RPC error `-32000 rate_limited` (reject, don't queue).
- `max_tokens_cap` = int (default 2048) clamping the server's requested
  `maxTokens`.

**Capability advertisement.** `_handshake` sends
`ClientCapabilities(sampling={}).model_dump()` ONLY when `mode != "off"`
AND a sampling completer is wired; else the current `{}`. So a server is
told sampling exists only when corlinman can actually service it.

**Dispatch.** `_handle_server_request` routes `sampling/createMessage` to
a `SamplingResponder` (new `sampling.py`):
1. mode gate (`off` → error; `ask` → ApprovalGate, today deny).
2. rate-limit (per-server bucket).
3. resolve the model: map `modelPreferences.hints[].name` against
   `allowed_models` (first hint that's whitelisted wins); no whitelisted
   match → JSON-RPC error `sampling_model_not_allowed` (reject, never
   silently substitute).
4. clamp `maxTokens` to `max_tokens_cap`; translate MCP `messages`
   (text/image content) → the provider's chat message shape.
5. call an injected `sampling_completer(SamplingRequest) ->
   SamplingResult` (async). Unwired → error `sampling_unavailable`.
6. shape the MCP result: `{role:"assistant", content:{type:"text",text},
   model, stopReason}`.

The completer is injected into `McpClientManager` from the gateway
(wraps the provider resolver + a small-fast-model default). Package-level
`corlinman-mcp-server` stays provider-agnostic — the completer is a
callable, mirroring the Dim 9 evaluator-injection pattern.

**Files.** new `sampling.py` (types + responder + token bucket) in
`corlinman-mcp-server`; `client_manager.py` (dispatch + config parse +
conditional cap); gateway wiring (§5).

## 4. tools/list_changed listener + unified refresh

**Listener.** `_handle_notification` recognizes
`notifications/tools/list_changed` (constant already at `types.py:34`) →
**debounced** per-server: coalesce bursts within a window
(`[mcp].list_changed_debounce_ms`, default 1500) → re-run `_list_tools`
for that server (`client_manager.py:557`) to refresh
`McpManagedServer.tools` → invoke a manager-level `on_tools_changed()`
callback (new nullable field). Debounce via a per-server asyncio task that
sleeps the window then fires once; a new notification during the window
resets it.

**Unified refresh entrypoint.** New
`refresh_mcp_advertisement(state) -> None` in a gateway module:
1. re-run the `_wire_mcp_tool_plane` body (recompute
   `state.extras["mcp_tools_json"]` + re-`register_mcp_tools`);
2. **prune** synthesized `mcp`-kind entries for servers no longer ready
   (fixes the no-unpublish gap — `registry.remove(name)` exists at
   `registry.py:171`);
3. call `chat_refresh_fn` (`app_factory.py:373`) so the live
   `ChatService` picks up the new `mcp_tools_json` (it re-reads
   `state.extras`). Bounded staleness: `grpc_backend.py:241` reads the
   snapshot per ChatStart, so no mid-turn swap is needed.

Refactor `_wire_mcp_tool_plane` so its body is a callable
`refresh_mcp_advertisement` can share (boot calls it once; the listener
and adapter call it on change). `on_tools_changed` is set at boot to
`lambda: refresh_mcp_advertisement(state)`.

**Close issue #108.** The `McpAdapter` mutators
(`enable_one`/`disable_one`/`restart_one`/`install`/`remove`/
`reconfigure`) call the same `refresh_mcp_advertisement` after mutating
the manager. `McpAdapter` gains a nullable `on_changed` callback set at
construction (`entrypoint.py:1070`) — no new `state` handle inside the
adapter.

**Files.** `client_manager.py` (listener + debounce + `on_tools_changed`);
new/edited gateway module for `refresh_mcp_advertisement` +
`_wire_mcp_tool_plane` refactor (`entrypoint.py`); `mcp_adapter.py`
(`on_changed`); `app_factory.py`/`entrypoint.py` wiring.

## 5. Gateway wiring

**FINAL STATE AS BUILT:** the sampling responder, `on_tools_changed`
listener→refresh, `McpAdapter.on_changed`→refresh (closes #108), and
`[mcp.sampling]`/`list_changed_debounce_ms` config parsing all shipped.
The `sampling_completer` (which runs the actual LLM completion) is the
one deferred piece — no provider resolver is stashed on `state`, so
wiring a completer needs provider-handle plumbing that is its own slice.
Until injected via `state.extras["mcp_sampling_completer"]`, the
responder never advertises the capability and rejects requests
(secure-off default), so the seam is tested but production-dormant —
mirroring the Dim 9 prompt/agent-evaluator deferral.


- Build `refresh_mcp_advertisement` closure over `state`; set
  `manager.on_tools_changed` and `adapter.on_changed` to it.
- Build the `sampling_completer` from the provider resolver (reuse the
  `_ReloadingProviderResolver` already built for the servicer) + the
  small-fast-model default; inject into `McpClientManager.from_config`
  (new optional param) or via a setter after construction.
- Parse `[mcp.sampling]` + `[mcp].list_changed_debounce_ms` in the config
  layer (defensive, defaults; no pydantic model exists for `[mcp]` — mirror
  the existing dict reads).
- All wiring best-effort + logged; unset completer / config → sampling
  stays `off`, listener still refreshes advertisement.

## 6. Test plan (TDD; new tests first)

`corlinman-mcp-server/tests/`:
- `test_inbound_router.py`: `classify_inbound` (request/notification/
  response); reader loop routes a server request → `on_server_request` →
  reply enqueued; unset handler → `-32601` reply (server never hangs);
  notification → `on_notification` fire-and-forget; response path
  unchanged; both stdio + ws.
- `test_sampling.py`: mode gate (off rejects + no cap advertised; auto
  allows; ask → deny today); rate-limit bucket (breach → `-32000`);
  model whitelist (hint match / no match → error / empty list); maxTokens
  clamp; MCP message translation; unwired completer → `sampling_unavailable`;
  result shape.
- `test_list_changed.py`: notification → debounced single re-list;
  burst coalesced to one refresh; `on_tools_changed` fired; re-list
  updates `McpManagedServer.tools`.
- `test_client_manager.py` additions: `_handshake` sends
  `{"sampling":{}}` only when wired+mode!=off, else `{}`.

`corlinman-server/tests/`:
- `test_advertise_refresh.py`: `refresh_mcp_advertisement` recomputes
  `mcp_tools_json`, prunes entries for a now-absent server, calls
  `chat_refresh_fn`.
- `test_mcp_adapter.py` additions: each mutator invokes `on_changed`.
- gateway wiring smoke: boot sets `on_tools_changed`/`on_changed`;
  sampling completer wired from resolver.

Existing MCP suites stay green untouched (request/response path, server
emit, namespacing, policy) = backwards-compat proof.

## 7. Commit sequence

1. `feat(mcp): server→client inbound frame router (both transports) + classify_inbound`
2. `feat(mcp): sampling/createMessage responder — mode/rate-limit/model-whitelist + conditional cap`
3. `feat(mcp): tools/list_changed listener + debounce + on_tools_changed`
4. `feat(gateway): refresh_mcp_advertisement — dynamic re-advertise, prune, wire listener + adapter (closes #108)`
5. `docs + config.example.toml + CHANGELOG + version bump (v1.26.0)`

Each commit: full `make ci` green locally. Then PR → Codex loop (measured
12-17 min/push; merge on silent convergence >2× that + CI green + all
findings fixed). Risks: (a) the reader-loop change is on the hot receive
path — classification must be O(1) dict lookups, response path untouched
when callbacks are None; (b) debounce tasks must be cancelled on server
close (no leak); (c) sampling is secure-by-default (`off`) so the risky
capability ships dormant; (d) `refresh_mcp_advertisement` must be
idempotent and safe to call concurrently (adapter + listener) — guard with
a per-state lock.
