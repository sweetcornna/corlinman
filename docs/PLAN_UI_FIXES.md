# PLAN — Admin UI Fixes (sessions / logs / providers / models)

**Status:** draft v1.0 · 2026-05-25
**Goal:** make the four "basically unusable" admin surfaces actually usable, with hermes-style polish where it pays off, and ship parity between `main` and the live deployment at `corlinman.cornna.xyz`.

---

## 0. Diagnosis

Three parallel Explore passes + a live probe at `corlinman.cornna.xyz/openapi.json` revealed the real shape of the problem:

### 0.1 The static code audit is misleadingly green

`/admin/sessions`, `/admin/logs`, `/admin/providers`, `/admin/credentials`, `/admin/models` all have:
- a functional page.tsx with skeletons + error states
- the API client functions they need (`fetchSessions`, `upsertProvider`, `fetchModels`, …)
- backend FastAPI handlers serving the expected shapes

So if you read the code with the audit, everything looks fine. The user still calls it "basically unusable."

### 0.2 The live deployment is on a different branch than main

Probed `https://corlinman.cornna.xyz/openapi.json`. The deployed gateway:

| Endpoint | Live | main HEAD |
|---|---|---|
| `/admin/sessions/{key}/events/live` (SSE) | ✗ 404 | ✓ shipped today |
| `/admin/sessions/{key}/turns/{turn_id}/events` | ✗ 404 | ✓ shipped today |
| `/admin/sessions/{key}/cost` | ✗ 404 | ✓ shipped today |
| `/admin/sessions/{key}/replay` | ✓ | ✗ |
| `/admin/providers/{name}/test` | ✓ | ✗ |
| `/admin/providers/{name}/models` | ✓ | ✗ |
| `/admin/providers/kinds` | ✓ | ✗ |
| `/health` shape | `{status,mode}` (legacy) | `{status,version,checks[]}` (new) |

This explains the user's "用不了" complaint:
- The **live UI** (older bundle) renders pages that try to hit endpoints the live backend doesn't fully support yet.
- The **main UI** (after today's observability work) calls new SSE / cost / turn endpoints that the live backend doesn't have.
- The live deployment has features (provider test, replay, provider-scoped model listing) that aren't in main, so when main re-deploys it would *regress* those.

In short: **two siblings diverged**. The user is caught in the gap.

### 0.3 Reference patterns we should adopt

From the hermes + openclaw audits:

**Hermes**:
- `EnvPage.tsx` provider grouping by env-var prefix + paste-only secret input + eye icon reveal (no test button — runtime validates)
- `ModelPickerDialog.tsx` two-column provider→model search; persist as global vs session
- `SessionsPage.tsx` per-row expand + lazy-load messages + FTS highlight + Resume button
- `LogsPage.tsx` four-axis segmented filters, no scroll-lock, manual refresh

**openclaw**:
- JSON-Schema-driven config form (`config-form.node.ts`) — text/select/toggle/number auto-generated; sensitive fields detected by path regex; reveal-to-edit
- Sessions spreadsheet with compaction checkpoints (`sessions.ts`) — inline edit, bulk delete, branch/restore
- `logs.tail` cursor pagination with byte limit (not unbounded ring buffer)
- `models.list` catalog with per-session override resolution chain

---

## 1. Target state

Fix the three concrete sources of breakage, plus apply UX polish where the audits show meaningful wins:

1. **Backport the live-only endpoints into main** so a deploy doesn't regress functionality
2. **Add the new endpoints' equivalents** (or graceful degradation) so the new UI works on the older backend too
3. **Adopt hermes EnvPage paste-only + ModelPickerDialog patterns** to materially upgrade credentials + model picker UX
4. **Add a missing-piece**: `GET /admin/sessions/{key}/turns` listing endpoint so turn drill-down has a way in beyond deep links
5. **Add e2e smoke** that catches "UI calls endpoint that doesn't exist" before deploy

---

## 2. Tasks (3 waves, 7 background agents)

### Wave 1 — Backend gap-fills (3 parallel)

#### W1.1 Backport `provider test` + `provider/{name}/models` + `provider/kinds`

- **Owner:** Backend Architect
- **Files:**
  - `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_b/providers.py` — add three handlers:
    - `POST /admin/providers/{name}/test` — issue a 1-token "ping" via the configured provider, return `{ok, latency_ms, error?}`. Use a minimal `/v1/chat/completions` call to the provider's base_url + a known cheap model. Don't burn user tokens on real models — if the provider exposes a "models" endpoint, hit that instead.
    - `GET /admin/providers/{name}/models` — proxy to the provider's `/v1/models` if OpenAI-compatible; or return a hardcoded list from `corlinman_providers.specs` if the provider has a known catalog (Anthropic, Google).
    - `GET /admin/providers/kinds` — return the kinds descriptor list from `corlinman_providers.registry.list_supported_kinds()` + each kind's `params_schema()`.
  - Add at least 2 tests per new endpoint.
- **Hermes pattern:** "no test button" — but openclaw's audit confirms a test endpoint exists, and corlinman live already has one. Backporting closes the gap.
- **Validation:** mirror the live shape (probe live with `curl /admin/providers/openai/test` while logged in, copy the response shape).
- **ETA:** 4h

#### W1.2 `GET /admin/sessions/{key}/turns` listing endpoint

- **Owner:** Backend Architect
- **Files:**
  - `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_b/sessions_events.py` (or sibling new file) — add:
    - `GET /admin/sessions/{key}/turns?limit=50&before_id=...` — returns `{turns: [{turn_id, started_at_ms, ended_at_ms, status, model, tool_call_count, finish_reason, user_text_preview}], next_cursor}`. Pulls from existing `turns` SQLite table.
  - Tests cover empty session, paginated session, completed + in-progress turns.
- **Deps:** none (turns table exists)
- **Validation:** UI's TODO at `ui/app/(admin)/sessions/[key]/page.tsx:14` lights up.
- **ETA:** 3h

#### W1.3 Add session replay endpoint back

- **Owner:** Minimal Change Engineer
- **Files:**
  - `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_a/sessions.py` — add `POST /admin/sessions/{session_key}/replay`. Body: `{mode: "transcript" | "rerun", since_turn_id?}`. Returns the new session_key for a rerun, or the transcript JSON for transcript mode. The live deployment has this — copy its surface.
  - Test: rerun a 1-turn session, assert new session key returned.
- **Validation:** ReplayDialog component at `ui/components/sessions/replay-dialog.tsx` actually has a target endpoint to POST to.
- **ETA:** 4h

### Wave 2 — Frontend polish (3 parallel, no backend deps after W1)

#### W2.1 Hermes-style credentials page (paste-only + eye icon)

- **Owner:** Frontend Developer + UI Designer
- **Files:**
  - `ui/components/credentials/env-var-row.tsx` (new) — copy the `EnvVarRow` shape from hermes (compact / expanded modes, eye-icon reveal via API, replace/clear buttons). Replace the current per-field render in `ui/app/(admin)/credentials/page.tsx` with this component.
  - `ui/components/credentials/provider-group-card.tsx` (new) — copy hermes `ProviderGroupCard` grouping pattern.
  - `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_b/credentials.py` — add `GET /admin/credentials/{provider}/{key}/reveal` if not present (returns the actual cleartext value for the eye-icon path; auth-gated).
- **Hermes pattern:** `EnvPage.tsx:99-330` row + `EnvPage.tsx:46-67` grouping
- **ETA:** 5h

#### W2.2 ModelPickerDialog two-column search

- **Owner:** Frontend Developer
- **Files:**
  - `ui/components/models/model-picker-dialog.tsx` (new) — copy hermes' two-column provider/model picker with search. Use it on:
    - `/admin/models` "Add alias" flow (pick provider + model instead of two separate selects)
    - `/admin/agents/[name]` "Bind model" flow (per-agent model override)
  - `ui/lib/api.ts` — add `getProviderModels(name)` to call the new W1.1 endpoint.
- **Hermes pattern:** `ModelPickerDialog.tsx:242-269`
- **Validation:** picker opens, search filters both columns, double-click confirms.
- **ETA:** 6h

#### W2.3 Past-turns navigator + provider test button

- **Owner:** Frontend Developer
- **Files:**
  - `ui/app/(admin)/sessions/[key]/page.tsx` — wire the TODO past-turns pill row. Calls W1.2 endpoint, renders ≤ 10 pills with `(turn_id, status, elapsed)` linking to drill-down page.
  - `ui/app/(admin)/providers/page.tsx` — add "Test connection" button per row. On click → POST `/admin/providers/{name}/test`, show toast with latency / error. Use the W1.1 endpoint.
  - `ui/lib/api.ts` — add `listSessionTurns(key, opts)`, `testProvider(name)`.
- **ETA:** 4h

### Wave 3 — Smoke + deploy parity (2 parallel)

#### W3.1 E2E smoke test: every admin page lights up

- **Owner:** API Tester
- **Files:**
  - `ui/tests/e2e/admin-pages-smoke.spec.ts` (new) — for each of `/admin/sessions`, `/admin/logs`, `/admin/providers`, `/admin/credentials`, `/admin/models`, `/admin/agents`:
    1. Navigate to the page
    2. Assert no console errors / no 404s on XHR
    3. Assert at least one expected UI element renders (table row, card, header)
    4. For sessions: click first row → assert detail page loads + EventTimeline appears
    5. For providers: click "Test connection" → assert toast shows
- **Validation:** runs in CI on every PR; catches "endpoint missing" regressions before deploy.
- **ETA:** 5h

#### W3.2 Deployment upgrade note + CHANGELOG

- **Owner:** Technical Writer
- **Files:**
  - `docs/observability.md` — add a "Deploying these changes" section noting:
    - The new SSE / cost / turn endpoints land in the next deploy
    - The legacy `/admin/sessions/{key}/replay` is backported (no regression)
    - The legacy `/admin/providers/{name}/{test,models,kinds}` are backported
  - `CHANGELOG.md` — add an entry under [Unreleased] covering the UI fixes wave.
- **ETA:** 1h

---

## 3. Parallelization

```
Wave 1 (3 parallel — backend backports):     W1.1   W1.2   W1.3
                                                 │
Wave 2 (3 parallel after W1):                 W2.1   W2.2   W2.3
                                                 │
Wave 3 (2 parallel after W2):                 W3.1   W3.2
```

Total wall-clock ~1 working day with 3 concurrent agents.

---

## 4. Explicitly out of scope

- Rewriting the existing `/admin/models` page in v2 shape only (keep dual v1+v2 detection — it works)
- Migrating the credentials page to JSON-Schema-driven form (openclaw pattern) — too much work for the win; hermes EnvPage pattern is already enough
- Per-session compaction checkpoint UI (openclaw has it; corlinman doesn't need it yet — different memory model)
- Spreadsheet-style bulk inline edit on the sessions list (out of "fix what's broken" scope)
- Adding an admin nav search box / command palette — separate effort

---

## 5. Risks

| Risk | Mitigation |
|---|---|
| The live `replay` endpoint may have semantics we don't know | Probe a real session on live before implementing; mirror exact request/response |
| `provider test` could leak api_key via timing | Use a deterministic 1-token request; cap latency report to 5s; never echo the key |
| Backporting endpoints may fight with the new observability `JournalBackedEmitter` | Test in isolation; the new and old endpoints have different paths so they coexist |
| User on a stale UI bundle still sees broken pages | docs/observability.md explicitly tells them to redeploy + `corlinman --upgrade` |

---

## 6. Decision points

- [ ] Plan accepted as-is, or trim subset (e.g. drop W2.2 ModelPickerDialog if hermes' shape doesn't fit Tidepool aesthetic)?
- [ ] OK to backport `/admin/providers/{name}/test` knowing it sends a 1-token chat request (counts toward provider usage)?
- [ ] Past-turns pill row default cap: 10 turns or all-with-virtualization?

---

**End of plan v1.0.**
