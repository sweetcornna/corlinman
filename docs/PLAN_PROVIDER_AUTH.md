# PLAN — Provider Auth + Per-Agent Models

**Status:** v1.0 · 2026-05-18 · multi-agent parallel execution

**Scope:** port hermes-agent's full credential and model-selection surface into corlinman so the
user-facing story is "configure one model, everything works", with three credential paths
(OAuth subscription / mainstream API key / custom protocol) and per-agent model binding.

**Hard requirements** (from the user):

1. OAuth login to Codex / Claude / etc. subscription accounts → consume that quota directly.
2. Fill in a mainstream provider's API key (already partially there).
3. Add a custom provider and pick its protocol.
4. Different agents call different API models.
5. Multi-agent parallel execution per `[[project-easy-setup-initiative]]` precedent.

---

## 0. Three-way state matrix

| Capability | hermes | corlinman current | gap |
|------------|--------|-------------------|------|
| API-key entry (per provider, masked, eye-toggle) | `EnvPage.tsx` | `/admin/credentials` + `(admin)/credentials/page.tsx` | **none** |
| Built-in provider catalogue | 27 plugins under `plugins/model-providers/` | `_KIND_TO_CLASS` (anthropic / openai / google / qwen / glm / deepseek / openai_compatible / newapi / mistral / cohere / together / groq / replicate / bedrock / azure / mock) | small — no plugin-self-registration UI |
| Add custom provider + pick protocol via UI | manual `config.yaml::custom_providers[]` editing | none (operator hand-edits TOML) | **medium** — needs UI + endpoint |
| OAuth subscription login (PKCE / device-code) | `OAuthLoginModal.tsx` + `_anthropic_oauth_status` + `~/.hermes/.anthropic_oauth.json` + `~/.claude/.credentials.json` auto-import + token refresh loop | **zero** | **large** |
| Per-agent model binding | profile-scoped, `auxiliary.{vision,web_extract,compression}` blocks | agent yamls have no `model:` field; routing is request-body-driven | **medium** |

---

## 1. Target architecture

### 1.1 Credential resolution chain (Anthropic, illustrative)

```
Request lands → ChatService picks AnthropicProvider instance
                       │
                       ▼
        AnthropicProvider._resolve_credential()
                       │
        ┌──────────────┼────────────────┬─────────────────┐
        ▼              ▼                ▼                 ▼
  oauth_token_file   ANTHROPIC_TOKEN   spec.api_key      ANTHROPIC_API_KEY
  (~/.corlinman/    env (manual OAuth   (TOML            env (legacy)
   .anthropic_      override)           [providers.      
   oauth.json)                           anthropic].api_key)
        │              │                 │                 │
        └────first non-empty wins ───────┴─────────────────┘
                       │
                       ▼
        ┌─ if OAuth token → Authorization: Bearer <token>
        └─ if API key     → x-api-key: <key>
```

Token-file storage layout (mirrors hermes):

```json
{
  "provider": "anthropic",
  "auth_type": "oauth",
  "access_token": "...",
  "refresh_token": "...",
  "expires_at_ms": 1747645123000,
  "scope": "user:inference",
  "obtained_at_ms": 1747641523000
}
```

### 1.2 Custom-provider config target shape

```toml
[providers.my-vllm]
kind = "openai_compatible"           # transport protocol selector
enabled = true
base_url = "https://vllm.internal/v1"
api_key = { env = "VLLM_API_KEY" }   # optional
params = { custom = true }           # UI marker — render in "Custom" group, not "Built-in"
```

### 1.3 Per-agent model binding

Agent card schema extension:

```yaml
# agents/researcher.yaml
name: researcher
description: ...
model: claude-sonnet-4-6      # optional — overrides global default for this agent
provider: anthropic           # optional — pin to a specific provider slot
```

Resolution chain at dispatch time:

```
request.model → agent.model (yaml) → [models].default (config) → "no model configured" error
```

Per-agent binding is read-only from yaml in this wave. A profile-scoped override table
(`agent_model_overrides`) is **deferred** to a later wave to keep the surface minimal.

### 1.4 UI surfaces

| New / extended | Route | What |
|----------------|-------|------|
| New | `/admin/oauth` | OAuth provider tiles: Anthropic / Claude Code / Codex / Gemini / xAI with status badges + Login / Refresh / Disconnect actions |
| New | `/admin/providers` | List custom providers + "Add custom provider" form (slug, base_url, kind selector, api_key field) |
| Extended | `/admin/agents` | New "Model" column with inline dropdown; PATCH to a new agent-model-binding endpoint |

---

## 2. Wave breakdown — 6 background agents

Each agent gets: scope, files, hard contract surface, validation. Dependencies are explicit so
Round 2 can fire only after Round 1's API surfaces have landed.

### Round 1 — backend (parallel, no shared mutating files)

#### W-D1 — Per-agent model binding (backend)

* **Touches**
  - `python/packages/corlinman-agent/src/corlinman_agent/agents/card.py` (add `model: str | None`, `provider: str | None` fields with defaults)
  - `python/packages/corlinman-agent/src/corlinman_agent/agents/registry.py` (parse the new fields)
  - `python/packages/corlinman-agent/tests/test_card.py` or sibling (new)
  - `python/packages/corlinman-server/src/corlinman_server/agent_servicer.py` (when dispatching, fall back to agent.model when request.model is unset; pass agent.provider as a resolver hint)
  - **DOES NOT TOUCH** `corlinman_providers/registry.py`
* **Hard contract**: pure data + resolution shape. Resolution order: `request.model || agent.model || global_default`. Empty agent.model = no change in behaviour.
* **Validation**: unit tests on AgentCard parsing + a small integration test that asserts agent binding wins over global default when request omits model.

#### W-B1 — Custom provider (backend)

* **Touches**
  - `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_b/providers.py` (add CRUD: list / create / update / delete custom providers; reuse existing config-write lock pattern from `credentials.py`)
  - `python/packages/corlinman-providers/src/corlinman_providers/specs.py` (add `list_supported_kinds() -> list[str]` static method or module function)
  - `python/packages/corlinman-server/tests/gateway/routes_admin_b/test_providers_custom.py` (new)
* **Hard contract**: new endpoints
  - `GET  /admin/providers/custom` → `{providers: [{slug, kind, base_url, has_api_key, params}]}`
  - `POST /admin/providers/custom` → 201 `{slug}` with body `{slug, kind, base_url, api_key, params}`
  - `PATCH /admin/providers/custom/{slug}` → 200
  - `DELETE /admin/providers/custom/{slug}` → 204
  - `GET  /admin/providers/kinds` → `{kinds: ["anthropic", "openai", "openai_compatible", ...]}` (drives the protocol dropdown)
* **Validation**: round-trip create → list → delete; slug regex `^[a-z0-9][a-z0-9_-]{0,31}$`; reject built-in slot collisions (anthropic, openai, etc.).

#### W-A1 — OAuth backend (Anthropic PKCE + Claude Code auto-import)

* **Touches** (all new files, no editing of providers/registry beyond a credential-resolution hook in `anthropic_provider.py`)
  - `python/packages/corlinman-server/src/corlinman_server/gateway/oauth/__init__.py` (new)
  - `python/packages/corlinman-server/src/corlinman_server/gateway/oauth/storage.py` (new — `OAuthCredential` dataclass + JSON load/save under `<data_dir>/.oauth/<provider>.json`)
  - `python/packages/corlinman-server/src/corlinman_server/gateway/oauth/anthropic_pkce.py` (new — PKCE pair gen, auth URL build, token exchange via `httpx`, refresh)
  - `python/packages/corlinman-server/src/corlinman_server/gateway/oauth/claude_code_import.py` (new — detect `~/.claude/.credentials.json`, parse into OAuthCredential view)
  - `python/packages/corlinman-server/src/corlinman_server/gateway/oauth/sessions.py` (new — in-memory `_oauth_sessions` dict with 1h TTL for PKCE verifier state)
  - `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_b/oauth.py` (new — router with start/submit/status/disconnect endpoints)
  - `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_b/__init__.py` (register new router; **ONLY** add one import + one `include_router` line)
  - `python/packages/corlinman-providers/src/corlinman_providers/anthropic_provider.py` (extend `__init__` + add `_resolve_credential()` method that checks OAuth file → env → spec.api_key → env fallback; conditionally set Authorization-Bearer vs x-api-key header)
  - `python/packages/corlinman-server/tests/gateway/oauth/` (new test directory)
* **Hard contract**: new endpoints under `/admin/oauth/anthropic/*`
  - `GET  /admin/oauth/status` → `{providers: [{id: "anthropic", source: "pkce|claude-code|env|api-key|none", expires_in_seconds?: int}]}`
  - `POST /admin/oauth/anthropic/start` → `{session_id, auth_url, expires_at_ms}`
  - `POST /admin/oauth/anthropic/submit` → 200 `{ok: true, expires_at_ms}` (body: `{session_id, code, state}`)
  - `POST /admin/oauth/anthropic/refresh` → 200 (manual refresh trigger)
  - `DELETE /admin/oauth/anthropic` → 204 (disconnects, deletes token file)
  - `POST /admin/oauth/claude-code/import` → 200 (one-shot import of `~/.claude/.credentials.json`)
* **Validation**: unit tests on PKCE pair gen + token-exchange (mock httpx); credential-resolution chain test in anthropic provider (oauth file present > env > api-key); claude-code-import test against a synthetic credentials.json fixture.

### Round 2 — frontend + remaining OAuth (parallel, depends on Round 1 contracts)

#### W-D2 — Per-agent model UI

* **Touches**
  - `ui/app/(admin)/agents/page.tsx` (new "Model" column; inline `<select>` populated from existing model list endpoint)
  - `ui/lib/api.ts` (new `getAgentModelBinding` / `setAgentModelBinding`)
  - `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_a/agents.py` or `routes_admin_b/` — small endpoint to expose the parsed agent binding so the dropdown can render current value
* **Depends on**: W-D1 (the AgentCard schema)

#### W-B2 — Custom provider UI

* **Touches**
  - `ui/app/(admin)/providers/page.tsx` (new) — list custom providers + "Add custom" modal
  - `ui/components/providers/add-custom-modal.tsx` (new) — form: slug (regex live-validated) + kind dropdown (loaded from `GET /admin/providers/kinds`) + base_url + api_key + params kvs
  - `ui/lib/api.ts` (new `listCustomProviders` / `createCustomProvider` / `deleteCustomProvider`)
* **Depends on**: W-B1's API

#### W-A2 — OAuth modal UI (Anthropic + Claude Code)

* **Touches**
  - `ui/components/admin/oauth-login-modal.tsx` (new) — phase-state machine: idle → opening-browser → awaiting-code → exchanging → done
  - `ui/app/(admin)/credentials/page.tsx` (extend) — new "OAuth" tab with provider tiles + status badges
  - `ui/lib/api.ts` (new `startOAuthLogin` / `submitOAuthCode` / `getOAuthStatus` / `disconnectOAuth` / `importClaudeCode`)
* **Depends on**: W-A1's API

#### W-A3 — OAuth backend (Codex + Gemini + xAI + others) — *deferred to round 3 or split off*

This is included for completeness but is not in the first execution cycle because of scope.
After Round 2 lands and is verified, we evaluate Round 3 with the user — Codex external CLI
detection is the most useful single addition, the others (Gemini / xAI / Nous device-code) can
ship one at a time.

---

## 3. Conflict-avoidance protocol

Memory `[[agent-worktree-caveats]]` notes worktree isolation doesn't actually isolate. To keep
6 agents from clobbering each other:

| File | Touched by | Resolution |
|------|-----------|-----------|
| `corlinman_providers/registry.py` | nobody (D1 routes through agent_servicer instead; B1 routes through specs.py) | clean |
| `routes_admin_b/__init__.py` | A1 only (B1 + D1 don't add new routers) | clean |
| `ui/lib/api.ts` | D2, B2, A2 | each agent appends to the END of the file with a unique marker block comment; review will merge |
| `anthropic_provider.py` | A1 only | clean |
| `agents/card.py` | D1 only | clean |
| `providers.py` route | B1 only | clean |

Round 1 agents must NEVER edit Round 2's files. Round 2 agents must NEVER edit Round 1's files.

---

## 4. Validation harness (run after every round)

```
uv run pytest python/packages/corlinman-server/tests/ -q
uv run pytest python/packages/corlinman-agent/tests/ -q
uv run pytest python/packages/corlinman-providers/tests/ -q
```

After Round 1: assert the 3 new contracts return shaped JSON (curl smoke).
After Round 2: a Playwright spec adds OAuth modal + custom-provider-add coverage.

---

## 5. Risks

| Risk | Mitigation |
|------|------------|
| Anthropic PKCE flow changes (Anthropic-controlled, undocumented) | mirror hermes's URL constants verbatim; pin to today's behaviour; add a "report broken OAuth" UI button |
| OAuth token file leaked from logs | `OAuthCredential.__repr__` redacts; never log `access_token` field; storage file mode 0o600 |
| Per-agent binding accidentally overrides explicit request model | resolution order is fixed: `request.model` wins. Document in agent yaml comments. |
| Custom provider with invalid `kind` crashes provider build | endpoint validates against `list_supported_kinds()`; bad kinds reject at 422 |
| Two agents pip-install incompatible httpx versions | none added — httpx already pinned in corlinman-server pyproject |

---

## 6. Sequencing

```
NOW          → Round 1 dispatched (D1, B1, A1) in parallel
+1 hour      → Round 1 lands; verify; commit per-wave
+1.5 hours   → Round 2 dispatched (D2, B2, A2) in parallel
+3 hours     → Round 2 lands; verify; commit per-wave
LATER        → user decides on Round 3 (Codex / Gemini / xAI)
```

---

## 7. Out of scope this round

- Bedrock SigV4 auth (declared in `_KIND_TO_CLASS` but raises NotImplementedError today).
- Profile-scoped agent-model override table (yaml-only this wave; UI is read-only for the model column on non-default profiles).
- Auxiliary task bindings (`auxiliary.vision`, etc. — hermes has them, we'll port if/when a user asks).
- Token-refresh background daemon — refresh-on-use is good enough for v1; cron-style refresh can come later.
