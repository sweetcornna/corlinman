# Audit Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the Phase 3/4 audit blockers so corlinman can pass critical security checks, real browser login, and release-grade regression gates.

**Architecture:** Work is split into waves. Wave 1 closes P0 production risks. Wave 2 restores automated quality gates. Wave 3 hardens lower-risk SAST findings. Wave 4 performs cross-stack regression and real client verification. Each worker owns a disjoint write scope unless explicitly sequenced by the controller.

**Tech Stack:** Python 3.13, FastAPI, uv, pytest, Next.js 15, React 19, pnpm, Vitest, Playwright, SwiftPM/macOS, Docker.

---

## Coordination Rules

- The controller keeps the main worktree cleanly integrated and never accepts a worker patch without local verification.
- Workers must not revert unrelated user changes.
- Workers must list every changed file in their final response.
- For each defect, add or update a regression test before or with the production fix.
- Commands should prefer targeted tests first, then broader suites.

## Wave 1: P0 Release Blockers

### Task 1: Enforce Admin Authentication On Route Dependencies

**Owner:** Worker A.

**Files:**
- Modify: `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_a/_auth_shim.py`
- Modify: `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_b/state.py`
- Modify: `python/packages/corlinman-server/tests/gateway/routes_admin_a/test_profiles.py`
- Add or modify tests under: `python/packages/corlinman-server/tests/gateway/routes_admin_a/`

**Problem Evidence:**
- Runtime: unauthenticated `GET /admin/profiles` returned `200 OK`.
- Root cause: `require_admin_dependency()` returns a function object instead of executing auth.

- [ ] **Step 1: Add failing regression test**

Add coverage that proves unauthenticated profile routes fail closed and authenticated requests still work.

```python
def test_profiles_require_admin_auth(tmp_path: Path) -> None:
    app = FastAPI()
    store = ProfileStore(tmp_path)
    state = AdminState(
        data_dir=tmp_path,
        profile_store=store,
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
    )
    set_admin_state(state)
    app.include_router(router())

    with TestClient(app) as client:
        resp = client.get("/admin/profiles")
        assert resp.status_code == 401
```

- [ ] **Step 2: Run failing test**

Run:

```bash
uv run pytest python/packages/corlinman-server/tests/gateway/routes_admin_a/test_profiles.py::test_profiles_require_admin_auth -q
```

Expected before fix: fails because current route returns `200`.

- [ ] **Step 3: Implement real dependency**

`require_admin_dependency` must accept `Request`, validate `corlinman_session` using `AdminState.session_store`, fall back to Basic auth using `AdminState.admin_username` and `AdminState.admin_password_hash`, and raise `HTTPException(401)` when both paths fail.

- [ ] **Step 4: Delegate routes_admin_b to the same dependency**

Change `routes_admin_b.state.require_admin(request: Request)` to call `routes_admin_a._auth_shim.require_admin_dependency(request)` so both admin bundles share the same fail-closed behavior.

- [ ] **Step 5: Update existing profile route tests**

Existing happy-path tests may currently assume no auth. Update the shared `client` fixture to authenticate with a valid cookie or use Basic auth headers intentionally.

- [ ] **Step 6: Verify**

Run:

```bash
uv run pytest python/packages/corlinman-server/tests/gateway/routes_admin_a/test_profiles.py -q
uv run pytest python/packages/corlinman-server/tests/gateway/middleware/test_auth_middleware.py -q
```

Expected: both pass.

### Task 2: Restore Real Browser Login And Full-Stack Playwright Wiring

**Owner:** Worker B.

**Files:**
- Modify: `python/packages/corlinman-server/src/corlinman_server/gateway/lifecycle/entrypoint.py`
- Modify: `ui/playwright.config.ts`
- Add or modify tests under: `python/packages/corlinman-server/tests/gateway/lifecycle/`

**Problem Evidence:**
- Browser fetch to `POST /admin/login` fails with `TypeError: Failed to fetch`.
- Gateway preflight: `OPTIONS /admin/login` returns `405 Method Not Allowed`.
- Playwright full-stack run times out waiting for login URL transition.

- [ ] **Step 1: Add CORS regression test**

Add a test that builds the gateway app with `CORLINMAN_CORS_ORIGINS=http://localhost:3000` and asserts preflight succeeds.

```python
def test_gateway_cors_preflight_allows_configured_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORLINMAN_CORS_ORIGINS", "http://localhost:3000")
    app = build_app(...)
    with TestClient(app) as client:
        resp = client.options(
            "/admin/login",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
    assert resp.status_code in {200, 204}
    assert resp.headers["access-control-allow-origin"] == "http://localhost:3000"
```

Use the existing lifecycle test helpers instead of inventing a new app factory.

- [ ] **Step 2: Implement opt-in CORS**

In `entrypoint.py`, parse comma-separated `CORLINMAN_CORS_ORIGINS`. When non-empty, install `fastapi.middleware.cors.CORSMiddleware` with credentials enabled and methods/headers required by the UI.

- [ ] **Step 3: Wire full-stack Playwright env**

In `ui/playwright.config.ts`, pass `CORLINMAN_CORS_ORIGINS: new URL(baseURL).origin` to the gateway web server and `NEXT_PUBLIC_GATEWAY_URL: gatewayURL` to the UI web server.

- [ ] **Step 4: Verify targeted**

Run:

```bash
uv run pytest python/packages/corlinman-server/tests/gateway/lifecycle -q
CORLINMAN_E2E=1 pnpm -C ui exec playwright test tests/e2e/00-onboard-to-admin.spec.ts --workers=1
```

Expected: preflight test passes; login flow reaches the expected account security/admin state.

## Wave 2: Quality Gates

### Task 3: Fix Python Pytest Collection Collision

**Owner:** Worker C.

**Files:**
- Add package markers or adjust pytest config for `python/packages/*/tests/`
- Modify only Python test package metadata or pytest configuration.

**Problem Evidence:**
- `uv run pytest -m "not live_llm and not live_transport"` fails during collection with `Plugin already registered under a different name`.

- [ ] **Step 1: Reproduce collection error**

Run:

```bash
env PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' uv run pytest -m "not live_llm and not live_transport" --collect-only -q
```

- [ ] **Step 2: Fix module identity**

Prefer adding `__init__.py` markers to package-level `tests` directories that contain `conftest.py`, unless pytest config already has a cleaner local pattern.

Affected directories found during audit:

```text
python/packages/corlinman-wstool/tests
python/packages/corlinman-auto-rollback/tests
python/packages/corlinman-shadow-tester/tests
python/packages/corlinman-tagmemo/tests
python/packages/corlinman-identity/tests
python/packages/corlinman-episodes/tests
python/packages/corlinman-mcp-server/tests
python/packages/corlinman-replay/tests
python/packages/corlinman-skills-registry/tests
python/packages/corlinman-user-model/tests
python/packages/corlinman-evolution-store/tests
python/packages/corlinman-canvas/tests
python/packages/corlinman-nodebridge/tests
python/packages/corlinman-channels/tests
python/packages/corlinman-evolution-engine/tests
```

- [ ] **Step 3: Verify collection and suite progress**

Run:

```bash
env PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' uv run pytest -m "not live_llm and not live_transport" --collect-only -q
env PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' uv run pytest -m "not live_llm and not live_transport" -q
```

Expected: collection no longer fails with duplicate `tests.conftest`. If runtime tests fail after collection, report the first failing modules separately.

### Task 4: Restore UI Unit Test Green

**Owner:** Worker D.

**Files:**
- Modify: `ui/tests/a11y-audit.test.tsx`
- Modify: `ui/components/ui/aurora-background.test.tsx`
- Optional: `ui/components/ui/theme-toggle.tsx` if the fix is small and covered by lint.

**Problem Evidence:**
- `skills has zero serious/critical violations` crashes because `useActiveProfile()` is outside `ActiveProfileProvider`.
- Aurora test expects stale `bg-tp-aurora` class while implementation uses `tp-bg-root`.
- Lint warns `aria-pressed` is not supported by `role=tab`.

- [ ] **Step 1: Fix a11y harness**

Wrap rendered pages in `ActiveProfileProvider` inside the test harness.

- [ ] **Step 2: Fix Aurora assertion**

Assert the current root class `tp-bg-root`, and keep coverage for `fixed` and `-z-10`.

- [ ] **Step 3: Fix tab ARIA warning if local pattern is clear**

For `role="tab"`, use `aria-selected={active}` instead of `aria-pressed={active}`.

- [ ] **Step 4: Verify**

Run:

```bash
pnpm -C ui test -- --run
pnpm -C ui lint
```

Expected: unit tests pass; no new lint errors.

### Task 5: Remediate High-Severity Dependency Advisories

**Owner:** Worker E.

**Files:**
- Modify: `ui/package.json`
- Modify: `pnpm-lock.yaml`
- Modify: `uv.lock`
- Modify package metadata only if needed by uv.

**Problem Evidence:**
- `pnpm audit --json` reports 7 high advisories for `next@15.5.15`.
- `pip-audit` reports CVEs for `idna 3.11` and `urllib3 2.6.3`.

- [ ] **Step 1: Upgrade direct UI dependencies**

Upgrade `next` to a fixed 15.5.x or later compatible release and `next-intl` to a fixed release. Keep React 19 compatibility.

- [ ] **Step 2: Upgrade vulnerable transitive dependencies**

Run a lockfile update that resolves DOMPurify, postcss, vite/esbuild, brace-expansion, and ws advisories where compatible.

- [ ] **Step 3: Upgrade Python vulnerable dependencies**

Use uv to update `idna` to at least `3.15` and `urllib3` to at least `2.7.0`.

- [ ] **Step 4: Verify audits and builds**

Run:

```bash
pnpm -C ui audit --json
pnpm -C ui typecheck
pnpm -C ui build
uv export --all-packages --all-groups --format requirements-txt --no-hashes --locked > /tmp/corlinman-req.txt
uvx pip-audit -r /tmp/corlinman-req.txt --no-deps --disable-pip --format json
```

Expected: no remaining high advisories. Any remaining moderate advisories must be listed with package owner and blocked upgrade reason.

## Wave 3: Security Hardening And Static Quality

### Task 6: Address Low-Risk SAST Findings With Minimal Churn

**Owner:** Worker F.

**Files:**
- Modify: `docker/Dockerfile`
- Modify: `ui/app/(admin)/channels/telegram/page.test.tsx`
- Modify raw SQL sites only when there is a real injection path.

**Problem Evidence:**
- Docker runs as root.
- Test fixture contains a Telegram-looking token.
- Semgrep flags raw SQL in controlled query builders.

- [ ] **Step 1: Docker non-root**

Create a non-root runtime user, chown writable directories, and set `USER`.

- [ ] **Step 2: Redact token-shaped fixture**

Replace token-like test data with a clearly invalid sentinel that still exercises masking/display behavior.

- [ ] **Step 3: Raw SQL triage**

For each Semgrep raw SQL finding, classify as:
- safe controlled column list,
- safe parameterized values,
- or exploitable.

Patch only exploitable cases. For safe dynamic SQL, prefer a short local comment and a Semgrep suppression with rationale.

- [ ] **Step 4: Verify**

Run:

```bash
uvx semgrep --config p/default --metrics=off --json docker python/packages/corlinman-agent-brain/src/corlinman_agent_brain/session_reader.py python/packages/corlinman-server/src/corlinman_server/profiles/store.py
pnpm -C ui test -- ui/app/'(admin)'/channels/telegram/page.test.tsx --run
```

Expected: no new behavior failures; any remaining Semgrep findings are documented as accepted with rationale.

### Task 7: Import Layer And Type Gate Triage

**Owner:** Worker G.

**Files:**
- Modify targeted Python files only after confirming owner boundaries.
- Primary known file: `python/packages/corlinman-providers/src/corlinman_providers/anthropic_provider.py`

**Problem Evidence:**
- `uv run lint-imports` reports provider importing server OAuth.
- `mypy` reports 100 errors in 50 files.

- [ ] **Step 1: Fix import-linter violation first**

Move shared OAuth helper code out of server-owned modules or introduce a provider-local abstraction that does not import `corlinman_server`.

- [ ] **Step 2: Run import-linter**

Run:

```bash
uv run lint-imports
```

Expected: import-linter passes or only unrelated violations remain with evidence.

- [ ] **Step 3: Type-check targeted changed packages**

Run:

```bash
MYPY_CACHE_DIR=/tmp/corlinman-mypy-cache uv run mypy python/packages/corlinman-providers/src python/packages/corlinman-server/src
```

Expected: no new type errors in changed files. Full `mypy python/packages/` may still be a follow-up if unrelated historical errors remain.

## Wave 4: Final Regression And Real Client Test

### Task 8: Full Regression Matrix

**Owner:** Controller plus QA worker.

**Commands:**

```bash
uv run ruff check . --no-cache
uv run ruff format --check .
uv run lint-imports
MYPY_CACHE_DIR=/tmp/corlinman-mypy-cache uv run mypy python/packages/
env PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' uv run pytest -m "not live_llm and not live_transport"
pnpm -C ui typecheck
pnpm -C ui lint
pnpm -C ui test -- --run
pnpm -C ui build
CORLINMAN_E2E=1 pnpm -C ui exec playwright test tests/e2e/00-onboard-to-admin.spec.ts --workers=1
```

**Expected:** P0/P1 gates pass. If broad static gates still fail due historical debt, the final report must separate new regressions from pre-existing debt.

### Task 9: Real Client Verification With Computer Use

**Owner:** Controller.

**Steps:**

- Start gateway with a throwaway data dir.
- Start UI dev server with `NEXT_PUBLIC_GATEWAY_URL`.
- Open Chrome to `http://127.0.0.1:<ui-port>/login`.
- Login with seeded `admin/root`.
- Confirm redirect to account security or admin page.
- Confirm unauthenticated `GET /admin/profiles` still returns `401`.
- Stop all servers.

**Expected:** Browser no longer shows `TypeError: Failed to fetch`; URL changes after login.

## Wave Dependencies

1. Wave 1 must finish before final E2E.
2. Wave 2 Task 5 should run after Task 4 if lockfile changes disturb UI tests.
3. Wave 3 can run after Wave 1 starts, but must not touch auth files.
4. Wave 4 runs only after all accepted patches are integrated.

## Done Criteria

- P0 auth bypass is fixed and covered by tests.
- Real browser login works in Chrome.
- `pnpm -C ui test`, `pnpm -C ui build`, and targeted Playwright login pass.
- Python collection no longer fails with duplicate `tests.conftest`.
- Dependency audits show no high advisories, or every remaining high is blocked with evidence and explicit owner approval.
- Final report lists changed files, verification commands, and residual risks.
